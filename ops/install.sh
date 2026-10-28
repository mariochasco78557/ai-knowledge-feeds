#!/usr/bin/env bash
# Installs the AI Knowledge Feeds systemd --user units.
#
# NOT run automatically: review it, then run it yourself:
#   bash ops/install.sh
# Requires `uv` in ~/.local/bin (adjust the units if it lives elsewhere).
set -euo pipefail

UNIT_DIR="$HOME/.config/systemd/user"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_DIR="$(dirname "$SRC_DIR")"

mkdir -p "$UNIT_DIR"
for f in kb-sync.service kb-sync.timer kb-sync-failure.service \
         "kb-trickle@.service" kb-trickle-weekday.timer kb-trickle-weekend.timer \
         kb-promote.service kb-promote.timer; do
  # @KB_DIR@ is replaced with this checkout's absolute path
  sed "s|@KB_DIR@|$KB_DIR|g" "$SRC_DIR/$f" > "$UNIT_DIR/$f"
done

# YouTube blocks many datacenter IPs. If yours is blocked, run a SOCKS5/HTTP
# proxy of your choice and export KB_PROXY (or set `proxy:` on the youtube
# source in sources.yaml). Docs and GitHub sources do not need it.
# To pass env vars to the units: systemctl --user edit kb-sync.service

systemctl --user daemon-reload
systemctl --user enable --now kb-sync.timer
systemctl --user enable --now kb-trickle-weekday.timer kb-trickle-weekend.timer
# The promote timer needs a vault repo (KB_VAULT); enable it only if you use that feature:
#   systemctl --user enable --now kb-promote.timer

# Let the timers run when you are not logged in (server use).
loginctl enable-linger "$(whoami)"

echo "Installed. Check with:"
echo "  systemctl --user list-timers"
echo "  systemctl --user status kb-sync.timer"
