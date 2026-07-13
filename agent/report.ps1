# Run on a WINDOWS reporting machine. Pushes this host's topology + live
# telemetry to the dashboard server. Self-updates from git on each start.
#
#   .\report.ps1 -Server http://host:8770              # run once (name = hostname)
#   .\report.ps1 -Install -Server http://host:8770     # persist: scheduled task, runs at logon
#   .\report.ps1 -Uninstall                            # remove the scheduled task
#   .\report.ps1 -Server http://host:8770 -Name node-a # custom card name
#   $env:TOPO_SERVER="http://host:8770"; .\report.ps1  # server via env
#   $env:TOPO_TOKEN="secret"; .\report.ps1             # if the server sets a shared token
#
# The server URL is required (-Server or $env:TOPO_SERVER) — ./install.sh prints
# it. The dashboard keeps ONE card per name (defaults to this host's hostname), so
# give machines that share a hostname distinct names or one overwrites the other.
#
# Requires Python 3 on PATH.
param(
  [string]$Server = $env:TOPO_SERVER,
  [string]$Name = $env:TOPO_NAME,
  [switch]$Install,
  [switch]$Uninstall
)

$TaskName = "TopologyAgent"

# -Uninstall: drop the scheduled task and exit (no server needed).
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "removed scheduled task '$TaskName'"
  exit 0
}

# -Install: register a scheduled task that runs THIS script at logon (hidden,
# execution-policy-bypassed, restarts if it dies), then start it now.
if ($Install) {
  if (-not $Server) { Write-Error "set -Server http://<dashboard-ip>:8770"; exit 1 }
  $arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Server $Server"
  if ($Name) { $arg += " -Name $Name" }
  $action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "OK: '$TaskName' runs at logon, reporting to $Server."
  Write-Host "    remove with:  .\agent\report.ps1 -Uninstall"
  exit 0
}

if (-not $Server) { Write-Error "set -Server http://<dashboard-ip>:8770 (or `$env:TOPO_SERVER)"; exit 1 }
Set-Location $PSScriptRoot

git pull --ff-only 2>$null

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "Python 3 not found on PATH - install it and re-run."; exit 1 }

$argv = @("topology_agent.py", "--server", $Server, "--report")
$suffix = ""
if ($Name) { $argv += @("--name", $Name); $suffix = " as '$Name'" }
Write-Host "reporting to $Server$suffix  (Ctrl-C to stop)"
& $py.Source @argv
