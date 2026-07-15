# Run on a WINDOWS reporting machine. Pushes this host's topology + live
# telemetry to the dashboard server. Self-updates from git on each start.
#
#   .\report.ps1 -Server http://host:8770              # run once (name = hostname)
#   .\report.ps1 -Install -Server http://host:8770     # persist: background task at logon (no window)
#   .\report.ps1 -Uninstall                            # remove the scheduled task
#   .\report.ps1 -Server http://host:8770 -Name node-a # custom card name
#   $env:TOPO_SERVER="http://host:8770"; .\report.ps1  # server via env
#   $env:TOPO_TOKEN="secret"; .\report.ps1             # if the server sets a shared token
#
# The server URL is required (-Server or $env:TOPO_SERVER) - ./install.sh prints
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

$VbsPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'TopologyAgent.vbs'

# -Uninstall: drop the scheduled task AND the Startup launcher (no server needed).
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Remove-Item $VbsPath -ErrorAction SilentlyContinue
  Write-Host "removed scheduled task + Startup launcher"
  exit 0
}

# -Install: persist reporting at logon. Prefer a scheduled task (restarts on
# failure); it needs admin, so fall back to a hidden Startup-folder launcher
# (no admin) when we can't register one. Then start reporting now.
if ($Install) {
  if (-not $Server) { Write-Error "set -Server http://<dashboard-ip>:8770"; exit 1 }

  # Launch the agent with pythonw.exe (GUI subsystem -> NEVER allocates a console),
  # invoked directly with no powershell wrapper, so there is guaranteed NO window.
  # topo_agent.py finds the repo via __file__ and uses sys.executable, so its children
  # (generator, service probe) also run under pythonw. All cwd-independent.
  $pyw = Get-Command pythonw -ErrorAction SilentlyContinue
  if (-not $pyw) { $pyw = Get-Command pyw -ErrorAction SilentlyContinue }
  if ($pyw) { $exe = $pyw.Source }
  else { $p = Get-Command python -ErrorAction SilentlyContinue; $exe = if ($p) { $p.Source } else { "pythonw.exe" } }
  $script = Join-Path $PSScriptRoot "topo_agent.py"
  $agentArgs = "`"$script`" --server $Server --report"
  if ($Name) { $agentArgs += " --name $Name" }

  try {
    $action  = New-ScheduledTaskAction -Execute $exe -Argument $agentArgs -WorkingDirectory $PSScriptRoot
    # At startup (headless boxes report before anyone logs in) AND at logon (desktops).
    # MultipleInstances defaults to IgnoreNew, so the two triggers won't double-start it.
    $trigger = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
               -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    # S4U: run whether logged on or not, in the background (session 0) - no stored password.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set -Principal $principal -Force -ErrorAction Stop | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Remove-Item $VbsPath -ErrorAction SilentlyContinue   # drop any old fallback
    Write-Host "OK: background task '$TaskName' runs pythonw at startup + logon (no window, restarts on failure), reporting to $Server."
  } catch {
    # No admin / S4U blocked: hidden Startup launcher. wscript + Run(...,0) has no window,
    # and pythonw is windowless too.
    $cmd = '"' + $exe + '" ' + $agentArgs
    'CreateObject("WScript.Shell").Run "' + ($cmd -replace '"', '""') + '", 0, False' |
      Set-Content -Path $VbsPath -Encoding ASCII
    Start-Process wscript.exe -ArgumentList "`"$VbsPath`""   # start now, no window
    Write-Host "OK: hidden Startup launcher installed (no admin, no window), reporting to $Server at each logon."
    Write-Host "    for auto-restart-on-failure, re-run in an ADMIN PowerShell to use a scheduled task."
  }
  Write-Host "    remove with:  .\agent\report.ps1 -Uninstall"
  exit 0
}

if (-not $Server) { Write-Error "set -Server http://<dashboard-ip>:8770 (or `$env:TOPO_SERVER)"; exit 1 }
Set-Location $PSScriptRoot

git pull --ff-only 2>$null

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "Python 3 not found on PATH - install it and re-run."; exit 1 }

$argv = @("topo_agent.py", "--server", $Server, "--report")
$suffix = ""
if ($Name) { $argv += @("--name", $Name); $suffix = " as '$Name'" }
Write-Host "reporting to $Server$suffix  (Ctrl-C to stop)"
& $py.Source @argv
