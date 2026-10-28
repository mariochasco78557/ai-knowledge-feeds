"""Human/agent-readable indexes generated from the SQLite index.

  index/INDEX.md                 one line per note/release/repo; docs as section counts
  index/pages/<source>.md        full page list per docs source
  index/topics/<tag>.md          items per taxonomy tag (+ top canonical doc pages)
  index/WHATS_NEW.md             rolling 14 days
  whats_new_compact()            <=1.5 KB digest for the SessionStart hook
"""
from __future__ import annotations

import re
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from kb import db
from kb import root as R
from kb.classify import TAXONOMY
from kb.state import atomic_write_text, load_last_run

PRERELEASE_RE = re.compile(r"(alpha|beta|rc|preview|nightly)", re.I)


def _one(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).replace("|", "/").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _is_new_candidate(it: dict) -> bool:
    return not it["meta"].get("baseline") and not it["meta"].get("legacy")


def _doc_section(item_id: str) -> str:
    # docs:<source>:<lang>/<section>/<page>.md
    path = item_id.split(":", 2)[-1]
    parts = path.split("/")
    if parts and re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", parts[0]):
        parts = parts[1:]
    return parts[0] if len(parts) > 1 else "(root)"


def _last_sync_line() -> tuple[str, dict]:
    lr = load_last_run()
    when = lr.get("finished") or lr.get("started") or "never"
    if when != "never":
        try:
            when = datetime.fromisoformat(when).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass
    return f"last sync {when} ({lr.get('status', 'unknown')})", lr


def _proxy_banner() -> str | None:
    """Warning line when the YouTube proxy was down in 3+ consecutive runs."""
    from kb.state import load_proxy

    n = int(load_proxy().get("consecutive_down") or 0)
    if n >= 3:
        return (f"WARNING: YouTube proxy down for {n} consecutive runs; video subtitles are not being "
                "fetched (check `kb doctor`, your proxy service).")
    return None


def _failure_banner(lr: dict) -> str | None:
    if lr.get("status") == "failed":
        reason = lr.get("failure_reason") or "; ".join((lr.get("errors") or [])[:2])
        return f"WARNING: last KB sync FAILED ({_one(reason, 120)}). Data may be stale; check `kb status` / logs/."
    if lr.get("status") == "partial":
        return "Note: last KB sync was partial (some sources errored); see `kb status`."
    return None


# ---------------------------------------------------------------------------
# INDEX.md + pages + topics
# ---------------------------------------------------------------------------

def generate_all() -> dict[str, int]:
    items = db.all_items()
    out_dir = R.index_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    n_topics = _write_topics(items)
    n_pages = _write_index(items)
    _write_whats_new(items)
    return {"items": len(items), "topics": n_topics, "doc_sources": n_pages}


