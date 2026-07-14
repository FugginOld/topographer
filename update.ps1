# Git-free install / update for the dashboard SERVER on Windows. Fetches the
# latest source zip from GitHub, extracts the operational files (skips the
# dev-only tests/ and .github/) into $Dir, then starts the dashboard via
# server.ps1. Your saved topologies (out/) and config.yaml are gitignored, so
# they're not in the zip and your data survives the update.
#
#   fresh install / update (no git needed):
#     irm https://raw.githubusercontent.com/FugginOld/topographer/main/update.ps1 | iex
#   set a dir / port / token first (optional):
#     $env:TOPO_DIR="C:\topo"; $env:TOPO_PORT="9000"; $env:TOPO_TOKEN="secret"
$ErrorActionPreference = "Stop"

$Repo = if ($env:TOPO_REPO) { $env:TOPO_REPO } else { "https://github.com/FugginOld/topographer" }
$Dir  = if ($env:TOPO_DIR)  { $env:TOPO_DIR }  else { Join-Path $env:LOCALAPPDATA "topographer" }

$zip = Join-Path $env:TEMP "topo-server.zip"
$tmp = Join-Path $env:TEMP "topo-server-extract"
Write-Host "fetching latest server -> $Dir"
Invoke-WebRequest "$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive $zip $tmp -Force
$src = Join-Path $tmp "topographer-main"
# drop the dev-only tree before copying (operational files only)
Remove-Item (Join-Path $src "tests"), (Join-Path $src ".github") -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Copy-Item (Join-Path $src "*") $Dir -Recurse -Force   # zip lacks config.yaml/out, so your data survives
Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue

# start the dashboard: server.ps1 opens the firewall, frees the port, runs the server.
# hashtable splat binds -Port/-Token by name (an array splat binds positionally)
$p = @{}
if ($env:TOPO_PORT)  { $p['Port']  = [int]$env:TOPO_PORT }
if ($env:TOPO_TOKEN) { $p['Token'] = $env:TOPO_TOKEN }
& (Join-Path $Dir "server\server.ps1") @p
