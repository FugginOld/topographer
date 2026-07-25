#!/usr/bin/env bash
# uninstall.sh must remove exactly its own two lines from the Unraid boot 'go'
# script and nothing else — a wrong sed here corrupts the array's boot config.
# Runs against a fake flash dir via TOPO_BOOT; touches nothing real.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/go" <<'EOF'
#!/bin/bash
# Start the Management Utility
/usr/local/sbin/emhttp &

# someone else's thing
bash /boot/config/plugins/other/start.sh &

# topo-server (install.sh)
bash /boot/config/topo-server.sh >/var/log/topo-server.log 2>&1 &

# topo-agent (bootstrap)
bash /boot/config/topo-agent.sh >/var/log/topo-agent.log 2>&1 &
EOF
touch "$TMP/topo-server.sh" "$TMP/topo-agent.sh"

TOPO_BOOT="$TMP" bash "$ROOT/uninstall.sh" >/dev/null

grep -q "emhttp" "$TMP/go"                 || { echo "FAIL: ate the emhttp line"; exit 1; }
grep -q "plugins/other/start.sh" "$TMP/go" || { echo "FAIL: ate another plugin's hook"; exit 1; }
grep -q "topo" "$TMP/go"                   && { echo "FAIL: left a topo line behind"; cat "$TMP/go"; exit 1; }
[ -f "$TMP/topo-server.sh" ]               && { echo "FAIL: launcher not deleted"; exit 1; }
[ -f "$TMP/topo-agent.sh" ]                && { echo "FAIL: agent launcher not deleted"; exit 1; }

# idempotent: a second run finds nothing and still exits clean
out="$(TOPO_BOOT="$TMP" bash "$ROOT/uninstall.sh")"
case "$out" in *"nothing installed"*) ;; *) echo "FAIL: not idempotent: $out"; exit 1;; esac

# a flash dir with no go script at all must not error
rm -f "$TMP/go"
TOPO_BOOT="$TMP" bash "$ROOT/uninstall.sh" >/dev/null

echo "uninstall.sh Unraid self-check ok"
