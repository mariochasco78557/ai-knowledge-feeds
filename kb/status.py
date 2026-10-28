"""`kb status` text (shared by CLI and MCP)."""
from __future__ import annotations

from kb import db
from kb import root as R
from kb.state import Seen, load_last_run


def status_text() -> str:
    lr = load_last_run()
    lines = [f"KB root: {R.root()}",
             f"last run: started={lr.get('started', '-')} finished={lr.get('finished', '-')} "
             f"status={lr.get('status', 'never')}"]
    if lr.get("counts"):
        lines.append("last run counts: " + ", ".join(f"{k}={v}" for k, v in lr["counts"].items()))
    if lr.get("errors"):
        lines.append(f"last run errors ({len(lr['errors'])}): " + " | ".join(str(e)[:160] for e in lr["errors"][:5]))
    if R.db_path().exists():
        by_src: dict[str, list[str]] = {}
        for src, kind, n in db.counts():
            by_src.setdefault(src, []).append(f"{kind}={n}")
        lines.append("indexed: " + "; ".join(f"{s}: {', '.join(v)}" for s, v in sorted(by_src.items())))
    else:
        lines.append("indexed: (no index; run `kb reindex`)")
    seen = Seen()
    for src, c in sorted(seen.counts().items()):
        lines.append(f"youtube state {src}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    for status in ("pending", "failed"):
        its = seen.items_with_status(status)
        for src, vid, e in its[:10]:
            lines.append(f"  {status}: {src}/{vid} attempts={e.get('attempts', 0)} "
                         f"first_seen={e.get('first_seen', '')[:16]} {str(e.get('title') or '')[:60]} "
                         f"err={str(e.get('last_error') or '')[:100]}")
        if len(its) > 10:
            lines.append(f"  … {len(its) - 10} more {status}")
    return "\n".join(lines)
