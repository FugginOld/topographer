# Start the topology dashboard server: ensures the firewall rule, prints the
# LAN address to give reporting machines, and runs serve.py.
#
#   .\server.ps1                       # port 8770
#   .\server.ps1 -Port 9000            # different port
#   .\server.ps1 -Token "secret"       # require a shared token from agents
#
# Adding the firewall rule needs Administrator the first time; if not elevated
# it prints the one command to run once and starts the server anyway.
param(
  [int]$Port = 8770,
  [string]$Token = $env:TOPO_TOKEN
)
Set-Location $PSScriptRoot

# --- firewall: allow inbound TCP $Port so other machines can reach the dashboard ---
$ruleName = "Topology dashboard $Port"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
  try {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -LocalPort $Port `
      -Protocol TCP -Action Allow -ErrorAction Stop | Out-Null
    Write-Host "firewall: opened inbound TCP $Port"
  } catch {
    Write-Warning "could not add the firewall rule (needs Administrator)."
    Write-Warning "Reporting machines may not reach the dashboard until you run, in an elevated PowerShell:"
    Write-Warning "  New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -LocalPort $Port -Protocol TCP -Action Allow"
  }
} else {
  Write-Host "firewall: TCP $Port already allowed"
}

# --- optional shared secret passed to serve.py via the environment ---
if ($Token) { $env:TOPO_TOKEN = $Token; Write-Host "ingest token: enabled" }

# --- free the port: an old serve.py still holding it would keep serving stale code ---
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
  $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
  if ($proc -and $proc.ProcessName -in @('python', 'py', 'pythonw')) {
    Write-Host "stopping old server (pid $($proc.Id)) holding port $Port"
    Stop-Process -Id $proc.Id -Force
    Start-Sleep -Milliseconds 600
  } elseif ($proc) {
    Write-Error "port $Port is in use by '$($proc.ProcessName)' (pid $($proc.Id)). Stop it, or run with -Port <other>."
    exit 1
  }
}

# --- locate Python 3 ---
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Error "Python 3 not found on PATH - install it and re-run."; exit 1 }

# --- show the address(es) to hand to reporting machines ---
$ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' }).IPAddress
Write-Host ""
Write-Host "dashboard : http://localhost:$Port"
foreach ($ip in $ips) { Write-Host "agents -> : http://$($ip):$Port" }
Write-Host ""

& $py.Source renderers\html\serve.py --port $Port
