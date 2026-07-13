#!/usr/bin/env bash
# One-shot provision for a FRESH reporting machine: install deps, clone the repo,
# and set up background reporting to the dashboard server (no terminal occupied,
# survives reboot). Uses systemd where present, the boot 'go' script on Unraid.
#
#   curl -fsSL https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.sh | bash
#   # or, to point at a different server / dir:
#   TOPO_SERVER=http://192.168.1.225:8770 TOPO_DIR=~/topo bash bootstrap.sh
#   # one-time snapshot (laptop/PC), no persistent service:
#   TOPO_SERVER=http://192.168.1.225:8770 TOPO_ONCE=1 bash bootstrap.sh
set -euo pipefail

REPO="${TOPO_REPO:-https://github.com/FugginOld/topologygenerator.git}"
SERVER="${TOPO_SERVER:-http://192.168.1.225:8770}"

# Unraid runs its OS from RAM, so anything under / is wiped on reboot — clone to
# the array (appdata) instead, and persist via the boot 'go' script (no systemd).
IS_UNRAID=false
[ -f /etc/unraid-version ] && IS_UNRAID=true
if [ "$IS_UNRAID" = true ] && [ -z "${TOPO_DIR:-}" ]; then
  if [ -d /mnt/user ]; then DIR=/mnt/user/appdata/topologygenerator
  else DIR=/boot/config/topologygenerator; fi   # array not up: fall back to flash
fi
DIR="${TOPO_DIR:-${DIR:-$HOME/topologygenerator}}"

# Privilege: already root -> no sudo needed; otherwise use sudo if present.
# CAN_ROOT is true when we can write system files (install pkgs + the unit).
RUN_USER="$(id -un)"
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi
if [ "$(id -u)" -eq 0 ] || [ -n "$SUDO" ]; then CAN_ROOT=true; else CAN_ROOT=false; fi

# Only touch the package manager for tools that are actually MISSING *and*
# relevant to this machine. A re-run on a provisioned host — or one with a wedged
# dpkg but the tools already present (e.g. a Pi) — then never invokes apt at all.
have() { command -v "$1" >/dev/null 2>&1; }
need=""
have git     || need="$need git"
have python3 || need="$need python3"
have lsblk   || need="$need util-linux"                        # storage enumeration
[ -n "$(ls -A /sys/bus/pci/devices 2>/dev/null)" ] && ! have lspci && need="$need pciutils"  # PCI names; skip on a Pi
case "$(uname -m)" in x86_64|i?86) have dmidecode || need="$need dmidecode";; esac  # SMBIOS is x86-only

if [ -n "$need" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    echo "installing:$need"
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq $need \
      || echo "warn: could not install:$need — fix dpkg, or install manually; devices needing them may be missing"
  else
    echo "warn: missing and no apt-get:$need — install these manually"
  fi
fi

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi

chmod +x "$DIR/report.sh"

# TOPO_ONCE: one-time snapshot (laptops / PCs) — push this machine's topology
# once and exit. No service, no lingering process.
if [ -n "${TOPO_ONCE:-}" ]; then
  echo "one-time snapshot -> pushing this machine's topology to $SERVER…"
  exec python3 "$DIR/topology_agent.py" --server "$SERVER"
fi

# Unraid: no systemd. Persist via a flash launcher + the boot 'go' script. The
# launcher waits for the array (so appdata exists), re-clones if needed, and runs.
if [ "$IS_UNRAID" = true ]; then
  echo "installing Unraid persistence (flash launcher + go hook)…"
  LAUNCHER=/boot/config/topology-agent.sh
  cat > "$LAUNCHER" <<EOF
#!/bin/bash
# topology reporting agent launcher — persisted on the Unraid flash by bootstrap
DIR="$DIR"; SERVER="$SERVER"; REPO="$REPO"
for i in \$(seq 1 60); do [ -d "\$(dirname "\$DIR")" ] && break; sleep 5; done  # wait ~5m for array
[ -d "\$DIR/.git" ] || git clone "\$REPO" "\$DIR"
exec "\$DIR/report.sh" "\$SERVER"
EOF
  chmod +x "$LAUNCHER"
  GO=/boot/config/go
  MARK="# topology-agent (bootstrap)"
  if ! grep -qF "$MARK" "$GO" 2>/dev/null; then
    printf '\n%s\n%s\n' "$MARK" "$LAUNCHER >/var/log/topology-agent.log 2>&1 &" >> "$GO"
  fi
  pkill -f "report.sh $SERVER" 2>/dev/null || true          # stop any previous run
  nohup "$LAUNCHER" >/var/log/topology-agent.log 2>&1 &      # start now
  echo
  echo "✓ topology-agent installed for Unraid (starts on boot via /boot/config/go)."
  echo "  launcher: $LAUNCHER   (clones/runs from $DIR)"
  echo "  log:      tail -f /var/log/topology-agent.log"
  echo "  stop:     remove the '$MARK' lines from $GO, then: pkill -f topology_agent.py"
  exit 0
fi

# Otherwise install a systemd service so reporting runs in the background
# (survives logout + reboot) instead of occupying this terminal. Falls back to
# foreground where systemd/root isn't available.
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
  $SUDO systemctl enable topology-agent
  $SUDO systemctl restart topology-agent   # restart so a re-run picks up config changes
  echo
  echo "✓ topology-agent is running as a service (starts on boot)."
  echo "  status:  systemctl status topology-agent"
  echo "  logs:    journalctl -u topology-agent -f"
  echo "  stop:    $SUDO systemctl disable --now topology-agent"
else
  echo "no systemd/root — running in the foreground instead (Ctrl-C to stop):"
  exec "$DIR/report.sh" "$SERVER"
fi
