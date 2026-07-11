#!/usr/bin/env bash
# Run on a LINUX reporting machine. Keeps this host's topology + live telemetry
# flowing to the dashboard server. Self-updates from git on each start.
#
#   ./report.sh                              # -> default server, name = hostname
#   ./report.sh http://host:8770             # -> override server
#   ./report.sh http://host:8770 node-a      # -> override server AND name
#   TOPO_NAME=node-a ./report.sh             # -> just set the name
#   TOPO_TOKEN=secret ./report.sh            # -> if the server sets a shared token
#
# The dashboard keeps ONE card per name (defaults to this host's hostname), so
# give machines that share a hostname distinct names or one overwrites the other.
#
# Uses only Python 3 stdlib; the Linux collector needs lspci/lsblk (+ dmidecode
# as root for per-DIMM detail). Install: apt-get install pciutils util-linux dmidecode
set -euo pipefail

SERVER="${1:-${TOPO_SERVER:-http://192.168.1.225:8770}}"
NAME="${2:-${TOPO_NAME:-}}"
cd "$(dirname "$(readlink -f "$0")")"

git pull --ff-only >/dev/null 2>&1 || echo "warn: git pull skipped (offline or local changes)"
for t in lspci lsblk; do
  command -v "$t" >/dev/null 2>&1 || echo "warn: '$t' not found — some devices will be missing (apt-get install pciutils util-linux)"
done

ARGS=(--server "$SERVER" --report)
[ -n "$NAME" ] && ARGS+=(--name "$NAME")
echo "reporting to $SERVER${NAME:+ as '$NAME'}  (Ctrl-C to stop)"
exec python3 agent.py "${ARGS[@]}"
