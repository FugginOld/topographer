#!/usr/bin/env bash
# Remove the topology systemd services this repo installed (dashboard server
# and/or reporting agent). Leaves the repo and out/ data in place.
set -euo pipefail

SUDO=""
if [ "$(id -u)" -ne 0 ]; then command -v sudo >/dev/null 2>&1 && SUDO=sudo; fi
command -v systemctl >/dev/null 2>&1 || { echo "no systemd — nothing to remove"; exit 0; }

removed=0
for svc in topo-server topo-agent; do
  if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
    echo "removing ${svc}…"
    $SUDO systemctl disable --now "$svc" >/dev/null 2>&1 || true
    $SUDO rm -f "/etc/systemd/system/${svc}.service"
    removed=1
  fi
done
$SUDO systemctl daemon-reload 2>/dev/null || true

[ "$removed" -eq 1 ] && echo "✓ removed. Repo + out/ data left in place." \
                     || echo "nothing installed by this repo was found."
echo "  Unraid: also delete the topo-agent lines from /boot/config/go if present."
