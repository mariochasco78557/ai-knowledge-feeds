"""Local markdown source (type: local_md). Never touches the network.

Import a folder of Markdown notes (your own notes, exports, or private
material you keep out of git); files are copied into data/<key>/ with proper
frontmatter by `kb import-local <key>`. `kb sync` only logs that the source
is local. Generated files are tracked in data/<key>/.manifest.json so a
re-run overwrites/removes generated files only.

Importer layout is selected by `importer:` in the source entry. Currently
`generic_md`: one Doc per .md file under import_dir, frontmatter keys
title, video_id, video_url, publish_date, kind.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from kb import root as R
from kb.clean import slugify
from kb.frontmatter import render, split
from kb.models import Doc

MANIFEST = ".manifest.json"
DEFAULT_DATE = "1970-01-01"  # used when a file has no date in its frontmatter


def _clean_body(text: str) -> str:
    fm, body = split(text)
    return body if fm else text


def _first_heading(body: str) -> str:
    for ln in body.splitlines():
        if ln.startswith("# "):
            return ln[2:].strip()
    return ""


def _summary(body: str, n: int = 300) -> str:
    for ln in body.splitlines():
        s = ln.strip()
        if len(s) > 50 and not s.startswith(("#", ">", "|", "-", "*", "[", "`")):
            return re.sub(r"\s+", " ", s)[:n]
    return ""


def _mk(cfg: dict, kind: str, slug: str, title: str, date: str, tags: list[str],
        summary: str, body: str, subdir: str, meta: dict | None = None) -> tuple[Doc, str]:
    key = cfg["key"]
    short = cfg.get("id_prefix", key)
    path = f"data/{key}/{subdir}/{slug}.md"
    m = {"private": bool(cfg.get("private", False))}
    m.update(meta or {})
    doc = Doc(id=f"{short}:{kind}:{slug}", source=key, authority=cfg.get("authority", "practice"),
              kind=kind, date=date, title=title, url=cfg.get("url", ""), path=path,
              tags=tags, summary=summary, text=body, meta=m)
    return doc, body


def collect_generic_md(cfg: dict, src: Path) -> list[tuple[Doc, str]]:
    """One kind=doc Doc per .md under src (recursive). Frontmatter keys used:
    title, publish_date|date, video_id, video_url, kind, source_file."""
    out: list[tuple[Doc, str]] = []
    base_tags = list(cfg.get("tags") or ["resource"])
    for p in sorted(src.rglob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        fm, body = split(p.read_text(encoding="utf-8"))
        rel = p.relative_to(src).with_suffix("")
        title = str(fm.get("title") or _first_heading(body) or p.stem)
        date = str(fm.get("publish_date") or fm.get("date") or DEFAULT_DATE)[:10]
        kind_tag = str(fm.get("kind") or "")
        tags = list(dict.fromkeys(t for t in base_tags + [kind_tag] + list(fm.get("tags") or []) if t))
        meta = {k: fm[k] for k in ("video_id", "video_url", "source_file") if fm.get(k)}
        doc, _ = _mk(cfg, "doc", slugify("-".join(rel.parts), 120), title, date, tags,
                     _summary(body), body, "docs", meta)
        if fm.get("video_url"):
            doc.url = str(fm["video_url"])
        out.append((doc, body))
    return out


def _frontmatter(doc: Doc) -> dict:
    from kb.frontmatter import doc_to_frontmatter

    fm = doc_to_frontmatter(doc)
    fm.pop("path", None)
    return fm


def import_local(cfg: dict, src: Path | None = None) -> dict[str, int]:
    """Idempotently (re)generate data/<key>/**.md; removes only files listed
    in the previous manifest that are no longer produced."""
    key = cfg["key"]
    imp = cfg.get("importer")
    if imp != "generic_md":
        raise ValueError(f"source {key}: unknown importer {imp!r}")
    src = Path(src or cfg["import_dir"]).expanduser()
    if not src.is_dir():
        raise FileNotFoundError(f"import_dir not found: {src}")
    items = collect_generic_md(cfg, src)
    root = R.root()
    base = R.data() / key
    mpath = base / MANIFEST
    old = set(json.loads(mpath.read_text())["files"]) if mpath.exists() else set()
    new: set[str] = set()
    counts: dict[str, int] = {}
    seen_ids: set[str] = set()
    for doc, body in items:
        if doc.id in seen_ids:
            raise ValueError(f"duplicate id {doc.id}")
        seen_ids.add(doc.id)
        p = root / doc.path
        text = render(_frontmatter(doc), body)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8")
        new.add(doc.path)
        counts[doc.kind] = counts.get(doc.kind, 0) + 1
    for rel in old - new:
        (root / rel).unlink(missing_ok=True)
    base.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps({"files": sorted(new)}, indent=1), encoding="utf-8")
    return counts
