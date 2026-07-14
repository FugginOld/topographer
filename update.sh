#!/usr/bin/env bash
# Git-free install / update for the dashboard SERVER. Fetches the latest source
# tarball from GitHub, extracts it into $DIR, then runs install.sh to (re)install
# and restart the systemd service. Your saved topologies (out/) and config.yaml
# are gitignored, so they're never in the tarball and survive the extract.
#
#   fresh install (no git needed):
#     curl -fsSL https://raw.githubusercontent.com/FugginOld/topographer/main/update.sh | bash
#   update an existing install (or re-run the one-liner):
#     ~/topographer/update.sh
#   options pass straight through to install.sh, e.g.:
#     TOPO_TOKEN=secret ~/topographer/update.sh
set -euo pipefail

REPO="${TOPO_REPO:-https://github.com/FugginOld/topographer}"
TARURL="${REPO%.git}/archive/refs/heads/main.tar.gz"
DIR="${TOPO_DIR:-$HOME/topographer}"

# Extract only the files needed to RUN the server + serve the client — skip the
# dev-only tree (tests, CI). out/ and config.yaml are gitignored so they're not
# in the tarball to begin with; your data is untouched.
echo "fetching latest server -> $DIR"
mkdir -p "$DIR"
if command -v curl >/dev/null 2>&1; then curl -fsSL "$TARURL"; else wget -qO- "$TARURL"; fi \
  | tar xz -C "$DIR" --strip-components=1 --exclude='*/tests' --exclude='*/.github'
exec bash "$DIR/install.sh"
