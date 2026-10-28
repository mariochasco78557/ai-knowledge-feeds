"""Anthropic docs mirror, driven by a published llms.txt index.

`cfg` is the source's entry from sources.yaml, e.g.:
    key: anthropic-claude-code-docs
    type: docs
    authority: canonical
    llms_txt_url: https://code.claude.com/docs/llms.txt
    include_patterns: [...]   # optional, regex list applied to the page URL
    exclude_patterns: [...]   # optional, regex list applied to the page URL
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from kb import throttle
from kb.models import Doc
from kb.paths import DATA, ROOT

_log = logging.getLogger("kb.sources.docs")

# "- [Title](https://.../page.md): optional description"
_LINK_RE = re.compile(r"^-\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)\s]+)\)")

_RETRIES = 3
_TIMEOUT = 20.0


def _page_path(url: str) -> str:
    """URL -> path under data/docs/<source>/pages/, e.g.
    https://code.claude.com/docs/en/overview.md -> en/overview.md
    https://platform.claude.com/docs/en/api/messages.md -> en/api/messages.md
    Falls back to the full URL path (minus leading '/') if there's no
    '/docs/' segment.
    """
    parsed = urlparse(url)
    path = parsed.path
    marker = "/docs/"
    if marker in path:
        path = path.split(marker, 1)[1]
    else:
        path = path.lstrip("/")
    if not path.endswith(".md"):
        path = path.rstrip("/") + ".md"
    return path


def list_pages(cfg: dict) -> list[dict]:
    """Fetch and parse the source's llms.txt index.

    Returns dicts: url, title, path (relative, under .../pages/).
    Applies include_patterns / exclude_patterns (regex, matched against the
    URL) from cfg when present.
    """
    resp = httpx.get(cfg["llms_txt_url"], follow_redirects=True, timeout=_TIMEOUT)
    resp.raise_for_status()

    include = [re.compile(p) for p in cfg.get("include_patterns", [])]
    exclude = [re.compile(p) for p in cfg.get("exclude_patterns", [])]

    pages: list[dict] = []
    seen_urls: set[str] = set()
    for line in resp.text.splitlines():
        m = _LINK_RE.match(line.strip())
        if not m:
            continue
        url = m.group("url")
        if not url.endswith(".md"):
            continue  # skip non-markdown links (e.g. the console root URL)
        if url in seen_urls:
            continue
        if include and not any(p.search(url) for p in include):
            continue
        if exclude and any(p.search(url) for p in exclude):
            continue
        seen_urls.add(url)
        pages.append(
            {
                "url": url,
                "title": m.group("title").strip(),
                "path": _page_path(url),
            }
        )
    return pages


def fetch_page(url: str) -> str:
    """Fetch one markdown page, with a few retries."""
    last_exc: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:  # pragma: no cover
            last_exc = exc
    raise RuntimeError(f"failed to fetch {url} after {_RETRIES} attempts: {last_exc}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_page(cfg_key: str, page: dict, text: str, fetch_date: str) -> Path:
    """Write one page's markdown + YAML frontmatter atomically (write to a
    temp file in the same directory, then os.replace) so a crash mid-write
    never leaves a truncated/corrupt page behind.
    """
    out_path = DATA / "docs" / cfg_key / "pages" / page["path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter_dict = {
        "id": f"docs:{cfg_key}:{page['path']}",
        "source": cfg_key,
        "authority": "canonical",
        "kind": "doc",
        "date": fetch_date,
        "title": page["title"],
        "url": page["url"],
    }
    frontmatter = yaml.safe_dump(frontmatter_dict, sort_keys=False, allow_unicode=True)
    content = f"---\n{frontmatter}---\n\n{text}"

    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(out_path)  # atomic rename within the same directory
    return out_path


def sync_docs(
    cfg: dict, hashes: dict[str, str]
) -> tuple[list[Doc], list[dict], list[str], dict[str, str]]:
    """Diff-sync one docs source against its llms.txt index.

    `hashes` maps doc id -> sha256 of the last-synced page text (as stored
    in state/doc_hashes.json by the caller).

    A page whose fetch fails (network error, non-2xx after retries) is left
    completely alone: not reported in `removed_ids` (it is still listed in
    llms.txt, just unreachable this run), and its old hash is carried
    forward unchanged into `new_hashes` so state isn't lost and it isn't
    misreported as removed on a later run either. Failed fetches are logged
    (not silently dropped) via the `logging` module.

    Returns:
      docs        Doc objects for every NEW or CHANGED page (ready to index)
      changed     [{id, old_text, new_text}] for pages that existed before
                  and whose text hash changed (drives the Sonnet "what
                  changed" summary upstream)
      removed_ids doc ids that were in `hashes` but are no longer listed in
                  the current llms.txt index (genuinely removed, not just a
                  failed fetch)
      new_hashes  full current id -> sha256 map (whole snapshot, so the
                  caller can persist it and detect removals next run)
    """
    cfg_key = cfg["key"]
    pages = list_pages(cfg)
    today = date.today().isoformat()

    docs: list[Doc] = []
    changed: list[dict] = []
    new_hashes: dict[str, str] = {}
    listed_ids: set[str] = set()  # every id still in the llms.txt index this run

    def _fetch_one(page: dict) -> tuple[dict, str | None]:
        try:
            return page, fetch_page(page["url"])
        except Exception:
            return page, None
        finally:
            throttle.pause("docs_sleep_seconds")

    with ThreadPoolExecutor(max_workers=max(1, int(throttle.get("docs_workers")))) as pool:
        futures = [pool.submit(_fetch_one, page) for page in pages]
        for fut in as_completed(futures):
            page, text = fut.result()
            doc_id = f"docs:{cfg_key}:{page['path']}"
            listed_ids.add(doc_id)

            if text is None:
                # Fetch failed after retries: still listed, so not removed.
                # Carry the old hash forward (if any) and move on.
                if doc_id in hashes:
                    new_hashes[doc_id] = hashes[doc_id]
                _log.warning("docs %s: fetch failed for %s (kept previous version)", cfg_key, page["url"])
                continue

            new_hash = _sha256(text)
            new_hashes[doc_id] = new_hash
            old_hash = hashes.get(doc_id)

            if old_hash == new_hash:
                continue  # unchanged: 0 tokens, nothing to write or index

            out_path = DATA / "docs" / cfg_key / "pages" / page["path"]
            old_text = ""
            if old_hash is not None and out_path.exists():
                old_text = out_path.read_text(encoding="utf-8", errors="ignore")
                changed.append({"id": doc_id, "old_text": old_text, "new_text": text})

            written = _write_page(cfg_key, page, text, today)
            docs.append(
                Doc(
                    id=doc_id,
                    source=cfg_key,
                    authority=cfg.get("authority", "canonical"),
                    kind="doc",
                    date=today,
                    title=page["title"],
                    url=page["url"],
                    path=str(written.relative_to(ROOT)),
                    text=text,
                )
            )

    removed_ids = [doc_id for doc_id in hashes if doc_id not in listed_ids]
    return docs, changed, removed_ids, new_hashes
