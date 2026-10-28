"""GitHub releases/changelogs and repo listings, via `gh api` (public
read-only GET endpoints only — no writes, no auth scopes beyond what the
logged-in `gh` CLI already has).
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import date as _date

from kb.models import Doc
from kb.paths import DATA, ROOT

_TIMEOUT = 30
_CHANGELOG_HEADING = re.compile(r"^##\s+\[?v?([0-9][^\]\s]*)\]?\s*(.*)$")
_RELEASES_PER_PAGE = 100  # "up to ~100 recent" releases, single page


def _gh_api(path: str, jq: str | None = None) -> str | None:
    cmd = ["gh", "api", path]
    if jq:
        cmd += ["--jq", jq]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
        if p.returncode != 0:
            return None
        return p.stdout
    except subprocess.TimeoutExpired:
        return None


def _gh_api_json(path: str):
    out = _gh_api(path)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _normalize_tag(tag: str) -> str:
    return tag.lstrip("vV")


def _changelog_last_commit_date(repo: str, changelog_path: str) -> str | None:
    """Best-effort: date of the changelog file's most recent commit (used as
    a fallback date for CHANGELOG-derived entries that have no matching
    GitHub Release)."""
    out = _gh_api_json(f"repos/{repo}/commits?path={changelog_path}&per_page=1")
    if not out:
        return None
    try:
        return out[0]["commit"]["committer"]["date"][:10]
    except (KeyError, IndexError, TypeError):
        return None


def _fetch_changelog_text(repo: str, changelog_path: str) -> str | None:
    """Decode CHANGELOG.md (or similar) content via the GitHub contents API."""
    out = _gh_api(f"repos/{repo}/contents/{changelog_path}", jq=".content")
    if not out:
        return None
    b64 = out.strip().strip('"').replace("\\n", "")
    try:
        return base64.b64decode(b64).decode("utf-8", errors="ignore")
    except Exception:
        return None


def _parse_changelog_sections(text: str) -> list[dict]:
    """Split a Keep-a-Changelog-style CHANGELOG.md by '## <version>' headings.
    Returns dicts: version, title, body (in file order, newest first as
    written in the file).
    """
    sections: list[dict] = []
    lines = text.splitlines()
    current: dict | None = None
    body_lines: list[str] = []

    def _flush():
        if current is not None:
            current["body"] = "\n".join(body_lines).strip()
            sections.append(current)

    for line in lines:
        m = _CHANGELOG_HEADING.match(line.strip())
        if m:
            _flush()
            current = {"version": m.group(1), "title": line.strip().lstrip("# ").strip()}
            body_lines = []
        elif current is not None:
            body_lines.append(line)
    _flush()
    return sections


def sync_releases(cfg: dict, seen_tags: set[str]) -> list[Doc]:
    """Fetch new GitHub releases for cfg['repo'] (and, if configured, parse
    CHANGELOG.md entries too when releases are sparse). Writes
    data/github/<source>/<tag>.md per new release/version. Returns Doc
    objects for anything not already in `seen_tags` (normalized, no
    leading 'v').

    `cfg['tag_exclude']`, if set, is a regex matched against the raw tag
    name; matching tags (e.g. codex `-alpha`/`-beta`/`-rc` pre-releases) are
    skipped entirely, in both the Releases and CHANGELOG.md paths.
    """
    cfg_key = cfg["key"]
    repo = cfg["repo"]
    docs: list[Doc] = []
    normalized_seen = {_normalize_tag(t) for t in seen_tags}
    tag_exclude = re.compile(cfg["tag_exclude"]) if cfg.get("tag_exclude") else None
    today = _date.today().isoformat()

    # tag (normalized) -> release date, kept even for already-seen tags so
    # the CHANGELOG.md fallback below can reuse it as its date too.
    release_dates: dict[str, str] = {}

    releases = _gh_api_json(f"repos/{repo}/releases?per_page={_RELEASES_PER_PAGE}") or []
    for rel in releases:
        tag = rel.get("tag_name") or rel.get("name") or ""
        if tag_exclude and tag_exclude.search(tag):
            continue
        norm = _normalize_tag(tag)
        if not norm:
            continue
        release_dates[norm] = (rel.get("published_at") or rel.get("created_at") or "")[:10]
        if norm in normalized_seen:
            continue
        normalized_seen.add(norm)

        title = rel.get("name") or tag
        body = rel.get("body") or ""
        url = rel.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}"
        published = (rel.get("published_at") or rel.get("created_at") or "")[:10]

        out_path = DATA / "github" / cfg_key / f"{norm}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            "---\n"
            f"id: gh:{cfg_key}:{norm}\n"
            f"source: {cfg_key}\n"
            "authority: canonical\n"
            "kind: release\n"
            f"date: {published}\n"
            f"title: {title!r}\n"
            f"url: {url}\n"
            "---\n\n"
            f"# {title}\n\n{body}\n",
            encoding="utf-8",
        )
        docs.append(
            Doc(
                id=f"gh:{cfg_key}:{norm}",
                source=cfg_key,
                authority=cfg.get("authority", "canonical"),
                kind="release",
                date=published,
                title=title,
                url=url,
                path=str(out_path.relative_to(ROOT)),
                text=body,
                meta={"tag": tag},
            )
        )

    # Sparse-releases fallback: parse CHANGELOG.md for versions not already
    # covered by GitHub Releases.
    changelog_path = cfg.get("changelog_path")
    if changelog_path and len(releases) < 3:
        text = _fetch_changelog_text(repo, changelog_path)
        if text:
            # Lazily fetched at most once per sync call, only if needed
            # (some section has no matching release date).
            commit_date: str | None = None
            commit_date_fetched = False

            for section in _parse_changelog_sections(text):
                if tag_exclude and tag_exclude.search(section["version"]):
                    continue
                norm = _normalize_tag(section["version"])
                if not norm or norm in normalized_seen:
                    continue
                normalized_seen.add(norm)

                title = section["title"] or section["version"]
                body = section["body"]
                url = f"https://github.com/{repo}/blob/main/{changelog_path}#{norm}"

                # Date preference: matching GitHub Release date, else the
                # changelog file's last commit date, else today (fetch date).
                published = release_dates.get(norm)
                if not published:
                    if not commit_date_fetched:
                        commit_date = _changelog_last_commit_date(repo, changelog_path)
                        commit_date_fetched = True
                    published = commit_date or today

                out_path = DATA / "github" / cfg_key / f"{norm}.md"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    "---\n"
                    f"id: gh:{cfg_key}:{norm}\n"
                    f"source: {cfg_key}\n"
                    "authority: canonical\n"
                    "kind: release\n"
                    f"date: {published}\n"
                    f"title: {title!r}\n"
                    f"url: {url}\n"
                    "---\n\n"
                    f"# {title}\n\n{body}\n",
                    encoding="utf-8",
                )
                docs.append(
                    Doc(
                        id=f"gh:{cfg_key}:{norm}",
                        source=cfg_key,
                        authority=cfg.get("authority", "canonical"),
                        kind="release",
                        date=published,
                        title=title,
                        url=url,
                        path=str(out_path.relative_to(ROOT)),
                        text=body,
                        meta={"tag": section["version"], "from": "CHANGELOG.md"},
                    )
                )

    return docs


def sync_repos(cfg: dict) -> list[Doc]:
    """List public repos for cfg['user']. Writes a single
    data/github/<source>/repos.md table (name | description | updated |
    url) and returns one Doc per repo (kind=repo, empty text; details in
    meta)."""
    cfg_key = cfg["key"]
    user = cfg["user"]

    repos = (
        _gh_api_json(f"users/{user}/repos?per_page=100&sort=updated&type=owner") or []
    )

    lines = ["| name | description | updated | url |", "|---|---|---|---|"]
    docs: list[Doc] = []
    for repo in repos:
        name = repo.get("name", "")
        desc = (repo.get("description") or "").replace("|", "/")
        updated = (repo.get("updated_at") or repo.get("pushed_at") or "")[:10]
        url = repo.get("html_url", "")
        lines.append(f"| {name} | {desc} | {updated} | {url} |")

    out_path = DATA / "github" / cfg_key / "repos.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "---\n"
        f"source: {cfg_key}\n"
        "authority: practice\n"
        "kind: repo\n"
        "---\n\n"
        f"# {user} — public repos\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    for repo in repos:
        name = repo.get("name", "")
        updated = (repo.get("updated_at") or repo.get("pushed_at") or "")[:10]
        docs.append(
            Doc(
                id=f"gh:{cfg_key}:{name}",
                source=cfg_key,
                authority=cfg.get("authority", "practice"),
                kind="repo",
                date=updated,
                title=name,
                url=repo.get("html_url", ""),
                path=str(out_path.relative_to(ROOT)),
                text="",
                meta={"description": repo.get("description") or ""},
            )
        )
    return docs
