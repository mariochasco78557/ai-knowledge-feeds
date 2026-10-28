"""YAML frontmatter helpers shared by db/index/cli."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from kb.models import Doc

# Frontmatter keys that map to Doc fields; everything else goes to Doc.meta.
_DOC_KEYS = ("id", "source", "authority", "kind", "date", "title", "url", "tags", "summary")


def split(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). Missing/invalid frontmatter -> ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    # no line cap: docs change logs carry 1000+ frontmatter lines (entries list)
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            try:
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(fm, dict):
                return {}, text
            return fm, "\n".join(lines[i + 1:]).lstrip("\n")
    return {}, text


def render(fm: dict, body: str) -> str:
    clean = {k: v for k, v in fm.items() if v not in (None, "", [], {})}
    y = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=1000).strip()
    return f"---\n{y}\n---\n\n{body.lstrip()}"


def doc_to_frontmatter(doc: Doc) -> dict:
    fm: dict[str, Any] = {k: getattr(doc, k) for k in _DOC_KEYS}
    fm["date"] = str(doc.date or "")
    fm["tags"] = list(doc.tags or [])
    for k, v in (doc.meta or {}).items():
        if k not in fm:
            fm[k] = v
    return fm


def doc_from_file(path: Path, root: Path) -> Doc | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, body = split(text)
    if not fm.get("id"):
        return None
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    meta = {k: v for k, v in fm.items() if k not in _DOC_KEYS}
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = str(path)
    return Doc(
        id=str(fm["id"]),
        source=str(fm.get("source") or ""),
        authority=str(fm.get("authority") or "practice"),
        kind=str(fm.get("kind") or "doc"),
        date=str(fm.get("date") or ""),
        title=str(fm.get("title") or path.stem),
        url=str(fm.get("url") or ""),
        path=rel,
        tags=[str(t) for t in tags],
        summary=str(fm.get("summary") or ""),
        text=body,
        meta=meta,
    )


def write_doc(doc: Doc, root: Path, body: str | None = None) -> Path:
    """Write doc (frontmatter + body) to root/doc.path atomically."""
    from kb.state import atomic_write_text

    p = root / doc.path
    atomic_write_text(p, render(doc_to_frontmatter(doc), body if body is not None else doc.text))
    return p
