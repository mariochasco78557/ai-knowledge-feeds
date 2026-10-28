"""Central path constants for the AI Knowledge Feeds project.

Every module should import these instead of hard-coding paths, so the
project can be moved or run from any working directory (e.g. under
systemd, where cwd is set via WorkingDirectory=).
"""
from pathlib import Path

# Project root: the repository root (parent of the kb/ package dir). Defaults
# below are relative to it (./data, ./index, ./state); set AI_KB_ROOT to move them.
ROOT: Path = Path(__file__).resolve().parents[1]

DATA: Path = ROOT / "data"
INDEX_DIR: Path = ROOT / "index"
STATE_DIR: Path = ROOT / "state"
LOGS: Path = ROOT / "logs"

SOURCES_YAML: Path = ROOT / "sources.yaml"
