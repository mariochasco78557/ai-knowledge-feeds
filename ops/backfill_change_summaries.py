"""Backfill doc-change summaries that a sync skipped (MAX_DOC_CHANGE_LLM_CALLS cap) or failed.

Old/new page text is recovered from git: the first commit on the change-log date that
touched the page (new) and its parent (old). Each change log is rewritten after every
summary, so the job is resumable: re-running only picks entries still marked skipped.

Usage: uv run python ops/backfill_change_summaries.py [--limit N] [--pause SECONDS]
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import time
from datetime import date, timedelta

from kb import root as R  # must be first: applies AI_KB_ROOT override
from kb import classify, db
from kb.frontmatter import doc_from_file, split, write_doc

log = logging.getLogger("backfill")
MARKERS = ("summary skipped", "summary unavailable")


def git(*args: str) -> str | None:
    p = subprocess.run(["git", *args], cwd=R.root(), capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def versions(path: str, day: str) -> tuple[str, str] | None:
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    out = git("log", "--reverse", "--format=%H", f"--since={day} 00:00", f"--until={nxt} 00:00", "--", path)
    commits = (out or "").split()
    if not commits:
        return None
    new = git("show", f"{commits[0]}:{path}")
    old = git("show", f"{commits[0]}^:{path}")
    if new is None or old is None:
        return None
    return split(old)[1], split(new)[1]


def rewrite(path, doc, con) -> None:
    es = doc.meta["entries"]
    key, day = doc.source, doc.date
    body = [f"# Docs changes: {key} ({day})", ""]
    for e in es:
        body += [f"## {e['title']}", "", f"{e.get('url', '')} (`{e['id']}`)", "", e["summary"], ""]
    write_doc(doc, R.root(), "\n".join(body))
    db.upsert(doc, con)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pause", type=float, default=3.0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    con = db.connect()
    files = sorted((R.data() / "docs").glob("*/changes/*.md"), reverse=True)  # newest first
    done = 0
    for f in files:
        doc = doc_from_file(f, R.root())
        if not doc or not doc.meta or not doc.meta.get("entries"):
            continue
        pending = [e for e in doc.meta["entries"] if any(m in e.get("summary", "") for m in MARKERS)]
        if not pending:
            continue
        log.info("%s: %d to summarize", f.relative_to(R.root()), len(pending))
        for e in pending:
            item = db.get_item(e["id"], con)
            v = versions(item["path"], doc.date) if item and item.get("path") else None
            if not v:
                log.warning("no git versions for %s on %s, leaving as is", e["id"], doc.date)
                continue
            summ = classify.summarize_doc_change(e["title"], v[0], v[1])
            if "summary unavailable" in summ:
                log.warning("LLM failed for %s, will retry on next run", e["id"])
                time.sleep(a.pause * 5)
                continue
            e["summary"] = "- Minor wording/formatting changes." if "No textual change" in summ else summ
            rewrite(f, doc, con)
            done += 1
            log.info("[%d] %s: %s", done, e["title"], e["summary"].splitlines()[0][:120])
            if a.limit and done >= a.limit:
                return
            time.sleep(a.pause)
    log.info("backfill finished: %d summaries written", done)


if __name__ == "__main__":
    main()
