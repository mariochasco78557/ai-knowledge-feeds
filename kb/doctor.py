"""`kb doctor`: one-screen health check (proxy, pending backoff, failed, timers)."""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone

from kb import root as R
from kb.state import Seen, load_last_run, load_proxy, parse_iso

TIMERS = ("kb-sync.timer", "kb-trickle-weekday.timer", "kb-trickle-weekend.timer",
          "kb-rediscover.timer", "kb-summaries.timer")
SERVICES: tuple[str, ...] = ()


def _systemctl_show(unit: str, prop: str) -> str | None:
    if not shutil.which("systemctl"):
        return None
    try:
        p = subprocess.run(["systemctl", "--user", "show", "-p", prop, unit],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    val = p.stdout.strip().partition("=")[2].strip()
    return val or "n/a"


def _first_youtube_cfg() -> dict | None:
    try:
        import yaml

        with R.sources_yaml().open(encoding="utf-8") as f:
            for s in (yaml.safe_load(f) or {}).get("sources") or []:
                if s.get("type") == "youtube":
                    return s
    except Exception:  # noqa: BLE001
        return None
    return None


def doctor_text(check_network: bool = True, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = ["== proxy =="]
    pr = load_proxy()
    if check_network:
        from kb.sources import youtube

        ok = youtube.youtube_reachable(_first_youtube_cfg())
        lines.append(f"proxy: {'ok' if ok else 'DOWN'} (live check)")
    else:
        lines.append("proxy: live check skipped")
    lines.append(f"last run check: {pr.get('last_status', '-')} at {pr.get('last_checked', '-')}, "
                 f"consecutive runs down: {pr.get('consecutive_down', 0)}")

    seen = Seen()
    pending = seen.items_with_status("pending")
    failed = seen.items_with_status("failed")
    lines += ["", f"== pending ({len(pending)}) =="]
    for src, vid, e in sorted(pending, key=lambda t: t[2].get("next_try") or "")[:15]:
        nxt = parse_iso(e.get("next_try"))
        when = "now" if not nxt or nxt <= now else nxt.strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"  {src}/{vid} attempts={e.get('attempts', 0)} next_try={when} "
                     f"{str(e.get('title') or '')[:50]} err={str(e.get('last_error') or '')[:60]}")
    if len(pending) > 15:
        lines.append(f"  ... {len(pending) - 15} more")
    lines.append(f"failed: {len(failed)}")

    lr = load_last_run()
    lines += ["", "== last sync ==", f"status={lr.get('status', 'never')} finished={lr.get('finished', '-')}"]

    lines += ["", "== systemd (user) =="]
    shown = False
    for unit in TIMERS:
        v = _systemctl_show(unit, "LastTriggerUSec")
        if v is not None:
            shown = True
            lines.append(f"  {unit}: last trigger {v}")
    for unit in SERVICES:
        v = _systemctl_show(unit, "ActiveEnterTimestamp")
        if v is not None:
            shown = True
            lines.append(f"  {unit}: active since {v}")
    if not shown:
        lines.append("  (systemctl unavailable)")
    return "\n".join(lines)
