"""Persistent sync state (JSON files under state/) and the sync lock.

Files:
  state/seen.json        {source: {video_id: {status, first_seen, attempts,
                                              last_error, relevant, title,
                                              updated}}}
  state/doc_hashes.json  {source: {page_id: sha256}}   (shape owned by docs.py)
  state/gh_seen.json     {source: [tag, ...]}
  state/last_run.json    {started, finished, status, counts, errors}
  state/sync.lock        fcntl lock file (gitignored)

All writes are atomic (tmp file + fsync + os.replace).

Pending retry policy: a video whose subtitles are not available yet (or whose
processing failed) stays `pending` and is retried on every run until
PENDING_MAX_AGE since `first_seen`; after that it becomes `failed` and is no
longer retried automatically (use `kb backfill` or edit seen.json to retry).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from kb import root as R

# Long on purpose: the trickle only runs in weekday-morning/weekend windows, so a
# large backfill queue or a multi-day proxy outage must not expire videos.
PENDING_MAX_AGE = timedelta(days=60)

# Retry backoff for pending items that were tried and failed (e.g. "no subtitles
# yet"): after attempt n wait BACKOFF[n-1], then BACKOFF_TAIL forever. Give up
# (status failed) after MAX_ATTEMPTS attempts or GIVE_UP_AGE since first_seen
# (the age rule only applies once at least one attempt was made, so videos
# queued by `backfill --discover-only` are not expired before being tried).
BACKOFF = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
BACKOFF_TAIL = timedelta(hours=48)
MAX_ATTEMPTS = 20
GIVE_UP_AGE = timedelta(days=30)


def backoff_delay(attempts: int) -> timedelta:
    """Wait before the next try, given the number of attempts made so far."""
    if attempts <= 0:
        return timedelta(0)
    return BACKOFF[attempts - 1] if attempts <= len(BACKOFF) else BACKOFF_TAIL

DONE, PENDING, SKIPPED, FAILED = "done", "pending", "skipped", "failed"
TERMINAL = {DONE, SKIPPED, FAILED}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# atomic JSON io
# ---------------------------------------------------------------------------

def _path(name: str) -> Path:
    return R.state_dir() / name


def read_json(name: str, default: Any) -> Any:
    p = _path(name)
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        # Corrupt state must not silently reset progress: keep a copy aside.
        bad = p.with_suffix(p.suffix + f".corrupt-{datetime.now():%Y%m%d%H%M%S}")
        with contextlib.suppress(OSError):
            os.replace(p, bad)
        return default


def write_json(name: str, obj: Any) -> None:
    atomic_write_text(_path(name), json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------

class LockBusy(RuntimeError):
    pass


@contextlib.contextmanager
def sync_lock() -> Iterator[None]:
    """Non-blocking exclusive lock; raises LockBusy if another sync runs."""
    p = _path("sync.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockBusy(f"another kb sync holds {p}") from None
            raise
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {now_iso()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# seen.json (YouTube)
# ---------------------------------------------------------------------------

class Seen:
    """In-memory view of seen.json. Call save() after each item so a crash
    mid-run never loses completed work."""

    NAME = "seen.json"

    def __init__(self, force_pending: bool = False) -> None:
        self.data: dict[str, dict[str, dict]] = read_json(self.NAME, {})
        self.force_pending = force_pending  # bypass next_try backoff

    def save(self) -> None:
        write_json(self.NAME, self.data)

    def get(self, source: str, vid: str) -> dict | None:
        return self.data.get(source, {}).get(vid)

    def _entry(self, source: str, vid: str) -> dict:
        src = self.data.setdefault(source, {})
        e = src.get(vid)
        if e is None:
            e = {"status": PENDING, "first_seen": now_iso(), "attempts": 0,
                 "last_error": None, "relevant": None}
            src[vid] = e
        return e

    def should_process(self, source: str, vid: str, now: datetime | None = None) -> bool:
        """True for unseen or retryable pending videos. Expired pending
        entries are flipped to failed here (and not processed)."""
        e = self.get(source, vid)
        if e is None:
            return True
        if e.get("status") in TERMINAL:
            return False
        now = now or datetime.now(timezone.utc)
        first = parse_iso(e.get("first_seen")) or now
        attempts = int(e.get("attempts") or 0)
        reason = None
        if now - first >= PENDING_MAX_AGE:
            reason = f"gave up after {PENDING_MAX_AGE.days}d pending"
        elif attempts >= MAX_ATTEMPTS:
            reason = f"gave up after {attempts} attempts"
        elif attempts > 0 and now - first >= GIVE_UP_AGE:
            reason = f"gave up after {GIVE_UP_AGE.days}d and {attempts} attempts"
        if reason:
            e["status"] = FAILED
            e["last_error"] = (e.get("last_error") or "") + f" | {reason}"
            e["updated"] = now_iso()
            e.pop("next_try", None)
            return False
        nxt = parse_iso(e.get("next_try"))  # missing field = eligible now
        if nxt and nxt > now and not self.force_pending:
            return False
        return True

    def mark(self, source: str, vid: str, status: str, *, relevant: Any = None,
             error: str | None = None, title: str | None = None,
             count_attempt: bool = False, save: bool = True, **extra: Any) -> dict:
        e = self._entry(source, vid)
        e["status"] = status
        if relevant is not None:
            e["relevant"] = relevant
        if title:
            e["title"] = title
        if count_attempt:
            e["attempts"] = int(e.get("attempts") or 0) + 1
        e["last_error"] = error
        e["updated"] = now_iso()
        e.update({k: v for k, v in extra.items() if v is not None})
        if save:
            self.save()
        return e

    def fail_attempt(self, source: str, vid: str, error: str, *, title: str | None = None,
                     relevant: Any = None, now: datetime | None = None) -> dict:
        """Record a failed attempt: stays pending until PENDING_MAX_AGE since first_seen,
        then failed."""
        e = self._entry(source, vid)
        now = now or datetime.now(timezone.utc)
        first = parse_iso(e.get("first_seen")) or now
        attempts = int(e.get("attempts") or 0) + 1
        too_old = now - first >= GIVE_UP_AGE
        give_up = attempts >= MAX_ATTEMPTS or too_old
        error = error[:500]
        if give_up:
            error += f" | gave up after {attempts} attempts / {(now - first).days}d"
        self.mark(source, vid, FAILED if give_up else PENDING, error=error, title=title,
                  relevant=relevant, count_attempt=True, save=False)
        if give_up:
            e.pop("next_try", None)
        else:
            e["next_try"] = (now + backoff_delay(attempts)).replace(microsecond=0).isoformat()
        self.save()
        return e

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for src, vids in self.data.items():
            c: dict[str, int] = {}
            for e in vids.values():
                c[e.get("status", "?")] = c.get(e.get("status", "?"), 0) + 1
            out[src] = c
        return out

    def items_with_status(self, status: str) -> list[tuple[str, str, dict]]:
        return [(s, v, e) for s, vids in self.data.items() for v, e in vids.items()
                if e.get("status") == status]


# ---------------------------------------------------------------------------
# other state files
# ---------------------------------------------------------------------------

def load_doc_hashes() -> dict:
    return read_json("doc_hashes.json", {})


def save_doc_hashes(h: dict) -> None:
    write_json("doc_hashes.json", h)


def load_gh_seen() -> dict[str, list[str]]:
    return read_json("gh_seen.json", {})


def save_gh_seen(d: dict) -> None:
    write_json("gh_seen.json", {k: sorted(set(v)) for k, v in d.items()})


def load_last_run() -> dict:
    return read_json("last_run.json", {})


def save_last_run(d: dict) -> None:
    write_json("last_run.json", d)


def load_proxy() -> dict:
    return read_json("proxy.json", {})


def record_proxy(ok: bool) -> int:
    """Record one run's proxy check; returns consecutive runs that saw it down."""
    d = load_proxy()
    d["consecutive_down"] = 0 if ok else int(d.get("consecutive_down") or 0) + 1
    d["last_checked"] = now_iso()
    d["last_status"] = "ok" if ok else "down"
    if ok:
        d["last_ok"] = d["last_checked"]
    write_json("proxy.json", d)
    return d["consecutive_down"]


def mark_failure(reason: str = "systemd OnFailure") -> dict:
    lr = load_last_run()
    lr["status"] = "failed"
    lr["failure_reason"] = reason
    lr["failure_marked_at"] = now_iso()
    save_last_run(lr)
    return lr
