# Run on a WINDOWS reporting machine. Pushes this host's topology + live
# telemetry to the dashboard server. Self-updates from git on each start.
#
#   .\report.ps1                                   # default server, name = hostname
#   .\report.ps1 -Server http://host:8770          # override server
#   .\report.ps1 -Name node-a                      # override the card name
#   $env:TOPO_TOKEN="secret"; .\report.ps1         # if the server sets a shared token
#
# The dashboard keeps ONE card per name (defaults to this host's hostname), so
# give machines that share a hostname distinct names or one overwrites the other.
#
# Requires Python 3 on PATH. If PowerShell blocks the script, run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
param(
  [string]$Server = $(if ($env:TOPO_SERVER) { $env:TOPO_SERVER } else { "http://192.168.1.225:8770" }),
  [string]$Name = $env:TOPO_NAME
)
Set-Location $PSScriptRoot

git pull --ff-only 2>$null

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "Python 3 not found on PATH — install it and re-run."; exit 1 }

$argv = @("agent.py", "--server", $Server, "--report")
if ($Name) { $argv += @("--name", $Name) }
Write-Host "reporting to $Server$(if ($Name) { " as '$Name'" })  (Ctrl-C to stop)"
& $py.Source @argv