def _write_index(items: list[dict]) -> int:
    by_src: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_src[it["source"]].append(it)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sync_line, _ = _last_sync_line()
    lines = [
        "# AI Knowledge Feeds: index",
        "",
        f"Generated {now}; {sync_line}; {len(items)} indexed items.",
        "Query with MCP tools `kb_search` then `kb_get` (or `uv run kb search/get`). "
        "Authority: **canonical** (official docs/changelogs) beats **practice** (creators).",
        "Line format: `date | title | tags | summary | id`.",
        "",
    ]
    pages_dir = R.index_dir() / "pages"
    n_doc_sources = 0
    order = sorted(by_src, key=lambda s: (0 if by_src[s][0]["authority"] == "canonical" else 1, s))
    for src in order:
        its = by_src[src]
        kinds = {it["kind"] for it in its}
        auth = its[0]["authority"]
        if "note" in kinds or "transcript" in kinds:
            notes = [i for i in its if i["kind"] == "note"]
            noted_parents = {n["parent_id"] for n in notes}
            orphans = [i for i in its if i["kind"] == "transcript" and i["id"] not in noted_parents]
            rows = sorted(notes + orphans, key=lambda i: (i["date"], i["id"]), reverse=True)
            lines += [f"## YouTube: {src} ({auth}), {len(rows)} videos", ""]
            for i in rows:
                flag = "" if i["kind"] == "note" else " (transcript only, no note)"
                lines.append(f"- {i['date']} | {_one(i['title'], 90)}{flag} | {', '.join(i['tags'])} | "
                             f"{_one(i['summary'], 160)} | {i['id']}")
            lines.append("")
        if "doc" in kinds:
            n_doc_sources += 1
            docs = sorted([i for i in its if i["kind"] == "doc"], key=lambda i: i["id"])
            sec = defaultdict(int)
            for d in docs:
                sec[_doc_section(d["id"])] += 1
            lines += [f"## Docs: {src} ({auth}), {len(docs)} pages", "",
                      f"Full page list: [pages/{src}.md](pages/{src}.md). Sections: "
                      + ", ".join(f"{k} ({v})" for k, v in sorted(sec.items(), key=lambda x: -x[1])), ""]
            pages_dir.mkdir(parents=True, exist_ok=True)
            plines = [f"# Docs pages: {src} ({auth}), {len(docs)} pages", "",
                      "Line format: `section | title | id | url`", ""]
            for d in docs:
                plines.append(f"- {_doc_section(d['id'])} | {_one(d['title'], 100)} | {d['id']} | {d['url']}")
            atomic_write_text(pages_dir / f"{src}.md", "\n".join(plines) + "\n")
        changes = sorted([i for i in its if i["kind"] == "change"], key=lambda i: i["date"], reverse=True)
        if changes:
            lines += [f"### Doc change logs: {src}", ""]
            for c in changes[:15]:
                n = len(c["meta"].get("entries") or [])
                lines.append(f"- {c['date']} | {n} page(s) changed | | {_one(c['summary'], 120)} | {c['id']}")
            lines.append("")
        rels = sorted([i for i in its if i["kind"] == "release"], key=lambda i: (i["date"], i["id"]), reverse=True)
        if rels:
            lines += [f"## Releases: {src} ({auth}), {len(rels)} versions (latest 20)", ""]
            for r in rels[:20]:
                lines.append(f"- {r['date'] or '?'} | {_one(r['title'], 60)} | | {_one(r['summary'], 140)} | {r['id']}")
            lines.append("")
        repos = sorted([i for i in its if i["kind"] == "repo"], key=lambda i: i["title"].lower())
        if repos:
            lines += [f"## Repos: {src} ({auth}), {len(repos)}", ""]
            for r in repos:
                lines.append(f"- {r['date']} | {_one(r['title'], 60)} | | {_one(r['summary'], 140)} | {r['id']}")
            lines.append("")
    atomic_write_text(R.index_dir() / "INDEX.md", "\n".join(lines).rstrip() + "\n")
    return n_doc_sources


def _write_topics(items: list[dict]) -> int:
    tdir = R.index_dir() / "topics"
    tdir.mkdir(parents=True, exist_ok=True)
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        if it["kind"] == "transcript" or (it.get("meta") or {}).get("private"):
            continue  # private local sources (private: true) must not spawn per-tag topic pages
        for t in it["tags"]:
            by_tag[t].append(it)
    written = set()
    con = db.connect()
    try:
        for tag in sorted(set(by_tag) | set(TAXONOMY)):
            its = sorted(by_tag.get(tag, []), key=lambda i: (i["date"], i["id"]), reverse=True)
            docs = []
            if tag not in ("other", "business", "clients", "pricing", "automation", "workflow"):
                docs = db.search(tag.replace("-", " "), kind="doc", limit=10, con=con)
            if not its and not docs:
                continue
            lines = [f"# Topic: {tag}", "",
                     "Canonical beats practice. Use `kb_get <id>` for details.", ""]
            if docs:
                lines += ["## Canonical docs (top matches)", ""]
                lines += [f"- {_one(d['title'], 90)} @ {d['locator']} | {d['id']}" for d in docs]
                lines.append("")
            if its:
                lines += [f"## Items ({len(its)})", ""]
                lines += [f"- {i['date']} | [{i['authority']}/{i['kind']}] {_one(i['title'], 90)} | "
                          f"{_one(i['summary'], 140)} | {i['id']}" for i in its]
            atomic_write_text(tdir / f"{tag}.md", "\n".join(lines).rstrip() + "\n")
            written.add(f"{tag}.md")
    finally:
        con.close()
    for p in tdir.glob("*.md"):
        if p.name not in written:
            p.unlink()
    return len(written)


