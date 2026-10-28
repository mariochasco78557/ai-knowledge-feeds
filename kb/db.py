"""SQLite + FTS5 index at index/kb.sqlite (rebuildable from data/**/*.md).

Schema
  items(id PK, source, authority, kind, date, title, url, path, tags JSON,
        summary, parent_id, meta JSON)
  chunks  FTS5(item_id UNINDEXED, locator UNINDEXED, ord UNINDEXED,
               title, tags, text)  tokenize='porter unicode61'

Chunking
  transcript        ~350 words, sentence-aware; locator = nearest preceding
                    "[mm:ss]" marker, else "chunk N"
  doc/note/change   by markdown heading (fence-aware); locator = heading path
                    "A > B"; sections > 450 words split into ~400-word parts
                    ("A > B (2)"); tiny sections merged with following ones
  release/repo      single chunk when short, heading chunks otherwise

Ranking
  bm25 weights title 4, tags 3, text 1, then multiplied by a kind weight
  (note 1.5, doc 1.3, change 1.2, release 1.1, repo 0.9, transcript 0.8) and
  an authority weight (canonical 1.25). At most 2 chunks per item.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from kb import root as R
from kb.frontmatter import doc_from_file
from kb.models import Doc

SCHEMA_VERSION = 1

TRANSCRIPT_WORDS = 350
SECTION_MAX_WORDS = 450
SECTION_PART_WORDS = 400
SECTION_MIN_WORDS = 60

KIND_WEIGHT = {"note": 1.5, "doc": 1.3, "change": 1.2, "release": 1.1, "repo": 0.9, "transcript": 0.8}
CANONICAL_WEIGHT = 1.25

MARK_RE = re.compile(r"(\[\d{1,2}:\d{2}(?::\d{2})?\])")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "how", "do", "does",
    "i", "is", "are", "what", "when", "why", "can", "my", "me", "use", "using", "it", "be", "by",
    "vs", "about", "from", "that", "this", "which", "should", "into", "at", "as", "we", "you",
}


# ---------------------------------------------------------------------------
# connection / schema
# ---------------------------------------------------------------------------

def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or R.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30)
    con.row_factory = sqlite3.Row
    if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    if ver not in (0, SCHEMA_VERSION):
        con.execute("DROP TABLE IF EXISTS chunks")
        con.execute("DROP TABLE IF EXISTS items")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS items(
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            authority TEXT NOT NULL,
            kind TEXT NOT NULL,
            date TEXT,
            title TEXT,
            url TEXT,
            path TEXT,
            tags TEXT DEFAULT '[]',
            summary TEXT DEFAULT '',
            parent_id TEXT,
            meta TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS items_date ON items(date);
        CREATE INDEX IF NOT EXISTS items_source ON items(source, kind);
        CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            item_id UNINDEXED, locator UNINDEXED, ord UNINDEXED,
            title, tags, text,
            tokenize='porter unicode61'
        );
        """
    )
    if ver != SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------

def _wc(s: str) -> int:
    return len(s.split())


def chunk_transcript(text: str, target: int = TRANSCRIPT_WORDS) -> list[tuple[str, str]]:
    """Split transcript into ~target-word chunks. Locator = nearest preceding
    [mm:ss] marker (the marker in force at the chunk start)."""
    pieces = MARK_RE.split(text)
    chunks: list[tuple[str | None, list[str]]] = []
    cur: list[str] = []
    cur_loc: str | None = None
    last_marker: str | None = None
    for piece in pieces:
        if not piece:
            continue
        if MARK_RE.fullmatch(piece):
            last_marker = piece
            if not cur:
                cur_loc = piece
            cur.append(piece)
            continue
        for w in piece.split():
            if not cur:
                cur_loc = last_marker
            cur.append(w)
            n = len(cur)
            if (n >= target and w[-1:] in ".?!") or n >= int(target * 1.4):
                chunks.append((cur_loc, cur))
                cur = []
    if cur:
        # avoid a tiny trailing chunk
        if chunks and len(cur) < target // 4:
            loc, words = chunks[-1]
            chunks[-1] = (loc, words + cur)
        else:
            chunks.append((cur_loc, cur))
    out = []
    for i, (loc, words) in enumerate(chunks, 1):
        out.append((loc or f"chunk {i}", " ".join(words)))
    return out


