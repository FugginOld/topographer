#!/usr/bin/env bash
# Install the topology dashboard server as a systemd service on THIS Linux host.
# Idempotent — re-run (or ./update.sh, which fetches the latest first) to update.
# Remove with ./uninstall.sh.
#
#   ./install.sh
#   TOPO_TOKEN=secret ./install.sh     # require a shared token from agents
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"          # repo root (this script lives here)
RUN_USER="$(id -un)"
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 && SUDO=sudo || { echo "not root and no sudo — cannot install"; exit 1; }
fi
command -v systemctl >/dev/null 2>&1 || {
  echo "no systemd here — run the dashboard directly:"
  echo "  python3 $DIR/renderers/html/topo_server.py"; exit 1; }

# deps: only what's missing (the server is stdlib; PyYAML is the one Python dep)
have() { command -v "$1" >/dev/null 2>&1; }
need=""
have python3 || need="$need python3"
python3 -c "import yaml" >/dev/null 2>&1 || need="$need python3-yaml"
if [ -n "$need" ]; then
  if have apt-get; then
    echo "installing:$need"
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq $need || echo "warn: could not install:$need"
  else
    echo "warn: missing (install manually):$need"
  fi
fi

UNIT=/etc/systemd/system/topo-server.service
echo "installing service -> $UNIT"
$SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Topographer dashboard server (fleet dashboard + ingest/telemetry API)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/renderers/html/topo_server.py
Restart=always
RestartSec=10
${TOPO_TOKEN:+Environment=TOPO_TOKEN=$TOPO_TOKEN}

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable topo-server >/dev/null 2>&1 || true
$SUDO systemctl restart topo-server

# open the port where a firewall is present (best effort)
if have ufw; then
  $SUDO ufw allow 8770/tcp >/dev/null 2>&1 || true
elif have firewall-cmd; then
  $SUDO firewall-cmd --permanent --add-port=8770/tcp >/dev/null 2>&1 && $SUDO firewall-cmd --reload >/dev/null 2>&1 || true
fi

# this host's own LAN IP — the address to open the dashboard and point agents at
IP="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.][0-9.]*\).*/\1/p' | head -1)"
[ -z "$IP" ] && IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "✓ topo-server running (starts on boot)."
echo "  dashboard:      http://${IP:-localhost}:8770"
echo "  point agents:   TOPO_SERVER=http://${IP:-<this-host-ip>}:8770"
echo "  logs:           journalctl -u topo-server -f"
echo "  update:         ./update.sh"
echo "  remove:         ./uninstall.sh"
