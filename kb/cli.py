"""kb command-line interface.

  kb sync [--dry-run] [--only youtube|docs|github]
  kb backfill --source S --since YYYY-MM-DD [--limit N] [--dry-run]
  kb import-local SOURCE [--dir DIR]
  kb search QUERY [--source --authority --kind --since --limit]
  kb get|show ID [--locator --around --chunk --max-chars]
  kb whats-new [--compact] [--days N] [--topic T]
  kb status | reindex | mark-failure | git-commit

Heavy modules (sources, classify network paths) are imported lazily so that
`kb whats-new --compact` (SessionStart hook) stays fast.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import subprocess
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

from kb import root as R  # must be first: applies AI_KB_ROOT override
from kb.models import Doc

log = logging.getLogger("kb")

MAX_DOC_CHANGE_LLM_CALLS = 40


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def setup_logging(to_file: bool) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if to_file:
        R.logs().mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(R.logs() / f"sync-{datetime.now():%Y-%m}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_sources() -> list[dict]:
    import yaml

    with R.sources_yaml().open(encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("sources") or []


def source_cfg(key: str) -> dict:
    for s in load_sources():
        if s.get("key") == key:
            return s
    raise SystemExit(f"unknown source {key!r} (see sources.yaml)")


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(R.root()))
    except ValueError:
        return str(p.resolve().relative_to(R.root().resolve()))


def today() -> str:
    return date.today().isoformat()


class Run:
    """Accumulates counters/errors for last_run.json."""

    def __init__(self) -> None:
        from kb.state import now_iso

        self.started = now_iso()
        self.counts: dict[str, int] = {}
        self.errors: list[str] = []
        self.source_errors = 0
        self.proxy_ok: bool | None = None  # set by the first YouTube proxy check of the run

    def inc(self, k: str, n: int = 1) -> None:
        self.counts[k] = self.counts.get(k, 0) + n

    def err(self, msg: str, source_level: bool = False) -> None:
        log.error(msg)
        self.errors.append(msg[:400])
        if source_level:
            self.source_errors += 1


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def _yt_paths(src: str) -> dict[str, Path]:
    base = R.data() / "youtube" / src
    return {"raw": base / "transcripts_raw", "transcripts": base / "transcripts", "notes": base / "notes"}


def process_video(cfg: dict, v: dict, seen, con, run: Run, dry_run: bool) -> str | None:
    """Returns None when no network/LLM work was done, else "ok" or "fail"."""
    from kb import classify, clean, db
    from kb.frontmatter import render, split
    from kb.sources import youtube
    from kb.state import DONE, SKIPPED, atomic_write_text, now_iso

    src, vid = cfg["key"], v["id"]
    title, desc = v.get("title", ""), v.get("description", "")
    authority = cfg.get("authority", "practice")
    before = (seen.get(src, vid) or {}).get("status")
    if not seen.should_process(src, vid):
        if not dry_run and (seen.get(src, vid) or {}).get("status") != before:
            seen.save()  # pending entry expired -> failed
            run.inc("youtube_failed")
        return
    if not dry_run and seen.get(src, vid) is not None:
        e0 = seen.get(src, vid)
        for k in ("published", "description"):
            if v.get(k) and not e0.get(k):
                e0[k] = v[k] if k == "published" else v[k][:2000]
    entry = seen.get(src, vid) or {}
    ingest_all_from = str(cfg.get("ingest_all_from") or "")
    # The RSS feed carries the newest uploads but often omits the date: unknown
    # date means "just published", not "older than any cutoff".
    published_or_new = v.get("published") or today()
    if ingest_all_from and published_or_new >= ingest_all_from:
        match = "yes"
    else:
        match = youtube.keyword_match(cfg, title, desc)
    if match == "no":
        log.info("skip (keywords) %s %s", vid, title)
        run.inc("youtube_skipped")
        if not dry_run:
            seen.mark(src, vid, SKIPPED, relevant=False, title=title)
        return
    if dry_run:
        print(f"  [youtube] {src} {v.get('published', '')} {vid} {title!r}: "
              f"{'relevant' if match == 'yes' else 'maybe (would ask LLM)'}"
              f"{' (pending retry)' if entry else ''}")
        run.inc("youtube_candidates")
        return
    if match == "maybe":
        rel_flag = entry.get("relevant")
        if rel_flag is None:
            rel_flag = classify.is_relevant({"title": title, "description": desc})
        if not rel_flag:
            log.info("skip (llm) %s %s", vid, title)
            run.inc("youtube_skipped")
            seen.mark(src, vid, SKIPPED, relevant=False, title=title)
            return "ok"  # a `claude -p` call was spent: counts against the per-run budget
        seen.mark(src, vid, entry.get("status") or "pending", relevant=True, title=title)

    published = v.get("published") or today()
    url = v.get("url") or f"https://www.youtube.com/watch?v={vid}"
    paths = _yt_paths(src)
    try:
        sub = youtube.fetch_subtitles(vid, paths["raw"])
        if not sub:
            e = seen.fail_attempt(src, vid, "no subtitles available yet", title=title, relevant=True)
            log.info("pending (no subs) %s %s -> %s", vid, title, e["status"])
            run.inc("youtube_pending" if e["status"] == "pending" else "youtube_failed")
            return "fail"
        text = clean.to_markdown_text(clean.clean_vtt(sub))
        if len(text.split()) < 40:
            e = seen.fail_attempt(src, vid, "transcript empty/too short", title=title, relevant=True)
            run.inc("youtube_pending" if e["status"] == "pending" else "youtube_failed")
            return "fail"
        tid = f"yt:{src}:{vid}"
        ingested = (entry.get("ingested") or now_iso())
        tdoc = Doc(id=tid, source=src, authority=authority, kind="transcript", date=published,
                   title=title, url=url, path="", meta={"video_id": vid, "ingested": ingested})
        tpath = clean.transcript_path(tdoc)
        tdoc.path = rel(tpath) if tpath.is_absolute() else str(tpath)
        meta = {"video_id": vid, "source": src, "title": title, "url": url, "date": published,
                "description": desc, "transcript_id": tid, "authority": authority, "ingested": ingested}
        d = classify.distill_video(meta, text)
        tdoc.tags, tdoc.summary = d["tags"], d["summary_1line"]
        from kb.frontmatter import doc_to_frontmatter

        atomic_write_text(R.root() / tdoc.path, render(doc_to_frontmatter(tdoc), f"# {title}\n\n{text}\n"))
        npath = paths["notes"] / f"{published}__{clean.slugify(title)}__{vid}.md"
        note_md = classify.render_note(meta, d)
        atomic_write_text(npath, note_md)
        fm, body = split(note_md)
        ndoc = Doc(id=fm["id"], source=src, authority=authority, kind="note", date=published, title=title,
                   url=url, path=rel(npath), tags=d["tags"], summary=d["summary_1line"], text=body,
                   meta={k: v2 for k, v2 in fm.items() if k not in ("id", "source", "authority", "kind", "date",
                                                                     "title", "url", "tags", "summary")})
        db.upsert(tdoc, con)
        db.upsert(ndoc, con)
        seen.mark(src, vid, DONE, relevant=d["relevant"], title=title, count_attempt=True, ingested=ingested)
        run.inc("youtube_new")
        log.info("ingested %s %s tags=%s", vid, title, ",".join(d["tags"]))
        return "ok"
    except Exception as e:  # noqa: BLE001  per-item isolation
        st = seen.fail_attempt(src, vid, f"{type(e).__name__}: {e}", title=title, relevant=True)
        run.err(f"youtube {src}/{vid}: {type(e).__name__}: {e} -> {st['status']}")
        log.debug(traceback.format_exc())
        return "fail"


def sync_youtube(cfg: dict, seen, con, run: Run, dry_run: bool, videos: list[dict] | None = None,
                 pending_only: bool = False) -> None:
    """Throttled: at most `max_videos_per_run` videos get real work per run
    (subtitle fetch + one `claude -p`), with a pause between them. The rest
    stay pending for the next run (kb-trickle.timer)."""
    from kb import throttle
    from kb.sources import youtube

    since = str(cfg.get("since") or "")
    if not dry_run:
        if run.proxy_ok is None:  # one cheap check per run, shared by all youtube sources
            run.proxy_ok = youtube.youtube_reachable(cfg)
            from kb.state import record_proxy

            down = record_proxy(run.proxy_ok)
            if not run.proxy_ok:
                log.warning("proxy_down: YouTube not reachable via proxy (consecutive runs down: %d) -- "
                            "skipping YouTube subtitle fetching this run, no attempts consumed", down)
                run.inc("youtube_proxy_down")
        if not run.proxy_ok:
            return
    if videos is None:
        videos = [] if pending_only else youtube.discover_rss(cfg)
        if not pending_only:
            log.info("youtube %s: rss returned %d videos", cfg["key"], len(videos))
            if not videos:
                log.warning("youtube %s: rss returned 0 videos (YouTube feed hiccup?) -- "
                            "new uploads are picked up by the next daily sync", cfg["key"])
        # also retry pending entries that dropped out of the RSS window (oldest first)
        rss_ids = {v["id"] for v in videos}
        pend = sorted((e.get("published") or "", vid, e) for s, vid, e in seen.items_with_status("pending")
                      if s == cfg["key"] and vid not in rss_ids)
        for _, vid, e in pend:
            videos.append({"id": vid, "title": e.get("title", ""), "published": e.get("published", ""),
                           "description": e.get("description", ""),
                           "url": f"https://www.youtube.com/watch?v={vid}"})
    budget = int(throttle.get("max_videos_per_run"))
    max_fail = int(throttle.get("stop_after_consecutive_failures"))
    worked = fails = 0
    for v in videos:
        if since and v.get("published") and v["published"] < since:
            continue
        if not dry_run and worked >= budget:
            log.info("youtube %s: per-run budget of %d videos reached, rest stays pending", cfg["key"], budget)
            run.inc("youtube_deferred")
            break
        if worked:
            throttle.pause("sleep_between_videos")
        outcome = process_video(cfg, v, seen, con, run, dry_run)
        if outcome is None:
            continue
        worked += 1
        fails = fails + 1 if outcome == "fail" else 0
        if max_fail and fails >= max_fail:
            log.warning("youtube %s: %d consecutive failures, stopping YouTube for this run", cfg["key"], fails)
            break


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

def sync_docs_source(cfg: dict, con, run: Run, dry_run: bool) -> None:
    from kb import classify, db
    from kb.frontmatter import render, split, write_doc
    from kb.sources import docs as docsrc
    from kb.state import atomic_write_text, load_doc_hashes, now_iso, save_doc_hashes

    key = cfg["key"]
    all_h = load_doc_hashes()
    old_h: dict[str, str] = all_h.get(key, {})
    first_run = not old_h
    if dry_run:
        pages = docsrc.list_pages(cfg)
        ids = {f"docs:{key}:{p['path']}" for p in pages}
        new = ids - set(old_h)
        gone = set(old_h) - ids
        print(f"  [docs] {key}: {len(pages)} pages listed; {len(new)} new, {len(gone)} removed"
              f"{' (first run: index only, no LLM)' if first_run else ''}; changed pages need a fetch to detect")
        for i in sorted(new)[:10]:
            print(f"    new: {i}")
        return

    docs_list, changed, removed, new_h = docsrc.sync_docs(cfg, old_h)
    # sync_docs reports pages whose fetch failed as "removed": verify against the listing
    if removed:
        listed = {f"docs:{key}:{p['path']}" for p in docsrc.list_pages(cfg)}
        really = [r for r in removed if r not in listed]
        for r in removed:
            if r in listed and r in old_h:
                new_h[r] = old_h[r]
                run.err(f"docs {key}: fetch failed for {r} (kept previous version)")
        removed = really
    by_id = {d.id: d for d in docs_list}
    entries: list[dict] = []
    llm_calls = 0
    ts = now_iso()
    for d in docs_list:
        try:
            d.date = today()
            d.meta = {**(d.meta or {}), "synced": ts}
            write_doc(d, R.root(), d.text)
            db.upsert(d, con)
            run.inc("docs_updated")
        except Exception as e:  # noqa: BLE001
            run.err(f"docs {d.id}: {type(e).__name__}: {e}")
    if not first_run:
        changed_ids = set()
        for c in changed:
            changed_ids.add(c["id"])
            d = by_id.get(c["id"])
            title = d.title if d else c["id"]
            old_body = split(c.get("old_text") or "")[1]
            if llm_calls < MAX_DOC_CHANGE_LLM_CALLS:
                summ = classify.summarize_doc_change(title, old_body, c.get("new_text") or "")
                llm_calls += 1
            else:
                summ = "- Page updated (summary skipped: too many changes in one run)."
            if "No textual change" in summ:
                continue
            entries.append({"id": c["id"], "title": title, "url": d.url if d else "", "summary": summ})
        for d in docs_list:
            if d.id not in changed_ids and d.id not in old_h:
                entries.append({"id": d.id, "title": d.title, "url": d.url, "summary": "- New page added."})
    for rid in removed:
        try:
            item = db.get_item(rid, con)
            db.delete(rid, con)
            if item and item.get("path"):
                p = R.root() / item["path"]
                if p.exists() and p.is_file():
                    p.unlink()
            entries.append({"id": rid, "title": item["title"] if item else rid, "url": item["url"] if item else "",
                            "summary": "- Page removed from the docs index.", "removed": True})
            run.inc("docs_removed")
        except Exception as e:  # noqa: BLE001
            run.err(f"docs remove {rid}: {type(e).__name__}: {e}")
    if entries:
        write_change_log(cfg, entries, con)
        run.inc("docs_changes", len(entries))
    all_h = load_doc_hashes()
    all_h[key] = new_h
    save_doc_hashes(all_h)
    log.info("docs %s: %d written, %d change entries, %d removed%s", key, len(docs_list), len(entries),
             len(removed), " (first run, no summaries)" if first_run else "")


def write_change_log(cfg: dict, entries: list[dict], con) -> None:
    from kb import db
    from kb.frontmatter import doc_from_file, write_doc

    key = cfg["key"]
    cid = f"docs:{key}:changes/{today()}"
    path = R.data() / "docs" / key / "changes" / f"{today()}.md"
    merged: dict[str, dict] = {}
    if path.exists():
        old = doc_from_file(path, R.root())
        for e in (old.meta.get("entries") if old else None) or []:
            merged[e["id"]] = e
    for e in entries:
        merged[e["id"]] = e
    es = list(merged.values())
    body = [f"# Docs changes: {key} ({today()})", ""]
    for e in es:
        body += [f"## {e['title']}", "", f"{e.get('url', '')} (`{e['id']}`)", "", e["summary"], ""]
    titles = ", ".join(e["title"] for e in es[:5]) + (f" +{len(es) - 5} more" if len(es) > 5 else "")
    d = Doc(id=cid, source=key, authority=cfg.get("authority", "canonical"), kind="change", date=today(),
            title=f"Docs changes {key} {today()}", url=cfg.get("llms_txt_url", ""), path=rel(path),
            summary=f"{len(es)} page(s): {titles}"[:300], meta={"entries": es})
    write_doc(d, R.root(), "\n".join(body))
    db.upsert(d, con)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def sync_github_releases(cfg: dict, con, run: Run, dry_run: bool) -> None:
    from kb import classify, db
    from kb.frontmatter import write_doc
    from kb.sources import github
    from kb.state import load_gh_seen, now_iso, save_gh_seen

    key = cfg["key"]
    gh = load_gh_seen()
    first = key not in gh
    seen_tags = set(gh.get(key, []))
    if dry_run:
        rels = github._gh_api_json(f"repos/{cfg['repo']}/releases") or []
        new = [r.get("tag_name") for r in rels if (r.get("tag_name") or "").lstrip("vV") not in
               {t.lstrip("vV") for t in seen_tags}]
        print(f"  [github] {key}: {len(rels)} recent releases, {len(new)} new"
              f"{' (first run: baseline, not in WHATS_NEW)' if first else ''}: {', '.join(map(str, new[:8]))}")
        return
    docs = github.sync_releases(cfg, seen_tags)
    ts = now_iso()
    for d in docs:
        try:
            d.summary = classify.summarize_release(d.title, d.text)
            if not d.date:
                d.date = "" if first else today()
            d.meta = {**(d.meta or {}), "ingested": ts, **({"baseline": True} if first else {})}
            write_doc(d, R.root(), f"# {d.title}\n\n{d.text}\n")
            db.upsert(d, con)
            seen_tags.add(d.id.split(":", 2)[2])
            run.inc("releases_new" if not first else "releases_baseline")
        except Exception as e:  # noqa: BLE001
            run.err(f"github {d.id}: {type(e).__name__}: {e}")
    gh = load_gh_seen()
    gh[key] = sorted(seen_tags)
    save_gh_seen(gh)
    log.info("github %s: %d new releases%s", key, len(docs), " (baseline)" if first else "")


def sync_github_repos(cfg: dict, con, run: Run, dry_run: bool) -> None:
    from kb import clean, db
    from kb.frontmatter import doc_from_file, write_doc
    from kb.sources import github
    from kb.state import load_gh_seen, save_gh_seen

    key = cfg["key"]
    if dry_run:
        print(f"  [github] {key}: repo listing refresh")
        return
    gh = load_gh_seen()
    first = key not in gh
    known = set(gh.get(key, []))
    docs = github.sync_repos(cfg)
    if not docs and known:
        run.err(f"github repos {key}: empty listing (API error?)", source_level=True)
        return
    for d in docs:
        try:
            path = R.data() / "github" / key / "repos" / f"{clean.slugify(d.title)}.md"
            old = doc_from_file(path, R.root()) if path.exists() else None
            first_seen = old.date if old else today()
            desc = (d.meta or {}).get("description", "")
            nd = Doc(id=d.id, source=key, authority=d.authority, kind="repo", date=first_seen, title=d.title,
                     url=d.url, path=rel(path),
                     summary=desc[:300], meta={"updated": d.date,
                                               **({"baseline": True} if (first or (old and old.meta.get("baseline"))) else {})})
            body = f"# {d.title}\n\n{desc}\n\n- URL: {d.url}\n- Last updated: {d.date}\n"
            if old is None or old.summary != nd.summary or old.meta.get("updated") != d.date:
                write_doc(nd, R.root(), body)
                nd.text = body
                db.upsert(nd, con)
            if d.title not in known:
                run.inc("repos_new")
            known.add(d.title)
        except Exception as e:  # noqa: BLE001
            run.err(f"github repo {d.id}: {type(e).__name__}: {e}")
    gh = load_gh_seen()
    gh[key] = sorted(known)
    save_gh_seen(gh)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_sync(args) -> int:
    from kb import db, index
    from kb.state import LockBusy, Seen, now_iso, save_last_run, sync_lock

    setup_logging(to_file=not args.dry_run)
    try:
        with sync_lock():
            run = Run()
            log.info("kb sync start (dry_run=%s, only=%s)", args.dry_run, args.only)
            seen = Seen(force_pending=args.force_pending)
            if args.pending_only and not seen.items_with_status("pending"):
                log.info("pending-only: nothing pending, exiting")
                return 0
            con = db.connect()
            try:
                for cfg in load_sources():
                    t = cfg.get("type")
                    group = {"youtube": "youtube", "docs": "docs", "github_releases": "github",
                             "github_repos": "github", "local_md": "local"}.get(t)
                    if args.only and group != args.only:
                        continue
                    if args.pending_only and t != "youtube":
                        continue
                    try:
                        if t == "youtube":
                            sync_youtube(cfg, seen, con, run, args.dry_run, pending_only=args.pending_only)
                        elif t == "docs":
                            sync_docs_source(cfg, con, run, args.dry_run)
                        elif t == "github_releases":
                            sync_github_releases(cfg, con, run, args.dry_run)
                        elif t == "github_repos":
                            sync_github_repos(cfg, con, run, args.dry_run)
                        elif t == "local_md":
                            log.info("local_md %s: local source, nothing to fetch (use `kb import-local`)",
                                     cfg.get("key"))
                        else:
                            log.warning("unknown source type %r for %s", t, cfg.get("key"))
                    except Exception as e:  # noqa: BLE001  per-source isolation
                        run.err(f"source {cfg.get('key')}: {type(e).__name__}: {e}", source_level=True)
                        log.debug(traceback.format_exc())
                if args.dry_run:
                    print(f"dry-run counts: {run.counts}")
                    return 0
                if date.today().weekday() == 6 and not args.pending_only:
                    weekly_maintenance(run)
                status = "ok" if not run.source_errors else "partial"
                save_last_run({"started": run.started, "finished": now_iso(), "status": status,
                               "counts": run.counts, "errors": run.errors[:50]})
            finally:
                con.close()
            try:
                index.generate_all()
            except Exception as e:  # noqa: BLE001
                run.err(f"index generation: {type(e).__name__}: {e}", source_level=True)
            if date.today().weekday() == 6 and not args.pending_only and not args.only:
                try:  # weekly digest for your review; reads only vault page names
                    from kb import vault

                    log.info("weekly vault candidates: %s", vault.generate(days=7))
                except Exception as e:  # noqa: BLE001
                    run.err(f"vault candidates: {type(e).__name__}: {e}")
            git_commit(run.counts)
            log.info("kb sync done: %s errors=%d", run.counts, len(run.errors))
            return 0
    except LockBusy as e:
        log.warning("skipping: %s", e)
        return 0


def weekly_maintenance(run: Run) -> None:
    from kb import db

    try:
        p = subprocess.run(["uv", "tool", "upgrade", "yt-dlp"], capture_output=True, text=True, timeout=300)
        log.info("weekly: uv tool upgrade yt-dlp rc=%s %s", p.returncode, (p.stdout + p.stderr).strip()[-200:])
    except Exception as e:  # noqa: BLE001
        log.warning("weekly yt-dlp upgrade failed: %s", e)
    from kb import throttle

    if not throttle.get("weekly_rebuild"):
        return  # incremental upserts keep the index current; full rebuild = `kb reindex` by hand
    try:
        log.info("weekly reindex: %s", db.rebuild())
    except Exception as e:  # noqa: BLE001
        run.err(f"weekly reindex: {type(e).__name__}: {e}")


def cmd_backfill(args) -> int:
    from kb import db, index
    from kb.sources import youtube
    from kb.state import LockBusy, Seen, sync_lock

    setup_logging(to_file=not args.dry_run)
    cfg = source_cfg(args.source)
    if cfg.get("type") != "youtube":
        raise SystemExit("backfill supports youtube sources only (docs/github mirror fully on every sync)")
    try:
        with sync_lock():
            run = Run()
            seen = Seen()
            vids = youtube.discover_backfill(cfg, args.since)
            vids = [v for v in vids if not v.get("published") or v["published"] >= args.since]
            vids.sort(key=lambda v: v.get("published") or "")
            todo = [v for v in vids if seen.should_process(cfg["key"], v["id"])]
            if args.discover_only:
                from kb.state import PENDING

                n = 0
                for v in todo:
                    if seen.get(cfg["key"], v["id"]) is None:
                        n += 1
                    seen.mark(cfg["key"], v["id"], (seen.get(cfg["key"], v["id"]) or {}).get("status") or PENDING,
                              title=v.get("title"), save=False, published=v.get("published") or None,
                              description=(v.get("description") or "")[:2000] or None)
                seen.save()
                log.info("backfill discover-only: %d listed, %d queued (%d new) for kb-trickle",
                         len(vids), len(todo), n)
                return 0
            if args.limit:
                todo = todo[: args.limit]
            log.info("backfill %s since %s: %d listed, %d to process", cfg["key"], args.since, len(vids), len(todo))
            con = db.connect()
            try:
                for v in todo:
                    process_video({**cfg, "since": args.since}, v, seen, con, run, args.dry_run)
            finally:
                con.close()
            if not args.dry_run:
                index.generate_all()
                git_commit(run.counts, label="kb backfill")
            log.info("backfill done: %s", run.counts)
            return 0
    except LockBusy as e:
        log.error("%s", e)
        return 75


def cmd_import_local(args) -> int:
    from kb.sources import local_md

    setup_logging(to_file=False)
    cfg = source_cfg(args.source)
    if cfg.get("type") != "local_md":
        print(f"{args.source} is not a local_md source", file=sys.stderr)
        return 2
    counts = local_md.import_local(cfg, Path(args.dir) if args.dir else None)
    print(f"import-local {args.source}: {counts} (run `kb reindex` to index)")
    return 0


def cmd_search(args) -> int:
    from kb import db

    rows = db.search(" ".join(args.query), source=args.source, authority=args.authority, kind=args.kind,
                     since=args.since, limit=args.limit)
    print(db.format_search(rows, " ".join(args.query)))
    return 0


def cmd_get(args) -> int:
    from kb import db

    res = db.get(args.id, locator=args.locator, around=args.around, chunk=args.chunk, max_chars=args.max_chars)
    print(db.format_get(res, args.id))
    return 0 if res else 1


def cmd_whats_new(args) -> int:
    from kb import index

    if args.compact:
        try:
            print(index.whats_new_compact(days=args.days))
        except Exception as e:  # noqa: BLE001  never break session start
            print(f"[AI KB] digest unavailable ({type(e).__name__})")
        return 0
    print(index.whats_new_text(days=args.days, topic=args.topic))
    return 0


def cmd_vault_candidates(args) -> int:
    from kb import vault

    print(vault.generate(days=args.days))
    print(R.index_dir() / "VAULT_CANDIDATES.md")
    return 0


def cmd_status(args) -> int:
    from kb.status import status_text

    print(status_text())
    return 0


def cmd_doctor(args) -> int:
    from kb.doctor import doctor_text

    print(doctor_text(check_network=not args.no_network))
    return 0


def cmd_reindex(args) -> int:
    from kb import db, index
    from kb.state import LockBusy, sync_lock

    setup_logging(to_file=False)
    try:
        with sync_lock():
            stats = db.rebuild()
            index.clean_generated()
            gen = index.generate_all()
            print(f"reindexed: {stats}; generated: {gen}")
            return 0
    except LockBusy as e:
        print(f"busy: {e}", file=sys.stderr)
        return 75


def cmd_mark_failure(args) -> int:
    from kb import index
    from kb.state import mark_failure

    setup_logging(to_file=True)
    lr = mark_failure(args.reason)
    log.error("sync marked FAILED: %s", args.reason)
    try:
        items = __import__("kb.db", fromlist=["all_items"]).all_items()
        index._write_whats_new(items)
    except Exception as e:  # noqa: BLE001
        log.error("could not regenerate WHATS_NEW: %s", e)
    print(f"last_run marked failed at {lr.get('failure_marked_at')}")
    return 0


def git_commit(counts: dict | None = None, label: str = "kb sync") -> bool:
    root = R.root()
    if not (root / ".git").exists():
        log.info("git-commit: no repo at %s, skipping", root)
        return False
    paths = [p for p in ("data", "index", "state") if (root / p).exists()]
    try:
        st = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", *paths],
                            capture_output=True, text=True, timeout=60)
        if st.returncode != 0 or not st.stdout.strip():
            log.info("git-commit: nothing to commit")
            return False
        # index/kb.sqlite and state/*.lock are gitignored
        subprocess.run(["git", "-C", str(root), "add", "-A", "--", *paths], check=True, timeout=120,
                       capture_output=True, text=True)
        c = counts or {}
        n_new = sum(v for k, v in c.items() if k in ("youtube_new", "releases_new", "docs_changes", "repos_new"))
        msg = f"{label} {datetime.now():%Y-%m-%d %H:%M}: {n_new} new"
        detail = ", ".join(f"{k}={v}" for k, v in sorted(c.items()))
        if detail:
            msg += f"\n\n{detail}"
        # Automated commits get their own identity (no global git identity on this VPS;
        # keeps bot commits distinguishable from yours).
        bot = {"GIT_AUTHOR_NAME": "kb-bot", "GIT_AUTHOR_EMAIL": "kb-bot@localhost",
               "GIT_COMMITTER_NAME": "kb-bot", "GIT_COMMITTER_EMAIL": "kb-bot@localhost"}
        r = subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg], capture_output=True, text=True,
                           timeout=120, env={**os.environ, **bot})
        if r.returncode != 0:
            log.warning("git commit failed: %s", (r.stderr or r.stdout).strip()[:300])
            return False
        log.info("git-commit: %s", msg.splitlines()[0])
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("git-commit error: %s", e)
        return False


def cmd_git_commit(args) -> int:
    setup_logging(to_file=False)
    git_commit()
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kb", description="AI Knowledge Feeds knowledge base")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="discover + ingest new items, regenerate indexes")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--only", choices=["youtube", "docs", "github"])
    s.add_argument("--pending-only", action="store_true",
                   help="only retry queued YouTube videos (throttled trickle; no RSS/docs/github)")
    s.add_argument("--force-pending", action="store_true",
                   help="ignore the retry backoff (next_try) of pending videos")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("backfill", help="ingest older videos of a YouTube source")
    s.add_argument("--source", required=True)
    s.add_argument("--since", required=True, help="YYYY-MM-DD")
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--discover-only", action="store_true",
                   help="only list the channel and queue videos as pending (processed later by kb-trickle)")
    s.set_defaults(func=cmd_backfill)

    s = sub.add_parser("import-local", help="(re)generate data/<key>/ from a local_md source's import_dir")
    s.add_argument("source")
    s.add_argument("--dir", help="override import_dir")
    s.set_defaults(func=cmd_import_local)
    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query", nargs="+")
    s.add_argument("--source")
    s.add_argument("--authority", choices=["canonical", "practice"])
    s.add_argument("--kind")
    s.add_argument("--since")
    s.add_argument("--limit", type=int, default=8)
    s.set_defaults(func=cmd_search)

    for name in ("get", "show"):
        s = sub.add_parser(name, help="print a fragment of one item")
        s.add_argument("id")
        s.add_argument("--locator")
        s.add_argument("--around", help="mm:ss (transcripts)")
        s.add_argument("--chunk", type=int)
        s.add_argument("--max-chars", type=int, default=4000)
        s.set_defaults(func=cmd_get)

    s = sub.add_parser("whats-new", help="recent changes digest")
    s.add_argument("--compact", action="store_true", help="<=1.5KB SessionStart digest")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--topic")
    s.set_defaults(func=cmd_whats_new)

    sub.add_parser("status").set_defaults(func=cmd_status)
    s = sub.add_parser("doctor", help="health check: proxy, pending backoff, failed, timers")
    s.add_argument("--no-network", action="store_true", help="skip the live proxy check")
    s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("vault-candidates", help="weekly digest of new items vs Vault wiki pages (read-only)")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(func=cmd_vault_candidates)
    sub.add_parser("reindex", help="rebuild SQLite + generated markdown from data/").set_defaults(func=cmd_reindex)

    s = sub.add_parser("mark-failure", help="(systemd OnFailure) flag last run as failed")
    s.add_argument("--reason", default="kb-sync.service failed (see journalctl --user -u kb-sync)")
    s.set_defaults(func=cmd_mark_failure)

    sub.add_parser("git-commit", help="(internal) commit data/index/state changes").set_defaults(func=cmd_git_commit)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        rc = 130
    except BrokenPipeError:
        rc = 0
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
