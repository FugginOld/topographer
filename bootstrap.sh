#!/usr/bin/env bash
# One-shot provision for a FRESH reporting machine: install deps, clone the repo,
# and set up background reporting to the dashboard server (no terminal occupied,
# survives reboot). Uses systemd where present, the boot 'go' script on Unraid.
#
# TOPO_SERVER is required — the dashboard prints its URL when you run ./install.sh,
# and the dashboard's right-click "Generate machine topology" fills this in for you.
#
#   curl -fsSL http://DASHBOARD-IP:8770/bootstrap.sh | TOPO_SERVER=http://DASHBOARD-IP:8770 bash
#   # one-time snapshot (laptop/PC), no persistent service:
#   curl -fsSL http://DASHBOARD-IP:8770/bootstrap.sh | TOPO_SERVER=http://DASHBOARD-IP:8770 TOPO_ONCE=1 bash
set -euo pipefail

SERVER="${TOPO_SERVER:-}"
if [ -z "$SERVER" ]; then
  echo "error: set TOPO_SERVER=http://<dashboard-ip>:8770 (the dashboard prints its URL on ./install.sh)" >&2
  exit 1
fi
# The dashboard server hosts the agent file set (agent + scanners + telemetry
# sampler) — fetch it from there, not github, so a machine bootstraps only from
# the dashboard it reports to. Re-running bootstrap re-fetches to update.
TARURL="${SERVER%/}/agent.tar.gz"

# Unraid runs its OS from RAM, so anything under / is wiped on reboot — clone to
# the array (appdata) instead, and persist via the boot 'go' script (no systemd).
IS_UNRAID=false
[ -f /etc/unraid-version ] && IS_UNRAID=true
if [ "$IS_UNRAID" = true ] && [ -z "${TOPO_DIR:-}" ]; then
  if [ -d /mnt/user ]; then DIR=/mnt/user/appdata/topographer
  else DIR=/boot/config/topographer; fi   # array not up: fall back to flash
fi
DIR="${TOPO_DIR:-${DIR:-$HOME/topographer}}"

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
{ have curl || have wget; } || need="$need curl"             # fetches the agent files
have python3 || need="$need python3"
python3 -m pip --version >/dev/null 2>&1 || need="$need python3-pip"  # agent self-installs Glances via pip
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

# Fetch the agent files from the dashboard server (no git, no github).
echo "fetching agent files from $SERVER -> $DIR"
mkdir -p "$DIR"
if have curl; then curl -fsSL "$TARURL"; else wget -qO- "$TARURL"; fi \
  | tar xz -C "$DIR"

chmod +x "$DIR/agent/report.sh"

# TOPO_ONCE: one-time snapshot (laptops / PCs) — push this machine's topology
# once and exit. No service, no lingering process.
if [ -n "${TOPO_ONCE:-}" ]; then
  echo "one-time snapshot -> pushing this machine's topology to $SERVER…"
  exec python3 "$DIR/agent/topo_agent.py" --server "$SERVER"
fi

# Unraid: no systemd. Persist via a flash launcher + the boot 'go' script. The
# launcher waits for the array (so appdata exists), re-clones if needed, and runs.
if [ "$IS_UNRAID" = true ]; then
  echo "installing Unraid persistence (flash launcher + go hook)…"
  LAUNCHER=/boot/config/topo-agent.sh
  cat > "$LAUNCHER" <<EOF
#!/bin/bash
# topology reporting agent launcher — persisted on the Unraid flash by bootstrap
DIR="$DIR"; SERVER="$SERVER"; TARURL="$TARURL"
for i in \$(seq 1 60); do [ -d "\$(dirname "\$DIR")" ] && break; sleep 5; done  # wait ~5m for array
if [ ! -f "\$DIR/agent/report.sh" ]; then          # re-fetch the agent files from the server (git-free)
  mkdir -p "\$DIR"; curl -fsSL "\$TARURL" | tar xz -C "\$DIR"
fi
exec bash "\$DIR/agent/report.sh" "\$SERVER"
EOF
  chmod +x "$LAUNCHER" 2>/dev/null || true   # no-op on FAT; we invoke it via bash anyway
  GO=/boot/config/go
  MARK="# topo-agent (bootstrap)"
  # /boot is FAT — it can't hold the +x bit, so the launcher must be run via
  # `bash`, not executed directly. Refresh the hook so a re-run replaces a stale
  # line instead of skipping it (the grep guard would otherwise keep the old one).
  [ -f "$GO" ] && grep -qF "$MARK" "$GO" && sed -i "\\|$MARK|,+1d" "$GO"
  printf '\n%s\n%s\n' "$MARK" "bash $LAUNCHER >/var/log/topo-agent.log 2>&1 &" >> "$GO"
  pkill -f "report.sh $SERVER" 2>/dev/null || true          # stop any previous run
  nohup bash "$LAUNCHER" >/var/log/topo-agent.log 2>&1 &  # start now (FAT-safe)
  echo
  echo "✓ topo-agent installed for Unraid (starts on boot via /boot/config/go)."
  echo "  launcher: $LAUNCHER   (clones/runs from $DIR)"
  echo "  log:      tail -f /var/log/topo-agent.log"
  echo "  stop:     remove the '$MARK' lines from $GO, then: pkill -f topo_agent.py"
  exit 0
fi

# Otherwise install a systemd service so reporting runs in the background
# (survives logout + reboot) instead of occupying this terminal. Falls back to
# foreground where systemd/root isn't available.
if command -v systemctl >/dev/null 2>&1 && [ "$CAN_ROOT" = true ]; then
  echo "installing systemd service 'topo-agent' (may prompt for sudo)…"
  $SUDO tee /etc/systemd/system/topo-agent.service >/dev/null <<EOF
[Unit]
Description=Topographer reporting agent (pushes hardware map + live telemetry)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$DIR/agent/report.sh $SERVER
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable topo-agent
  $SUDO systemctl restart topo-agent   # restart so a re-run picks up config changes
  echo
  echo "✓ topo-agent is running as a service (starts on boot)."
  echo "  status:  systemctl status topo-agent"
  echo "  logs:    journalctl -u topo-agent -f"
  echo "  stop:    $SUDO systemctl disable --now topo-agent"
else
  echo "no systemd/root — running in the foreground instead (Ctrl-C to stop):"
  exec "$DIR/agent/report.sh" "$SERVER"
fi
