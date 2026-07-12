# HOWTO — Multi-Machine Topology Dashboard

Map the real hardware of every machine on your network and watch them live from
one dashboard.

- **One dashboard server** (your Windows box, `192.168.1.225`) runs `topology_server.py`,
  stores every machine's topology, and shows the live HUD.
- **Each reporting machine** runs a small **agent** that scans its own hardware
  and pushes its topology + live telemetry to the server.

```
   Windows PC ─┐
   Linux box  ─┼──►  http://192.168.1.225:8770   (topology_server.py on the Windows server)
   Debian srv ─┘      dashboard lists every host, live HUD per host
```

Nothing is installed globally; everything is Python 3 stdlib plus a few Linux
CLI tools. Data pushes **out** from each machine, so no inbound access to the
reporting machines is needed — only the server's port `8770` must be reachable.

---

## Part A — Set up the dashboard server (Windows, 192.168.1.225)

Do this **once**, on the machine that will host the dashboard.

1. **Install Python 3** (if not already): https://www.python.org/downloads/
   During install, tick **"Add python.exe to PATH"**. Verify:
   ```powershell
   python --version
   ```

2. **Get the repo:**
   ```powershell
   git clone https://github.com/FugginOld/topologygenerator.git
   cd topologygenerator
   ```

3. **Start the server** — the launcher opens the firewall, prints the address to
   give reporting machines, and runs the server. Run it from an **Administrator**
   PowerShell the first time so it can add the firewall rule (after that, a normal
   window is fine). Leave the window open.

   ```powershell
   .\server.ps1
   ```

   Prefer to do it by hand? Run this once as Administrator, then start the server
   any time:

   ```powershell
   New-NetFirewallRule -DisplayName "Topology dashboard" -Direction Inbound -LocalPort 8770 -Protocol TCP -Action Allow
   python renderers\html\topology_server.py
   ```

4. **Open the dashboard:** http://localhost:8770
   - Click **GENERATE** to add *this* server's own hardware map.
   - Other machines will appear automatically once their agents report (Part B/C).

> The server can also report itself like any other host — but you don't need to;
> selecting its locally-generated topology shows the server's own live HUD.

---

## Part B — Add a Linux reporting machine

### Fastest: one-line bootstrap (fresh Debian/Ubuntu)

Installs dependencies, clones the repo, and starts reporting in one shot:

```bash
curl -fsSL https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.sh | bash
```

To label this machine's card (recommended — see **Naming** below), set
`TOPO_NAME` before the pipe:

```bash
curl -fsSL https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.sh | TOPO_NAME=proxmox-b bash
```

> Requires the GitHub repo to be **public**. If it's private, use the manual
> steps below with `git clone` and your credentials.

### Manual (any Linux)

```bash
# 1. dependencies (Debian/Ubuntu; adjust for your distro). prefix with sudo if not root
apt-get update
apt-get install -y git python3 pciutils util-linux dmidecode

# 2. get the repo
git clone https://github.com/FugginOld/topologygenerator.git
cd topologygenerator

# 3. start reporting to the server (optionally with a name)
./report.sh                                       # name = hostname
./report.sh http://192.168.1.225:8770 proxmox-b   # server + name
```

`report.sh` defaults to `http://192.168.1.225:8770`. Leave it running — it pushes
live telemetry every 3s and re-scans the topology every 5 min. The machine now
appears in the dashboard sidebar. To keep it running across reboots, see
**Part D**.

### Naming

The dashboard keeps **one card per name**, defaulting to the machine's
**hostname**. Re-running an agent **refreshes that card in place** (so you don't
pile up stale copies). But machines that share a hostname — common with cloned
Proxmox/VM templates (`localhost`, `debian`, `pve`…) — would **overwrite each
other's card**. Give each such machine a distinct name:

- Linux: `./report.sh http://192.168.1.225:8770 NAME` or `TOPO_NAME=NAME ./report.sh`
- Windows: `.\report.ps1 -Name NAME`

Machines with already-unique hostnames need no name.

---

## Part C — Add a Windows reporting machine

1. **Install Python 3** (tick "Add to PATH") and **Git**.

