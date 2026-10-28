"""Runtime path resolution with an optional AI_KB_ROOT override (KB_ROOT is accepted as an alias).

`kb.paths` holds the static defaults. When the environment variable AI_KB_ROOT
is set, importing this module re-points `kb.paths` attributes at the
override, so modules that read `paths.X` at call time follow it too.
Import this module before any module that does `from kb.paths import X`.
"""
from __future__ import annotations

import os
from pathlib import Path

from kb import paths as _paths


def _apply_override() -> None:
    override = os.environ.get("AI_KB_ROOT") or os.environ.get("KB_ROOT")
    if not override:
        return
    root = Path(override).expanduser().resolve()
    _paths.ROOT = root
    _paths.DATA = root / "data"
    _paths.INDEX_DIR = root / "index"
    _paths.STATE_DIR = root / "state"
    _paths.LOGS = root / "logs"
    if (root / "sources.yaml").exists():
        _paths.SOURCES_YAML = root / "sources.yaml"


_apply_override()


def root() -> Path:
    return _paths.ROOT


def data() -> Path:
    return _paths.DATA


def index_dir() -> Path:
    return _paths.INDEX_DIR


def state_dir() -> Path:
    return _paths.STATE_DIR


def logs() -> Path:
    return _paths.LOGS


def sources_yaml() -> Path:
    return _paths.SOURCES_YAML


def db_path() -> Path:
    return _paths.INDEX_DIR / "kb.sqlite"
