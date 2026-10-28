"""YouTube discovery (RSS + yt-dlp flat-playlist backfill) and subtitle fetch.

`cfg` throughout is the source's entry from sources.yaml (a dict with at
least: channel_url, channel_id, include_keywords; optionally `proxy`).

IP-block workaround (documented 2026-09-13): from this VPS, YouTube blocks
both yt-dlp and youtube-transcript-api ("Sign in to confirm you're not a
bot" / RequestBlocked) regardless of yt-dlp player_client variant or PO
Token provider — the block happens at the player-response stage, before any
PO Token would even be requested, so it's an IP-reputation block rather than
a PO Token gap. The only free/no-account fix found to work reliably (5/5
test videos) is routing YouTube requests through a local SOCKS5 proxy
(any SOCKS5/HTTP proxy). Set the KB_PROXY environment variable, or the
`proxy:` key on a youtube source in sources.yaml. See README.md. Docs/GitHub
sources are unaffected and do NOT use this proxy.
"""
from __future__ import annotations

import json
import os
import logging
import shutil
import socket
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import feedparser

from kb import throttle
from kb.paths import SOURCES_YAML

RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

_STRONG_KEYWORDS = ("claude code", "codex", "claude")

_log = logging.getLogger("kb.sources.youtube")


def discover_rss(cfg: dict) -> list[dict]:
    """Latest videos from the channel's RSS feed (no API key, ~15 most recent).

    Returns dicts: id, title, published (YYYY-MM-DD), description, url.
    """
    channel_id = cfg["channel_id"]
    url = RSS_URL.format(channel_id=channel_id)
    feed = feedparser.parse(url)
    out = []
    for e in feed.entries:
        video_id = e.get("yt_videoid") or e.get("id", "").rsplit(":", 1)[-1]
        published = (e.get("published") or "")[:10]
        out.append(
            {
                "id": video_id,
                "title": e.get("title", ""),
                "published": published,
                "description": e.get("summary", ""),
                "url": e.get("link") or f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return out


def keyword_match(cfg: dict, title: str, description: str) -> str:
    """"yes" | "maybe" | "no" keyword prefilter.

    Strong match (claude code / codex / claude in the title) -> "yes".
    Any configured include_keyword found in title or description -> "maybe"
    (send to the Sonnet classifier). Otherwise -> "no".
    """
    t = (title or "").lower()
    d = (description or "").lower()

    if any(k in t for k in _STRONG_KEYWORDS):
        return "yes"

    keywords = [k.lower() for k in cfg.get("include_keywords", [])]
    if any(k in t or k in d for k in keywords):
        return "maybe"

    return "no"


def _have_ytdlp() -> bool:
    return shutil.which("yt-dlp") is not None


@lru_cache(maxsize=1)
def _default_proxy() -> str | None:
    """Read the `proxy` key off the first youtube-type source in
    sources.yaml, for callers (like fetch_subtitles) that aren't handed a
    `cfg` directly. Cached for the process lifetime (sources.yaml doesn't
    change mid-sync); errors are swallowed -> no proxy, not a crash.
    """
    env = os.environ.get("KB_PROXY", "").strip()
    if env:
        return env
    try:
        import yaml

        with SOURCES_YAML.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for src in data.get("sources", []):
            if src.get("type") == "youtube" and src.get("proxy"):
                return src["proxy"]
    except Exception:
        return None
    return None


def _proxy_reachable(proxy: str, timeout: float = 2.0) -> bool:
    """Quick TCP check that the proxy's host:port is accepting connections,
    so a down proxy fails fast with a clear message instead of yt-dlp
    timing out (or silently falling through to the blocked direct path).
    """
    parsed = urlparse(proxy)
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_proxy(cfg: dict | None) -> str | None:
    """Proxy URL to use for a YouTube request: `cfg["proxy"]` if given,
    else whatever sources.yaml has configured. Logs (once per call) and
    returns None if a proxy is configured but not reachable, so callers can
    skip cleanly instead of hitting the IP block directly.
    """
    proxy = (cfg or {}).get("proxy") or _default_proxy()
    if not proxy:
        return None
    if not _proxy_reachable(proxy):
        _log.error(
            "youtube: configured proxy %s is not reachable (is your proxy "
            "running? set KB_PROXY or `proxy:` in sources.yaml) -- skipping this "
            "fetch rather than hitting YouTube directly from this VPS's blocked IP.",
            proxy,
        )
        return None
    return proxy


def youtube_reachable(cfg: dict | None, timeout: float = 10.0) -> bool:
    """End-to-end check that YouTube answers through the configured proxy.

    A listening port is not enough: once a tunnel was up
    locally but returned "Host unreachable", and every video in the run was
    wrongly recorded as "no subtitles". Callers skip YouTube for the run
    (without consuming attempts) when this is False.
    """
    proxy = (cfg or {}).get("proxy") or _default_proxy()
    if proxy and not _proxy_reachable(proxy):
        return False
    try:
        import requests

        proxies = None
        if proxy:
            p = proxy.replace("socks5://", "socks5h://", 1)
            proxies = {"http": p, "https": p}
        r = requests.head("https://www.youtube.com", proxies=proxies, timeout=timeout, allow_redirects=True)
        return r.status_code < 500
    except Exception as e:  # noqa: BLE001
        _log.warning("youtube: reachability check failed via %s: %s", proxy or "direct", e)
        return False


def _ytdlp_json(args: list[str], timeout: int = 120, proxy: str | None = None) -> dict | list | None:
    if not _have_ytdlp():
        return None
    cmd = ["yt-dlp", "-J", "--skip-download", "--no-warnings", *args]
    if proxy:
        cmd += ["--proxy", proxy]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def discover_backfill(cfg: dict, since: str) -> list[dict]:
    """Flat-playlist listing of the channel's /videos and /shorts tabs,
    going back to `since` (YYYY-MM-DD).

    yt-dlp's --flat-playlist listing has titles/ids but no reliable upload
    date, so: prefilter candidates by title keyword match first, then fetch
    full per-video metadata (upload_date, description) only for those
    candidates. Stops scanning a tab once a fetched candidate is older than
    `since` (best-effort: channel tabs are newest-first).

    Returns dicts: id, title, published (YYYY-MM-DD), description, url.
    """
    if not _have_ytdlp():
        return []

    proxy = _resolve_proxy(cfg)

    channel_url = cfg["channel_url"].rstrip("/")
    seen_ids: set[str] = set()
    results: list[dict] = []

    for tab in ("videos", "shorts"):
        flat = _ytdlp_json(["--flat-playlist", f"{channel_url}/{tab}"], timeout=180, proxy=proxy)
        if not flat or "entries" not in flat:
            continue

        for entry in flat["entries"]:
            if not entry:
                continue
            video_id = entry.get("id")
            title = entry.get("title") or ""
            if not video_id or video_id in seen_ids:
                continue
            seen_ids.add(video_id)

            match = keyword_match(cfg, title, "")
            if match == "no":
                continue  # cheap title-only prefilter; full pass happens upstream too

            throttle.pause("ytdlp_sleep_seconds")
            meta = _ytdlp_json(
                ["--no-playlist", f"https://www.youtube.com/watch?v={video_id}"],
                timeout=60,
                proxy=proxy,
            )
            if not meta:
                continue

            upload_date = meta.get("upload_date", "")  # YYYYMMDD
            published = (
                f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
                if len(upload_date) == 8
                else ""
            )
            if published and published < since:
                break  # this tab is newest-first; older than window -> stop

            results.append(
                {
                    "id": video_id,
                    "title": meta.get("title", title),
                    "published": published,
                    "description": meta.get("description", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )

    return results


def fetch_subtitles(video_id: str, out_dir: str | Path, cfg: dict | None = None) -> Path | None:
    """Fetch English subtitles for `video_id` into `out_dir` as
    `<video_id>.vtt` using yt-dlp (same flags as the legacy
    download_transcripts.py). Falls back to youtube-transcript-api,
    writing a timestamped .vtt-equivalent, if yt-dlp gets nothing (or is
    unavailable). Returns the written path, or None if no subs exist yet.

    `cfg` is optional (existing callers pass just video_id/out_dir): if
    omitted, the proxy is read straight from sources.yaml via
    `_resolve_proxy`/`_default_proxy`, so no caller changes are required to
    pick up the proxy workaround.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _existing() -> Path | None:
        for ext in ("vtt", "srt"):
            hits = sorted(out_dir.glob(f"{video_id}*.{ext}"))
            if hits:
                return hits[0]
        return None

    hit = _existing()
    if hit:
        return hit

    proxy = _resolve_proxy(cfg)

    if _have_ytdlp():
        out_tmpl = str(out_dir / f"{video_id}.%(ext)s")
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            "en.*,en",
            "--sub-format",
            "vtt/srt/best",
            "--convert-subs",
            "vtt",
            "--no-playlist",
            "--retries",
            "3",
            "--ignore-errors",
            "-o",
            out_tmpl,
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        if proxy:
            cmd += ["--proxy", proxy]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            pass
        hit = _existing()
        if hit:
            return hit

    # Fallback: youtube-transcript-api
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        if proxy:
            from youtube_transcript_api.proxies import GenericProxyConfig

            api = YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy))
        else:
            api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        lines = ["WEBVTT", ""]
        for snippet in transcript:
            start = snippet.start
            end = start + snippet.duration
            lines.append(_seconds_to_vtt_ts(start) + " --> " + _seconds_to_vtt_ts(end))
            lines.append(snippet.text)
            lines.append("")
        out_path = out_dir / f"{video_id}.en.vtt"
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path
    except Exception as exc:
        _log.warning("youtube: subtitle fetch failed for %s: %s: %s", video_id, type(exc).__name__, exc)
        return None


def _seconds_to_vtt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