2. **Clone and run:**
   ```powershell
   git clone https://github.com/FugginOld/topologygenerator.git
   cd topologygenerator
   .\report.ps1                       # name = hostname
   .\report.ps1 -Name workstation-1   # custom card name (see Naming, Part B)
   ```

   If PowerShell blocks the script, allow local scripts once:
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```

`report.ps1` defaults to `http://192.168.1.225:8770`; override with
`.\report.ps1 -Server http://OTHER-HOST:8770`. Leave it running; see **Part D**
to make it persistent.

---

## Part D — Keep reporting across reboots

### Linux — systemd service

```bash
# edit User= and the two paths to match your machine
nano systemd/topology-agent.service

cp systemd/topology-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now topology-agent

# watch it
journalctl -u topology-agent -f
```

### Windows — Task Scheduler

Create a task that runs at logon:

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-WindowStyle Hidden -File `"$PWD\report.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "TopologyAgent" -Action $action -Trigger $trigger
```

---

## Part E — (Optional) Lock down who can report

By default any machine that can reach the server may push. To require a shared
secret:

**On the server**, set the token before starting `topology_server.py`:
```powershell
$env:TOPO_TOKEN = "pick-a-long-secret"
python renderers\html\topology_server.py
```

**On each reporting machine**, provide the same token:
```bash
TOPO_TOKEN="pick-a-long-secret" ./report.sh        # Linux
```
```powershell
$env:TOPO_TOKEN = "pick-a-long-secret"; .\report.ps1   # Windows
```

Pushes without a matching token get `403`.

---

## Part F — Verify & troubleshoot

**Is a machine reporting?** It shows in the sidebar; its HUD shows live CPU/NET.
If a machine's topology is listed but the HUD says **OFFLINE**, the topology was
pushed but the agent isn't currently sending telemetry (agent stopped, or only a
one-shot push was done).

**Can't reach the server from a reporting machine?**
```bash
curl http://192.168.1.225:8770/api/list      # should return JSON
```
If it hangs/refuses: the server isn't running, or the firewall rule (Part A.3)
is missing, or the IP is wrong.

**HUD stuck at zeros on the server itself?** You're likely viewing an old
`topology_server.py`. Stop it (Ctrl-C) and restart, then hard-reload the page (Ctrl-F5).

**Linux map missing devices?** Install the collectors:
`apt-get install pciutils util-linux dmidecode`. Per-DIMM RAM detail needs
`dmidecode`, which reads DMI as root (the systemd service runs as your user —
run it as root, or accept the `/proc/meminfo` total-only fallback).

**Real CPU temperature:** available on **Linux** (`/sys/class/hwmon`), shown as
`CPU 34% · 52°C`. **Windows** can't expose CPU temp without an elevated
vendor/driver, so only `CPU %` shows there.

**A one-shot push (topology only, no live telemetry):**
```bash
python3 topology_agent.py --server http://192.168.1.225:8770        # Linux
python topology_agent.py --server http://192.168.1.225:8770         # Windows
```
Add `--report` (what `report.sh`/`report.ps1` do) to also stream live telemetry.

---

## Reference — what runs where

| Machine | Runs | Command |
|---|---|---|
| **Server** (192.168.1.225) | dashboard + store | `python renderers\html\topology_server.py` |
| **Reporting (Linux)** | agent (push) | `./report.sh` |
| **Reporting (Windows)** | agent (push) | `.\report.ps1` |

| File | Purpose |
|---|---|
| `server.ps1` | start the dashboard (firewall + topology_server.py) on Windows |
| `renderers/html/topology_server.py` | dashboard server + ingest/telemetry API |
| `renderers/html/index.html` | the dashboard UI |
| `make_pc_topology.py` | Windows hardware scan |
| `make_linux_topology.py` | Linux hardware scan |
| `local_telemetry.py` | live CPU/net/disk/temp sampler (both OSes) |
| `topology_agent.py` | push topology + telemetry to the server |
| `report.sh` / `report.ps1` | run the agent (self-updating) |
| `bootstrap.sh` | fresh-Debian one-liner |
| `systemd/topology-agent.service` | persistent Linux reporting |

**Server port:** `8770` (change with `--port`; the dashboard reads the same host
it's served from, so no client config needed).
