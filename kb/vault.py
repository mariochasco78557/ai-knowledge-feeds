"""Weekly "Vault candidates" digest: index/VAULT_CANDIDATES.md.

the vault is a human-curated Markdown wiki (e.g. an Obsidian vault).
This KB is the raw, auto-updated layer. The digest cross-references what
arrived here in the last N days with Vault's existing page slugs so the owner
can decide, in his weekly review, what to promote through Vault's own
ingest workflow.

Hard boundary: Vault is READ-ONLY from here. Only page *filenames* are
read (no page contents), and nothing is ever written inside Vault.
Cheap by design: no LLM, no network, a single pass over the SQLite items.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kb import db
from kb import root as R
from kb.state import atomic_write_text

# Page groups worth enriching from AI news. clients/ (personal data) and
# sources/ (provenance records) are deliberately excluded.
GROUPS = ("concepts", "playbooks", "capabilities", "operational-intelligence", "prompts", "tools", "agency",
          "projects")
STOP = {"the", "and", "for", "with", "from", "into", "your", "that", "this", "how", "what", "first", "level",
        "system", "model", "decision", "workflow", "delivery", "prompts", "playbooks", "claude", "code", "using",
        "build", "built", "made", "make", "just", "every", "actually", "full", "course", "hour", "mins"}
# KB taxonomy tag -> Vault slugs it most naturally enriches.
TAG_TO_SLUGS = {
    "claude-code": ["claude-code-playbooks", "claude-md-strategy"],
    "subagents": ["sequential-subagent-delegation"],
    "context-engineering": ["context-engineering", "managing-context"],
    "skills": ["instruction-governance"],
    "hooks": ["instruction-governance"],
    "prompting": ["prompt-library", "claude-code-prompts"],
    "automation": ["ai-automation", "agentic-workflow-ladder"],
    "n8n": ["ai-automation"],
    "agents-sdk": ["agentic-workflow-ladder"],
    "workflow": ["explore-plan-code-commit"],
    "codex": ["codex-partner-prompts"],
    "clients": ["client-delivery-prompts"],
    "business": ["automation-strategy-prompts"],
    "pricing": ["automation-strategy-prompts"],
}
MAX_PER_PAGE = 6
MAX_UNMATCHED = 15


def vault_wiki() -> Path:
    env = os.environ.get("KB_VAULT_WIKI")
    return Path(env) if env else R.root().parent / "vault" / "wiki"


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) >= 4 and t not in STOP}


def load_pages(wiki: Path) -> dict[str, dict]:
    pages: dict[str, dict] = {}
    for group in GROUPS:
        base = wiki / group
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            pages[p.stem] = {"slug": p.stem, "group": group, "tokens": _tokens(p.stem)}
    return pages


def _recent_items(days: int) -> list[dict]:
    """New (non-legacy, non-baseline) notes by ingest time, doc changes and
    releases by date. Backfilled videos count when they were ingested."""
    cutoff_d = (date.today() - timedelta(days=days)).isoformat()
    cutoff_ts = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for it in db.all_items():
        meta = it.get("meta") or {}
        if meta.get("legacy") or meta.get("baseline") or it["kind"] not in ("note", "change", "release"):
            continue
        if it["kind"] == "note":
            ts = str(meta.get("ingested") or "")
            try:
                recent = datetime.fromisoformat(ts) >= cutoff_ts
            except ValueError:
                recent = bool(it["date"]) and it["date"] >= cutoff_d
        else:
            recent = bool(it["date"]) and it["date"] >= cutoff_d
        if recent:
            out.append(it)
    return out


def _match(it: dict, pages: dict[str, dict]) -> list[str]:
    scores: dict[str, int] = {}
    for tag in it.get("tags") or []:
        for slug in TAG_TO_SLUGS.get(tag, []):
            if slug in pages:
                scores[slug] = scores.get(slug, 0) + 2
    words = _tokens(f"{it.get('title', '')} {it.get('summary', '')}")
    for slug, pg in pages.items():
        hit = len(pg["tokens"] & words)
        if hit and (hit >= 2 or hit == len(pg["tokens"])):
            scores[slug] = scores.get(slug, 0) + 3 * hit
    return [s for s, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:2]]


def _line(it: dict) -> str:
    one = lambda s, n: (lambda t: t if len(t) <= n else t[: n - 1] + "…")(re.sub(r"\s+", " ", str(s or "")).strip())
    kind = {"note": "vídeo", "change": "docs", "release": "release"}[it["kind"]]
    return (f"- `{it['authority']}` {it['date']} {kind} · **{one(it['title'], 90)}** — {one(it['summary'], 160)} "
            f"(`{it['id']}`)")


def generate(days: int = 7) -> dict[str, int]:
    wiki = vault_wiki()
    pages = load_pages(wiki)
    items = sorted(_recent_items(days), key=lambda i: (i["authority"] != "canonical", i["date"] or ""),
                   reverse=False)
    by_page: dict[str, list[dict]] = {}
    unmatched: list[dict] = []
    for it in items:
        slugs = _match(it, pages) if pages else []
        if not slugs:
            if it["kind"] != "release":  # unmatched releases are noise
                unmatched.append(it)
            continue
        by_page.setdefault(slugs[0], []).append(it)

    today = date.today().isoformat()
    lines = [
        f"# Vault candidates — semana hasta {today}",
        "",
        f"Novedades de ai-kb de los últimos {days} días cruzadas con las páginas existentes de "
        "the vault (solo nombres de página; no se lee ni escribe nada dentro de Vault).",
        "Uso: revísalo en la revisión semanal y, lo que merezca la pena, incorpóralo con el flujo de "
        "ingesta del propio Vault. Detalle de cada ítem: `kb_get <id>` (MCP ai-kb).",
        "",
    ]
    if not pages:
        lines += [f"> Aviso: no se encontró la wiki de Vault en `{wiki}`.", ""]
    if not items:
        lines += ["Nada nuevo esta semana.", ""]
    for slug in sorted(by_page, key=lambda s: (-len(by_page[s]), s)):
        grp = by_page[slug]
        lines.append(f"## [[{slug}]] ({pages[slug]['group']}) — {len(grp)}")
        lines += [_line(i) for i in grp[:MAX_PER_PAGE]]
        if len(grp) > MAX_PER_PAGE:
            lines.append(f"- … {len(grp) - MAX_PER_PAGE} más")
        lines.append("")
    if unmatched:
        lines.append(f"## Sin página en Vault (¿concepto nuevo?) — {len(unmatched)}")
        lines += [_line(i) for i in unmatched[:MAX_UNMATCHED]]
        if len(unmatched) > MAX_UNMATCHED:
            lines.append(f"- … {len(unmatched) - MAX_UNMATCHED} más")
        lines.append("")
    atomic_write_text(R.index_dir() / "VAULT_CANDIDATES.md", "\n".join(lines).rstrip() + "\n")
    return {"items": len(items), "pages_matched": len(by_page), "unmatched": len(unmatched)}
