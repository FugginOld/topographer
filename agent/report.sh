#!/usr/bin/env bash
# Run on a LINUX reporting machine. Keeps this host's topology + live telemetry
# flowing to the dashboard server. Self-updates from git if run from a full
# checkout; a bootstrap agent install is a minimal file set — re-run bootstrap to update.
#
#   ./report.sh http://host:8770             # -> server (name = hostname)
#   ./report.sh http://host:8770 node-a      # -> server AND name
#   TOPO_SERVER=http://host:8770 ./report.sh # -> server via env, name = hostname
#   TOPO_TOKEN=secret ./report.sh …          # -> if the server sets a shared token
#
# The server URL is required (arg or TOPO_SERVER) — ./install.sh prints it. The
# dashboard keeps ONE card per name (defaults to this host's hostname), so give
# machines that share a hostname distinct names or one overwrites the other.
#
# Uses only Python 3 stdlib; the Linux collector needs lspci/lsblk (+ dmidecode
# as root for per-DIMM detail). Install: apt-get install pciutils util-linux dmidecode
set -euo pipefail

SERVER="${1:-${TOPO_SERVER:-}}"
NAME="${2:-${TOPO_NAME:-}}"
if [ -z "$SERVER" ]; then
  echo "usage: ./report.sh http://<dashboard-ip>:8770 [name]   (or set TOPO_SERVER)" >&2
  exit 1
fi
cd "$(dirname "$(readlink -f "$0")")"   # -> agent/ ; repo root is ..

# self-update only when this is a full git checkout; a bootstrap agent install is
# a minimal file set (no .git) — update those by re-running bootstrap.
if [ -d ../.git ]; then
  git -C .. pull --ff-only >/dev/null 2>&1 || echo "warn: git pull skipped (offline or local changes)"
fi
for t in lspci lsblk; do
  command -v "$t" >/dev/null 2>&1 || echo "warn: '$t' not found — some devices will be missing (apt-get install pciutils util-linux)"
done

ARGS=(--server "$SERVER" --report)
[ -n "$NAME" ] && ARGS+=(--name "$NAME")
echo "reporting to $SERVER${NAME:+ as '$NAME'}  (Ctrl-C to stop)"
exec python3 topo_agent.py "${ARGS[@]}"