def topic_counts() -> list[tuple[str, int]]:
    c: dict[str, int] = defaultdict(int)
    for it in db.all_items():
        if it["kind"] == "transcript":
            continue
        for t in it["tags"]:
            c[t] += 1
    return sorted(c.items(), key=lambda x: (-x[1], x[0]))


# ---------------------------------------------------------------------------
# WHATS_NEW
# ---------------------------------------------------------------------------

def collect_new(days: int, topic: str | None = None, items: list[dict] | None = None) -> dict[str, list[dict]]:
    """Entries per category within `days`. Each entry: date, authority, kind,
    source, title, summary, id."""
    items = items if items is not None else db.all_items()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out: dict[str, list[dict]] = {"changes": [], "releases": [], "videos": [], "repos": []}
    for it in items:
        if not it["date"] or it["date"] < cutoff or not _is_new_candidate(it):
            continue
        tword = topic.replace("-", " ").lower() if topic else ""
        if topic and it["kind"] != "change" and topic not in it["tags"] \
                and tword not in f"{it['title']} {it['summary']}".lower():
            continue
        base = {"date": it["date"], "authority": it["authority"], "kind": it["kind"],
                "source": it["source"], "title": it["title"], "summary": it["summary"], "id": it["id"],
                "tags": it["tags"]}
        if it["kind"] == "change":
            for e in it["meta"].get("entries") or []:
                if topic and tword not in f"{e.get('title', '')} {e.get('summary', '')}".lower():
                    continue
                summ = "; ".join(re.sub(r"^\s*[-*]\s*", "", l).strip()
                                 for l in str(e.get("summary", "")).splitlines() if l.strip())
                out["changes"].append({**base, "title": e.get("title", ""), "summary": summ,
                                       "id": e.get("id", it["id"]), "removed": e.get("removed", False)})
        elif it["kind"] == "release":
            out["releases"].append(base)
        elif it["kind"] == "note":
            out["videos"].append(base)
        elif it["kind"] == "repo":
            out["repos"].append(base)
    for k in out:
        out[k].sort(key=lambda e: (e["date"], e["id"]), reverse=True)
    return out


def _write_whats_new(items: list[dict], days: int = 14) -> None:
    sync_line, lr = _last_sync_line()
    new = collect_new(days, items=items)
    lines = []
    banner = _failure_banner(lr)
    if banner:
        lines += [f"> {banner}", ""]
    proxy_down = _proxy_banner()
    if proxy_down:
        lines += [f"> {proxy_down}", ""]
    lines += [f"# What's new in the AI KB (last {days} days)", "",
              f"{sync_line}. Canonical beats practice. Details: `kb_get <id>`.", ""]
    lines += ["## Official docs changes (canonical)", ""]
    if new["changes"]:
        for e in new["changes"]:
            tag = "REMOVED " if e.get("removed") else ""
            lines.append(f"- {e['date']} [{e['source']}] {tag}{_one(e['title'], 80)}: {_one(e['summary'], 300)} ({e['id']})")
    else:
        lines.append("- none")
    lines += ["", "## Releases (canonical)", ""]
    if new["releases"]:
        per_src: dict[str, list[dict]] = defaultdict(list)
        for e in new["releases"]:
            per_src[e["source"]].append(e)
        for src, es in per_src.items():
            stable = [e for e in es if not PRERELEASE_RE.search(e["title"] + e["id"])]
            shown = (stable or es)[:6]
            for e in shown:
                lines.append(f"- {e['date']} {src} {_one(e['title'], 50)}: {_one(e['summary'], 200)} ({e['id']})")
            hidden = len(es) - len(shown)
            if hidden > 0:
                lines.append(f"- … {src}: {hidden} more (incl. pre-releases); `kb_search` with source={src}")
    else:
        lines.append("- none")
    lines += ["", "## New videos (practice)", ""]
    if new["videos"]:
        for e in new["videos"]:
            lines.append(f"- {e['date']} {e['source']}: {_one(e['title'], 90)} [{', '.join(e['tags'])}] "
                         f"{_one(e['summary'], 160)} ({e['id']})")
    else:
        lines.append("- none")
    if new["repos"]:
        lines += ["", "## New repos (practice)", ""]
        for e in new["repos"]:
            lines.append(f"- {e['date']} {e['source']}: {_one(e['title'], 60)} {_one(e['summary'], 120)} ({e['id']})")
    atomic_write_text(R.index_dir() / "WHATS_NEW.md", "\n".join(lines).rstrip() + "\n")


