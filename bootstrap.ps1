# Fresh-Windows one-liner: fetch the repo, ensure Python, and install the
# persistent reporting task. The Windows counterpart of bootstrap.sh. Run in
# PowerShell (no admin needed):
#
#   $env:TOPO_SERVER="http://DASHBOARD-IP:8770"; irm http://DASHBOARD-IP:8770/bootstrap.ps1 | iex
#
# Optional first:  $env:TOPO_NAME="my-pc"   (card name; defaults to hostname)
#                  $env:TOPO_DIR="C:\path"  (install dir; defaults to LocalAppData)
$ErrorActionPreference = "Stop"

$Server = $env:TOPO_SERVER
if (-not $Server) { Write-Error "set `$env:TOPO_SERVER=http://<dashboard-ip>:8770 first (the dashboard prints its URL on ./install.sh)"; return }
$Name = $env:TOPO_NAME
$Dir  = if ($env:TOPO_DIR) { $env:TOPO_DIR } else { Join-Path $env:LOCALAPPDATA "topographer" }

# --- fetch the agent files from the dashboard server (not github) ---
$zip = Join-Path $env:TEMP "topo-agent.zip"
$tmp = Join-Path $env:TEMP "topo-agent-extract"
Invoke-WebRequest ($Server.TrimEnd('/') + "/agent.zip") -OutFile $zip -UseBasicParsing
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive $zip $tmp -Force
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Copy-Item (Join-Path $tmp "*") $Dir -Recurse -Force   # the zip has no top-level dir
Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue

# --- ensure Python 3 ---
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host "installing Python via winget..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Write-Host "`nPython installed. CLOSE this window, open a NEW PowerShell, and re-run the same one-liner to finish."
    return
  }
  Write-Error "Python 3 not found. Install from https://python.org (tick 'Add python.exe to PATH'), then re-run."
  return
}

# --- install the persistent scheduled task (report.ps1 -Install) ---
# hashtable splat binds -Install/-Server by NAME (an array splat binds positionally)
$p = @{ Install = $true; Server = $Server }
if ($Name) { $p['Name'] = $Name }
& (Join-Path $Dir "agent\report.ps1") @p
Write-Host "installed from $Dir"
