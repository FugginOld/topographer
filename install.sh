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
# Unraid has no systemd and runs its OS from RAM. Persist the dashboard the same
# way the agent does: a flash launcher invoked from the boot 'go' script. The
# server is stdlib (yaml is a lazy optional import), so stock python3 is enough.
if [ -f /etc/unraid-version ]; then
  echo "Unraid detected — installing via boot 'go' script (no systemd)…"
  command -v python3 >/dev/null 2>&1 || {
    echo "python3 not found — install it (NerdTools plugin) and re-run"; exit 1; }

  # The network scan parses config.yaml, which needs PyYAML. Unraid's python3 has
  # no PyYAML and no persistent site-packages (OS runs from RAM), so vendor it into
  # appdata and point PYTHONPATH there from the launcher (survives reboot).
  if ! PYTHONPATH="$DIR/vendor" python3 -c "import yaml" 2>/dev/null; then
    echo "vendoring PyYAML -> $DIR/vendor …"
    python3 -m pip --version >/dev/null 2>&1 || python3 -m ensurepip >/dev/null 2>&1 || true
    python3 -m pip install -q --target "$DIR/vendor" pyyaml \
      || echo "warn: PyYAML install failed — network scan (config.yaml) stays off until it's present"
  fi

  LAUNCHER=/boot/config/topo-server.sh
  cat > "$LAUNCHER" <<EOF
#!/bin/bash
# topology dashboard server launcher — persisted on the Unraid flash by install.sh
DIR="$DIR"
for i in \$(seq 1 60); do [ -d "\$(dirname "\$DIR")" ] && break; sleep 5; done  # wait ~5m for array
export PYTHONPATH="\$DIR/vendor\${PYTHONPATH:+:\$PYTHONPATH}"   # vendored PyYAML (config.yaml)
${TOPO_TOKEN:+export TOPO_TOKEN=$TOPO_TOKEN}
exec python3 "\$DIR/renderers/html/topo_server.py"
EOF
  chmod +x "$LAUNCHER" 2>/dev/null || true   # no-op on FAT; we invoke it via bash anyway
  GO=/boot/config/go
  MARK="# topo-server (install.sh)"
  # /boot is FAT (no +x bit) so the launcher runs via `bash`. Refresh the hook so
  # a re-run replaces a stale line instead of the grep guard keeping the old one.
  [ -f "$GO" ] && grep -qF "$MARK" "$GO" && sed -i "\\|$MARK|,+1d" "$GO"
  printf '\n%s\n%s\n' "$MARK" "bash $LAUNCHER >/var/log/topo-server.log 2>&1 &" >> "$GO"
  pkill -f "topo_server.py" 2>/dev/null || true          # stop any previous run
  nohup bash "$LAUNCHER" >/var/log/topo-server.log 2>&1 &  # start now (FAT-safe)
  IP="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.][0-9.]*\).*/\1/p' | head -1)"
  [ -z "$IP" ] && IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "✓ topo-server installed for Unraid (starts on boot via /boot/config/go)."
  echo "  dashboard:    http://${IP:-<this-host-ip>}:8770"
  echo "  point agents: TOPO_SERVER=http://${IP:-<this-host-ip>}:8770"
  echo "  launcher:     $LAUNCHER   (runs from $DIR)"
  echo "  log:          tail -f /var/log/topo-server.log"
  echo "  stop:         remove the '$MARK' lines from $GO, then: pkill -f topo_server.py"
  exit 0
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