def _md_sections(text: str) -> list[tuple[str, str]]:
    """[(heading_path, section_text_including_heading_line)]"""
    sections: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    cur_path = ""
    cur: list[str] = []
    in_fence = False
    seen_content = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEAD_RE.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if level == 1 and not seen_content and not stack:
                # document title heading: not part of locator paths
                cur.append(line)
                continue
            if any(l.strip() for l in cur):
                sections.append((cur_path, cur))
            cur = [line]
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_path = " > ".join(t for _, t in stack)
            seen_content = True
            continue
        if line.strip():
            seen_content = True
        cur.append(line)
    if any(l.strip() for l in cur):
        sections.append((cur_path, cur))
    return [(p or "intro", "\n".join(ls).strip()) for p, ls in sections]


def _split_words(text: str, size: int) -> list[str]:
    paras = re.split(r"\n\s*\n", text)
    parts: list[str] = []
    cur: list[str] = []
    cur_n = 0
    for para in paras:
        n = _wc(para)
        if cur and cur_n + n > size:
            parts.append("\n\n".join(cur))
            cur, cur_n = [], 0
        if n > size * 1.5:  # giant paragraph: hard split by words
            words = para.split()
            for i in range(0, len(words), size):
                parts.append(" ".join(words[i:i + size]))
            continue
        cur.append(para)
        cur_n += n
    if cur:
        parts.append("\n\n".join(cur))
    return parts


def chunk_markdown(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pend_loc: str | None = None
    pend: list[str] = []
    for path, body in _md_sections(text):
        n = _wc(body)
        if n > SECTION_MAX_WORDS:
            if pend:
                out.append((pend_loc or path, "\n\n".join(pend)))
                pend, pend_loc = [], None
            for i, part in enumerate(_split_words(body, SECTION_PART_WORDS), 1):
                out.append((path if i == 1 else f"{path} ({i})", part))
            continue
        if not pend:
            pend_loc = path
        pend.append(body)
        if sum(_wc(b) for b in pend) >= SECTION_MIN_WORDS:
            out.append((pend_loc or path, "\n\n".join(pend)))
            pend, pend_loc = [], None
    if pend:
        if out and sum(_wc(b) for b in pend) < SECTION_MIN_WORDS // 2 and \
                _wc(out[-1][1]) + sum(_wc(b) for b in pend) <= SECTION_MAX_WORDS:
            loc, t = out[-1]
            out[-1] = (loc, t + "\n\n" + "\n\n".join(pend))
        else:
            out.append((pend_loc or "intro", "\n\n".join(pend)))
    return out


def chunk_doc(doc: Doc, text: str) -> list[tuple[str, str]]:
    if not text.strip():
        return [("summary", doc.summary or doc.title)]
    if doc.kind == "transcript":
        return chunk_transcript(text)
    if doc.kind in ("release", "repo") and _wc(text) <= SECTION_MAX_WORDS:
        return [(doc.kind, text.strip())]
    return chunk_markdown(text)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def _body_for(doc: Doc) -> str:
    if doc.text:
        return doc.text
    p = R.root() / doc.path if doc.path else None
    if p and p.exists():
        d = doc_from_file(p, R.root())
        if d:
            return d.text
    return ""


def upsert(doc: Doc, con: sqlite3.Connection | None = None) -> int:
    own = con is None
    con = con or connect()
    try:
        text = _body_for(doc)
        chunks = chunk_doc(doc, text)
        parent = (doc.meta or {}).get("parent") or (doc.meta or {}).get("parent_id")
        meta = {k: v for k, v in (doc.meta or {}).items() if k not in ("parent", "parent_id")}
        with con:
            con.execute("DELETE FROM chunks WHERE item_id = ?", (doc.id,))
            con.execute(
                "INSERT OR REPLACE INTO items(id, source, authority, kind, date, title, url, path,"
                " tags, summary, parent_id, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc.id, doc.source, doc.authority, doc.kind, str(doc.date or ""), doc.title,
                 doc.url, doc.path, json.dumps(list(doc.tags or [])), doc.summary or "",
                 parent, json.dumps(meta, default=str)),
            )
            tags_txt = " ".join(doc.tags or [])
            if doc.summary:
                tags_txt = f"{tags_txt} {doc.summary}".strip()
            for i, (loc, ctext) in enumerate(chunks, 1):
                title = doc.title if loc.startswith(("[", "chunk ")) or loc in (doc.kind, "intro", "summary") \
                    else f"{doc.title} — {loc}"
                # summary/tags only on the first chunk so they don't inflate every chunk
                con.execute(
                    "INSERT INTO chunks(item_id, locator, ord, title, tags, text) VALUES (?,?,?,?,?,?)",
                    (doc.id, loc, i, title, tags_txt if i == 1 else " ".join(doc.tags or []), ctext),
                )
        return len(chunks)
    finally:
        if own:
            con.close()