def whats_new_text(days: int = 7, topic: str | None = None, max_lines: int = 60) -> str:
    """Compact plain text for the MCP tool."""
    sync_line, lr = _last_sync_line()
    new = collect_new(days, topic)
    out = []
    banner = _failure_banner(lr)
    if banner:
        out.append(banner)
    out.append(f"AI KB, {sync_line}. Last {days}d" + (f", topic={topic}" if topic else "") + ":")
    rows = []
    for e in new["changes"]:
        rows.append(f"[canonical/doc-change] {e['date']} {e['source']}: {_one(e['title'], 70)}: {_one(e['summary'], 220)} ({e['id']})")
    rel = new["releases"]
    stable = [e for e in rel if not PRERELEASE_RE.search(e["title"] + e["id"])]
    for e in stable[:15]:
        rows.append(f"[canonical/release] {e['date']} {e['source']} {_one(e['title'], 40)}: {_one(e['summary'], 160)} ({e['id']})")
    if len(rel) > len(stable[:15]):
        rows.append(f"[canonical/release] +{len(rel) - len(stable[:15])} more (pre-releases/older)")
    for e in new["videos"]:
        rows.append(f"[practice/video] {e['date']} {e['source']}: {_one(e['title'], 80)}: {_one(e['summary'], 160)} ({e['id']})")
    for e in new["repos"]:
        rows.append(f"[practice/repo] {e['date']} {e['source']}: {_one(e['title'], 50)} ({e['id']})")
    if not rows:
        rows.append("nothing new")
    if len(rows) > max_lines:
        rows = rows[:max_lines] + [f"… {len(rows) - max_lines} more"]
    return "\n".join(out + rows)


def whats_new_compact(days: int = 7, max_items: int = 10, max_bytes: int = 1500) -> str:
    """SessionStart digest: never raises, never exceeds max_bytes."""
    kb_path = str(R.root())
    try:
        sync_line, lr = _last_sync_line()
    except Exception:  # noqa: BLE001
        sync_line, lr = "last sync unknown", {}
    header = [f"[AI KB] {kb_path} | {sync_line}",
              "Planning/tooling decisions on Claude Code, Codex, agents, MCP or automation: "
              "call MCP ai-kb kb_whats_new/kb_search, then kb_get fragments. Canonical beats practice."]
    banner = _failure_banner(lr)
    if banner:
        header.insert(0, banner)
    rows: list[str] = []
    try:
        if not R.db_path().exists():
            rows = ["(index not built yet: run `uv run kb reindex`)"]
        else:
            new = collect_new(days)
            canon = new["changes"] + [e for e in new["releases"] if not PRERELEASE_RE.search(e["title"] + e["id"])]
            canon.sort(key=lambda e: e["date"], reverse=True)
            practice = new["videos"] + new["repos"]
            for e in canon:
                what = "docs" if e["kind"] == "change" else "release"
                rows.append(f"- [canonical] {e['date'][5:]} {what} {e['source']}: {_one(e['title'], 50)}: {_one(e['summary'], 70)} ({e['id']})")
            for e in practice:
                rows.append(f"- [practice] {e['date'][5:]} {e['source']}: {_one(e['title'], 60)}: {_one(e['summary'], 60)} ({e['id']})")
            total = len(rows)
            rows = rows[:max_items]
            header.append(f"New in last {days}d ({total} items" + (f", top {len(rows)}" if total > len(rows) else "") + "):")
            if not rows:
                rows = ["- nothing new"]
    except Exception as e:  # noqa: BLE001
        rows = [f"(digest unavailable: {type(e).__name__})"]
    text = "\n".join(header)
    for r in rows:
        cand = text + "\n" + r
        if len(cand.encode("utf-8")) > max_bytes - 20:
            text += "\n- … (more: kb_whats_new)"
            break
        text = cand
    b = text.encode("utf-8")
    if len(b) > max_bytes:
        text = b[:max_bytes].decode("utf-8", "ignore")
    return text


def clean_generated() -> None:
    """Remove generated markdown (used by reindex before regenerating)."""
    for sub in ("topics", "pages"):
        p = R.index_dir() / sub
        if p.exists():
            shutil.rmtree(p)
