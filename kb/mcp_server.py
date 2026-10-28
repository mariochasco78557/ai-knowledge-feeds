"""stdio MCP server exposing the AI knowledge base (read-only).

Tools return compact plain text (no structured output) to keep token use low.
Register (user scope):
  claude mcp add --scope user ai-kb -- uv run --directory <path-to-repo> kb-mcp
"""
from __future__ import annotations

import logging
import sys

from kb import root as R  # noqa: F401  (applies AI_KB_ROOT override first)
from kb import db, index
from kb.status import status_text

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

try:
    from mcp_types import ToolAnnotations  # mcp >= 2
except ImportError:  # pragma: no cover
    try:
        from mcp.types import ToolAnnotations  # type: ignore
    except ImportError:
        ToolAnnotations = None  # type: ignore

INSTRUCTIONS = (
    "Local AI knowledge base (Claude Code, Codex, Claude API/Agent SDK, MCP, skills, hooks, subagents, "
    "n8n, AI automation): official Anthropic docs + GitHub changelogs (authority=canonical) and creator "
    "videos/notes (authority=practice). Protocol: kb_search first -> kb_get only the fragment you need "
    "(note or doc section before any transcript chunk; never read whole transcripts). Canonical beats "
    "practice on conflicts; cite id + date; flag practice knowledge older than ~3 months as possibly stale. "
    "When planning projects or making tooling decisions, start with kb_whats_new."
)

mcp = _Server("ai-kb", instructions=INSTRUCTIONS)

_RO = {"annotations": ToolAnnotations(readOnlyHint=True, openWorldHint=False)} if ToolAnnotations else {}


def _tool(description: str):
    kwargs = dict(description=description, **_RO)
    try:
        return mcp.tool(structured_output=False, **kwargs)
    except TypeError:  # older SDKs without structured_output/annotations
        return mcp.tool(description=description)


def _err(e: Exception) -> str:
    return f"kb error: {type(e).__name__}: {e}"


@_tool(
    "Search the AI knowledge base (always call this FIRST, before kb_get). Full-text (BM25) over "
    "official Anthropic docs, Claude Code/Codex release notes (canonical) and creator video notes/"
    "transcripts (practice). Returns up to `limit` compact hits: authority/kind, date, title, locator, "
    "id, chunk number and a short snippet. Canonical beats practice on conflicts. Filters: "
    "source (e.g. my-creator, anthropic-claude-code-docs, anthropic-platform-docs, claude-code, codex), "
    "authority (canonical|practice), kind (note|doc|transcript|release|change|repo), since "
    "(YYYY-MM-DD or 7d/4w/3m). Use short keyword queries (e.g. 'subagents tools permission')."
)
def kb_search(query: str, source: str | None = None, authority: str | None = None,
              kind: str | None = None, since: str | None = None, limit: int = 8) -> str:
    try:
        rows = db.search(query, source=source, authority=authority, kind=kind, since=since, limit=limit)
        return db.format_search(rows, query)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@_tool(
    "Fetch only a fragment of one knowledge-base item by id (from kb_search). Prefer the smallest "
    "fragment: chunk=N (from search hits), locator='Heading > Subheading' for docs/notes, or "
    "around='mm:ss' for a transcript moment. Without selectors returns the item up to ~4000 chars plus "
    "an outline of chunks. For videos read the distilled note (id ending ':note') before any transcript "
    "chunk. Canonical beats practice."
)
def kb_get(id: str, locator: str | None = None, around: str | None = None, chunk: int | None = None) -> str:
    try:
        res = db.get(id, locator=locator, around=around, chunk=chunk)
        return db.format_get(res, id)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@_tool(
    "What changed recently in the knowledge base: official docs page changes and releases "
    "(canonical) plus new creator videos (practice), newest first, one line each with ids. Call at "
    "the start of planning or tooling decisions, then kb_search / kb_get for details. "
    "days: lookback window (default 7). topic: optional taxonomy tag (see kb_topics), e.g. hooks, mcp."
)
def kb_whats_new(days: int = 7, topic: str | None = None) -> str:
    try:
        return index.whats_new_text(days=max(1, min(int(days), 90)), topic=topic)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@_tool(
    "List topic tags with item counts (use as `topic` for kb_whats_new or as search terms). "
    "Search first for specific questions; this is for orientation."
)
def kb_topics() -> str:
    try:
        tc = index.topic_counts()
        if not tc:
            return "no tagged items yet"
        return "topics (tag: items): " + ", ".join(f"{t}: {n}" for t, n in tc)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@_tool("Knowledge-base health: last sync time/status, item counts per source and kind, pending/failed videos.")
def kb_status() -> str:
    try:
        return status_text()
    except Exception as e:  # noqa: BLE001
        return _err(e)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