def delete(item_id: str, con: sqlite3.Connection | None = None) -> None:
    own = con is None
    con = con or connect()
    try:
        with con:
            con.execute("DELETE FROM chunks WHERE item_id = ?", (item_id,))
            con.execute("DELETE FROM items WHERE id = ?", (item_id,))
    finally:
        if own:
            con.close()


def iter_data_docs(data_dir: Path | None = None) -> Iterable[Doc]:
    base = data_dir or R.data()
    for p in sorted(base.rglob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        d = doc_from_file(p, R.root())
        if d:
            yield d


def rebuild() -> dict[str, int]:
    """Drop and rebuild the whole index from data/**/*.md frontmatter."""
    con = connect()
    try:
        with con:
            con.execute("DROP TABLE IF EXISTS chunks")
            con.execute("DROP TABLE IF EXISTS items")
        ensure_schema(con)
        n_items = n_chunks = 0
        for d in iter_data_docs():
            n_chunks += upsert(d, con)
            n_items += 1
        with con:
            con.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
        return {"items": n_items, "chunks": n_chunks}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def normalize_since(since: str | None) -> str | None:
    if not since:
        return None
    s = since.strip()
    m = re.fullmatch(r"(\d+)\s*([dwm])", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        days = n * {"d": 1, "w": 7, "m": 30}[u]
        return (date.today() - timedelta(days=days)).isoformat()
    return s[:10]


def build_queries(query: str) -> tuple[str | None, str | None]:
    """Turn free text into safe FTS5 MATCH strings: (AND query, OR query).
    Quoted phrases are kept; every other token is quoted so FTS5 operators
    and punctuation in user input can never raise syntax errors."""
    phrases = [p.strip() for p in re.findall(r'"([^"]+)"', query) if p.strip()]
    rest = re.sub(r'"[^"]*"', " ", query)
    tokens = re.findall(r"[\w][\w\-\.\+/']*", rest, flags=re.UNICODE)
    tokens = [t.strip(".-'/+") for t in tokens]
    tokens = [t for t in tokens if t]
    content = [t for t in tokens if t.lower() not in STOPWORDS]
    if content:
        tokens = content
    terms = [f'"{p.replace(chr(34), "")}"' for p in phrases] + [f'"{t}"' for t in tokens]
    # dedupe preserving order
    seen: set[str] = set()
    terms = [t for t in terms if not (t.lower() in seen or seen.add(t.lower()))]
    if not terms:
        return None, None
    and_q = " AND ".join(terms)
    or_q = " OR ".join(terms) if len(terms) > 1 else None
    return and_q, or_q


def _run_search(con, match: str, source, authority, kind, since, cand: int) -> list[sqlite3.Row]:
    where = ["chunks MATCH ?"]
    args: list[Any] = [match]
    if source:
        where.append("i.source = ?")
        args.append(source)
    if authority:
        where.append("i.authority = ?")
        args.append(authority)
    if kind:
        where.append("i.kind = ?")
        args.append(kind)
    if since:
        where.append("i.date >= ?")
        args.append(since)
    sql = (
        "SELECT chunks.item_id AS item_id, chunks.locator AS locator, chunks.ord AS ord,"
        " i.title AS title, i.date AS date, i.source AS source, i.authority AS authority,"
        " i.kind AS kind, i.parent_id AS parent_id, i.summary AS summary,"
        " snippet(chunks, 5, '', '', '…', 18) AS snip,"
        " bm25(chunks, 0.0, 0.0, 0.0, 4.0, 3.0, 1.0) AS score"
        " FROM chunks JOIN items i ON i.id = chunks.item_id"
        f" WHERE {' AND '.join(where)} ORDER BY score LIMIT ?"
    )
    args.append(cand)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def search(query: str, source: str | None = None, authority: str | None = None,
           kind: str | None = None, since: str | None = None, limit: int = 8,
           con: sqlite3.Connection | None = None) -> list[dict]:
    own = con is None
    con = con or connect()
    try:
        limit = max(1, min(int(limit or 8), 25))
        since_n = normalize_since(since)
        and_q, or_q = build_queries(query)
        if not and_q:
            return []
        cand = max(60, limit * 8)
        rows = _run_search(con, and_q, source, authority, kind, since_n, cand)
        mode = "and"
        if not rows and or_q:
            rows = _run_search(con, or_q, source, authority, kind, since_n, cand)
            mode = "or"
        scored = []
        for r in rows:
            w = KIND_WEIGHT.get(r["kind"], 1.0) * (CANONICAL_WEIGHT if r["authority"] == "canonical" else 1.0)
            scored.append((r["score"] * w, r))  # bm25 is negative: larger |x| = better
        scored.sort(key=lambda x: x[0])
        per_item: dict[str, int] = {}
        out: list[dict] = []
        for s, r in scored:
            if per_item.get(r["item_id"], 0) >= 2:
                continue
            per_item[r["item_id"]] = per_item.get(r["item_id"], 0) + 1
            snip = re.sub(r"https?://\S+", "", r["snip"] or "")
            snip = re.sub(r"(^|\s)(#{1,6}|-|\*\*)(?=\s)", " ", snip)
            snip = re.sub(r"\s+", " ", snip).strip()
            out.append({
                "id": r["item_id"], "title": r["title"], "date": r["date"], "source": r["source"],
                "authority": r["authority"], "kind": r["kind"], "locator": r["locator"],
                "chunk": int(r["ord"]), "snippet": snip, "score": round(-s, 3),
                "parent_id": r["parent_id"], "match": mode,
            })
            if len(out) >= limit:
                break
        # pointer from transcript hits to their note (notes are the cheap read)
        tids = [o["id"] for o in out if o["kind"] == "transcript"]
        if tids:
            q = "SELECT parent_id, id FROM items WHERE kind='note' AND parent_id IN (%s)" % ",".join("?" * len(tids))
            notes = {a: b for a, b in con.execute(q, tids).fetchall()}
            for o in out:
                if o["kind"] == "transcript" and o["id"] in notes:
                    o["note_id"] = notes[o["id"]]
        return out
    finally:
        if own:
            con.close()


def format_search(rows: list[dict], query: str = "") -> str:
    if not rows:
        return f"No results for {query!r}. Try fewer/different terms or drop filters."
    lines = []
    if rows and rows[0].get("match") == "or":
        lines.append("(no chunk matched all terms; showing any-term matches)")
    shown: set[str] = set()
    for i, r in enumerate(rows, 1):
        loc = r.get("locator") or ""
        loc = f" @ {loc[:60]}" if loc and loc not in (r["kind"], "intro", "summary") and not loc.startswith("chunk ") else ""
        extra = f" note={r['note_id']}" if r.get("note_id") else ""
        title = r["title"] if len(r["title"]) <= 70 else r["title"][:69] + "…"
        if r["id"] in shown:
            lines.append(f"{i}. (same item {r['id']}) chunk={r['chunk']}{loc}")
            lines.append(f"   {r['snippet']}")
            continue
        shown.add(r["id"])
        lines.append(f"{i}. [{r['authority']}/{r['kind']}] {r['date']} {title}{loc} | {r['id']} chunk={r['chunk']}{extra}")
        lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def _secs(t: str) -> int | None:
    t = t.strip().strip("[]")
    parts = t.split(":")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 3:
        return None
    s = 0
    for p in parts:
        s = s * 60 + int(p)
    return s


def get_item(item_id: str, con: sqlite3.Connection | None = None) -> dict | None:
    own = con is None
    con = con or connect()
    try:
        r = con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d
    finally:
        if own:
            con.close()


def get(item_id: str, locator: str | None = None, around: str | None = None,
        chunk: int | None = None, max_chars: int = 4000,
        con: sqlite3.Connection | None = None) -> dict | None:
    own = con is None
    con = con or connect()
    try:
        item = get_item(item_id, con)
        if not item:
            # tolerate the video id without the ":note" suffix and vice versa
            return None
        rows = [dict(r) for r in con.execute(
            "SELECT locator, CAST(ord AS INTEGER) AS ord, text FROM chunks WHERE item_id = ? ORDER BY CAST(ord AS INTEGER)",
            (item_id,)).fetchall()]
        total = len(rows)
        selected: list[dict] = []
        hint = None
        mode = "full"
        if chunk is not None:
            mode = "chunk"
            selected = [r for r in rows if r["ord"] == int(chunk)]
        elif around:
            mode = "around"
            t = _secs(around)
            if t is not None:
                best = None
                for r in rows:
                    marks = [_secs(m) for m in MARK_RE.findall(r["text"])]
                    start = _secs(r["locator"]) if r["locator"].startswith("[") else None
                    if start is None and marks:
                        start = marks[0]
                    if start is not None and start <= t:
                        best = r
                selected = [best or rows[0]] if rows else []
                if rows and not any(MARK_RE.search(r["text"]) for r in rows):
                    hint = "this transcript has no [mm:ss] markers (legacy import); around= is approximate, use chunk=N"
        elif locator:
            mode = "locator"
            lo = locator.strip().lower()
            exact = [r for r in rows if r["locator"].lower() == lo]
            if exact:
                idx = rows.index(exact[0])
                selected = [rows[idx]]
                # include following sub-sections / continuation parts of the same section
                for r in rows[idx + 1:]:
                    if r["locator"].lower().startswith(lo):
                        selected.append(r)
                    else:
                        break
            else:
                selected = [r for r in rows if lo in r["locator"].lower()]
            if not selected:
                # small sections are merged into neighbouring chunks: look for the heading line
                leaf = re.escape(lo.split(">")[-1].strip())
                hre = re.compile(rf"^#{{1,6}}\s+{leaf}\s*#*\s*$", re.I | re.M)
                selected = [r for r in rows if hre.search(r["text"])][:1] or \
                    [r for r in rows if lo.split(">")[-1].strip() in r["text"].lower()][:1]
        else:
            selected = rows
        text_parts: list[str] = []
        used = 0
        returned: list[int] = []
        truncated = False
        for r in selected:
            piece = r["text"]
            if used + len(piece) > max_chars:
                if not text_parts:
                    text_parts.append(piece[:max_chars])
                    returned.append(r["ord"])
                truncated = True
                break
            text_parts.append(piece)
            returned.append(r["ord"])
            used += len(piece) + 2
        outline = None
        if truncated or (mode != "full" and total > len(returned)):
            outline = [(r["ord"], r["locator"]) for r in rows]
        note_id = None
        if item["kind"] == "transcript":
            nr = con.execute("SELECT id FROM items WHERE kind='note' AND parent_id = ?", (item_id,)).fetchone()
            note_id = nr[0] if nr else None
        return {"item": item, "mode": mode, "text": "\n\n".join(text_parts), "chunks": returned,
                "total_chunks": total, "truncated": truncated, "outline": outline,
                "not_found": bool((chunk is not None or locator or around) and not returned),
                "note_id": note_id, "hint": hint}
    finally:
        if own:
            con.close()


def format_get(res: dict | None, item_id: str = "") -> str:
    if res is None:
        return f"Unknown id {item_id!r}. Use kb_search to find ids."
    it = res["item"]
    head = [f"{it['title']} [{it['authority']}/{it['kind']}] {it['date']} source={it['source']}",
            f"id: {it['id']} | url: {it['url']}"]
    if it.get("tags"):
        head.append("tags: " + ", ".join(it["tags"]))
    if res.get("note_id"):
        head.append(f"distilled note (read first): {res['note_id']}")
    if res["chunks"]:
        rng = ",".join(str(c) for c in res["chunks"][:12])
        nav = ""
        if res["mode"] in ("chunk", "around", "locator"):
            lo, hi = min(res["chunks"]), max(res["chunks"])
            nav = " | " + " ".join(x for x in (f"prev chunk={lo - 1}" if lo > 1 else "",
                                                f"next chunk={hi + 1}" if hi < res["total_chunks"] else "") if x)
        head.append(f"chunks {rng} of {res['total_chunks']}" + (" (truncated)" if res["truncated"] else "")
                    + (nav if nav.strip(" |") else ""))
    if res.get("hint"):
        head.append(f"note: {res['hint']}")
    if res["not_found"]:
        head.append("requested fragment not found; outline below")
    out = "\n".join(head) + "\n---\n" + res["text"]
    if res.get("outline") and (res["truncated"] or res["not_found"]):
        ol = res["outline"]
        lines = [f"  chunk={o} {l}" for o, l in ol[:40]]
        if len(ol) > 40:
            lines.append(f"  … {len(ol) - 40} more")
        out += "\n---\noutline (use chunk=N or locator=...):\n" + "\n".join(lines)
    return out


# ---------------------------------------------------------------------------
# misc queries
# ---------------------------------------------------------------------------

def all_items(con: sqlite3.Connection | None = None) -> list[dict]:
    own = con is None
    con = con or connect()
    try:
        out = []
        for r in con.execute("SELECT * FROM items ORDER BY date DESC, id"):
            d = dict(r)
            d["tags"] = json.loads(d.get("tags") or "[]")
            d["meta"] = json.loads(d.get("meta") or "{}")
            out.append(d)
        return out
    finally:
        if own:
            con.close()


def counts(con: sqlite3.Connection | None = None) -> list[tuple[str, str, int]]:
    own = con is None
    con = con or connect()
    try:
        return [tuple(r) for r in con.execute(
            "SELECT source, kind, COUNT(*) FROM items GROUP BY source, kind ORDER BY source, kind")]
    finally:
        if own:
            con.close()
