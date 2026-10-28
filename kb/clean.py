"""Transcript cleaning: VTT/SRT -> deduplicated timestamped segments -> markdown.

Implements rolling-caption de-duplication) but keeps per-segment timestamps so we can
insert [mm:ss] markers, and unescapes HTML entities.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import yaml

from kb.models import Doc
from kb.paths import DATA

# --- timestamp / cue parsing -------------------------------------------------

_VTT_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}.*"
)
_SRT_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}.*"
)
_TAG = re.compile(r"<[^>]+>")  # <c>, <00:00:00.000> inline cue tags
_META = re.compile(r"^(WEBVTT|Kind:|Language:|NOTE).*", re.I)


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def clean_vtt(path: str | Path) -> list[tuple[float, str]]:
    """Parse a VTT/SRT subtitle file into deduplicated (seconds, text) segments.

    Mirrors the rolling-caption de-duplication of clean_transcripts.py
    (skip a line equal to, or fully contained in, the previously kept
    line) but keeps the start timestamp of each kept line and
    html.unescape()s entities.
    """
    raw_text = html.unescape(Path(path).read_text(encoding="utf-8", errors="ignore"))

    segments: list[tuple[float, str]] = []
    current_ts = 0.0
    last_kept: str | None = None

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.isdigit():
            continue

        m = _VTT_TS.match(line)
        if m:
            current_ts = _ts_to_seconds(*m.groups())
            continue
        m = _SRT_TS.match(line)
        if m:
            current_ts = _ts_to_seconds(*m.groups())
            continue
        if _META.match(line):
            continue

        line = _TAG.sub("", line).strip()
        if not line:
            continue

        # collapse the rolling-caption repetition typical of YouTube auto-subs
        if last_kept and (line == last_kept or last_kept.endswith(line)):
            continue

        segments.append((current_ts, line))
        last_kept = line

    # second pass: drop a segment whose text is fully contained in the
    # previous kept segment's text
    deduped: list[tuple[float, str]] = []
    for ts, line in segments:
        if deduped and line in deduped[-1][1]:
            continue
        deduped.append((ts, line))
    return deduped


def to_markdown_text(segments: list[tuple[float, str]], marker_every: float = 120.0) -> str:
    """Join segments into prose, inserting a `[mm:ss]` marker roughly every
    `marker_every` seconds (and always at the start)."""
    if not segments:
        return ""
    parts: list[str] = []
    next_marker = 0.0
    for ts, text in segments:
        if ts >= next_marker:
            mm, ss = int(ts) // 60, int(ts) % 60
            parts.append(f"\n\n[{mm:02d}:{ss:02d}] ")
            next_marker = ts + marker_every
        parts.append(text + " ")
    return "".join(parts).strip()


# --- slug / filenames --------------------------------------------------------

def slugify(title: str, maxlen: int = 90) -> str:
    """Human-readable filesystem-safe slug (same rules as clean_transcripts.py):
    lowercase, '&' -> 'and', '%' -> 'percent', drop punctuation/emoji,
    collapse whitespace/underscores to '-'."""
    s = (title or "").lower().replace("&", " and ").replace("%", " percent ")
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s.strip())
    s = re.sub(r"-+", "-", s).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0].strip("-")
    return s or "untitled"


def transcript_path(doc: Doc) -> Path:
    """data/youtube/<source>/transcripts/<date>__<slug>__<videoId>.md"""
    video_id = doc.id.rsplit(":", 1)[-1]
    date = doc.date or "unknown"
    fname = f"{date}__{slugify(doc.title)}__{video_id}.md"
    return DATA / "youtube" / doc.source / "transcripts" / fname


def write_transcript(doc: Doc, text: str) -> Path:
    """Write `text` as a markdown file with YAML frontmatter for `doc`.
    Returns the absolute path written. Also sets doc.path (ROOT-relative)
    and doc.text."""
    out_path = transcript_path(doc)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "id": doc.id,
        "source": doc.source,
        "authority": doc.authority,
        "kind": doc.kind,
        "date": doc.date,
        "title": doc.title,
        "url": doc.url,
        "tags": doc.tags,
    }
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()

    body = f"---\n{fm_yaml}\n---\n\n# {doc.title}\n\n{text}\n"
    out_path.write_text(body, encoding="utf-8")

    from kb.paths import ROOT

    doc.path = str(out_path.relative_to(ROOT))
    doc.text = text
    return out_path
