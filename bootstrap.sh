#!/usr/bin/env bash
# One-shot provision for a FRESH Debian/Ubuntu reporting machine: install deps,
# clone the repo, and install a background systemd service that reports to the
# dashboard server (no terminal left occupied; survives logout + reboot).
#
#   curl -fsSL https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.sh | bash
#   # or, to point at a different server / dir:
#   TOPO_SERVER=http://192.168.1.225:8770 TOPO_DIR=~/topo bash bootstrap.sh
set -euo pipefail

REPO="${TOPO_REPO:-https://github.com/FugginOld/topologygenerator.git}"
DIR="${TOPO_DIR:-$HOME/topologygenerator}"
SERVER="${TOPO_SERVER:-http://192.168.1.225:8770}"

# Privilege: already root -> no sudo needed; otherwise use sudo if present.
# CAN_ROOT is true when we can write system files (install pkgs + the unit).
RUN_USER="$(id -un)"
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
if [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; then CAN_ROOT=true; else CAN_ROOT=false; fi

if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -qq \
    && $SUDO apt-get install -y -qq git python3 pciutils util-linux dmidecode \
    || echo "warn: dependency install failed — install manually: git python3 pciutils util-linux dmidecode"
else
  # ponytail: no apt-get (e.g. Unraid/Slackware) — check what's actually present instead of a blanket warning
  missing=""
  for bin in git python3 lspci lsblk dmidecode; do
    command -v "$bin" >/dev/null 2>&1 || missing="$missing $bin"
  done
  [ -n "$missing" ] && echo "warn: no apt-get and missing:$missing — install these manually"
fi

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi

chmod +x "$DIR/report.sh"

# Install as a systemd service so reporting runs in the background (survives
# logout + reboot) instead of occupying this terminal. Falls back to foreground
# where systemd/root isn't available.
if command -v systemctl >/dev/null 2>&1 && [ "$CAN_ROOT" = true ]; then
  echo "installing systemd service 'topology-agent' (may prompt for sudo)…"
  $SUDO tee /etc/systemd/system/topology-agent.service >/dev/null <<EOF
[Unit]
Description=Topology reporting agent (pushes hardware map + live telemetry)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$DIR/report.sh $SERVER
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now topology-agent
  echo
  echo "✓ topology-agent is running as a service (starts on boot)."
  echo "  status:  systemctl status topology-agent"
  echo "  logs:    journalctl -u topology-agent -f"
  echo "  stop:    $SUDO systemctl disable --now topology-agent"
else
  echo "no systemd/root — running in the foreground instead (Ctrl-C to stop):"
  exec "$DIR/report.sh" "$SERVER"
fi
