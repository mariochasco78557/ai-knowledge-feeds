"""Resource throttling knobs (the `throttle:` block of sources.yaml).

This VPS also runs several Claude Code / VS Code sessions; on 2026-09-13 a
bulk backfill pushed the machine into the OOM killer, which took VS Code
(and the backfill with it) down. Everything that fetches or processes data
must therefore go slowly: few items per run, one worker, pauses between
network calls. CPU/RAM hard caps live in the systemd units (ops/*.service).
"""
from __future__ import annotations

import os
import time
from functools import lru_cache

from kb import root as R

DEFAULTS: dict[str, float | int | bool] = {
    "max_videos_per_run": 3,       # videos that reach subtitle fetch + `claude -p` per run
    "sleep_between_videos": 60,    # seconds
    "ytdlp_sleep_seconds": 5,      # between yt-dlp metadata calls during discovery
    "docs_workers": 1,             # parallel HTTP fetches for docs mirrors
    "docs_sleep_seconds": 0.5,     # pause after each docs page fetch
    "stop_after_consecutive_failures": 2,  # likely blocked/proxy down -> stop YouTube for this run
    "weekly_rebuild": False,       # full SQLite rebuild on Sundays (heavy; `kb reindex` by hand instead)
}


@lru_cache(maxsize=1)
def settings() -> dict:
    import yaml

    try:
        with R.sources_yaml().open(encoding="utf-8") as f:
            block = (yaml.safe_load(f) or {}).get("throttle") or {}
    except OSError:
        block = {}
    merged = {**DEFAULTS, **{k: v for k, v in block.items() if k in DEFAULTS}}
    # Per-timer overrides, e.g. KB_THROTTLE_MAX_VIDEOS_PER_RUN=6 set by a systemd unit.
    for k, default in DEFAULTS.items():
        env = os.environ.get(f"KB_THROTTLE_{k.upper()}")
        if env is not None and not isinstance(default, bool):
            merged[k] = type(default)(env)
    return merged


def get(key: str):
    return settings()[key]


def pause(key: str) -> None:
    secs = float(get(key) or 0)
    if secs > 0:
        time.sleep(secs)
