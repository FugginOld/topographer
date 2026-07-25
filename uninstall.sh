#!/usr/bin/env bash
# Remove what this repo installed on THIS host: the systemd units, and/or the
# retired Unraid flash-launcher install (the dashboard is a container now).
# Leaves the repo and out/ data in place.
set -euo pipefail

SUDO=""
if [ "$(id -u)" -ne 0 ]; then command -v sudo >/dev/null 2>&1 && SUDO=sudo; fi

removed=0

# ── Unraid flash install (no systemd): a launcher on /boot/config plus a two-line
# hook in the boot 'go' script. install.sh no longer creates the server one; this
# removes it either way, so an existing native install can migrate to the container.
# TOPO_BOOT points the removal at a fake flash dir for tests/test_uninstall_unraid.sh
# — a bad edit here would corrupt the array's boot config, so it stays covered.
BOOT="${TOPO_BOOT:-/boot/config}"
for entry in "topo-server|# topo-server (install.sh)" "topo-agent|# topo-agent (bootstrap)"; do
  svc="${entry%%|*}"
  mark="${entry#*|}"
  launcher="$BOOT/$svc.sh"
  hooked=0
  [ -f "$BOOT/go" ] && grep -qF "$mark" "$BOOT/go" && hooked=1
  if [ "$hooked" = 1 ] || [ -f "$launcher" ]; then
    echo "removing ${svc} (Unraid flash install)…"
    # drop the mark line and the launch line right after it; everything else stays
    [ "$hooked" = 1 ] && sed -i "\\|$mark|,+1d" "$BOOT/go"
    rm -f "$launcher"
    # only stop a live process on the host that actually ran it
    [ -f /etc/unraid-version ] && { pkill -f "${svc/topo-/topo_}.py" 2>/dev/null || true; }
    removed=1
  fi
done

# ── systemd units
if command -v systemctl >/dev/null 2>&1; then
  for svc in topo-server topo-agent; do
    if systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
      echo "removing ${svc}…"
      $SUDO systemctl disable --now "$svc" >/dev/null 2>&1 || true
      $SUDO rm -f "/etc/systemd/system/${svc}.service"
      removed=1
    fi
  done
  $SUDO systemctl daemon-reload 2>/dev/null || true
fi

if [ "$removed" -eq 1 ]; then
  echo "✓ removed. Repo + out/ data left in place."
  [ -f /etc/unraid-version ] && cat <<'EOT'

  Run the dashboard as a container instead — search "topographer" in Community
  Applications. Point its appdata at /mnt/user/appdata/topographer and your saved
  topologies and widgets carry over untouched.
EOT
else
  echo "nothing installed by this repo was found."
fi
exit 0
