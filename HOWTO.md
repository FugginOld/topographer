# HOWTO — Multi-Machine Topology Dashboard

Map the real hardware of every machine on your network and watch them live from
one dashboard.

- **One dashboard server** (a Linux box, `192.168.1.225`) runs `topology_server.py`
  as a systemd service, stores every machine's topology, and shows the live HUD.
- **Each reporting machine** runs a small **agent** that scans its own hardware
  and pushes its topology + live telemetry to the server.

```text
   Windows PC ─┐
   Linux box  ─┼──►  http://192.168.1.225:8770   (topology_server.py service on the Linux host)
   Proxmox    ─┘      dashboard lists every host, live HUD per host
```

Nothing is installed globally; everything is Python 3 stdlib plus a few Linux
CLI tools. Data pushes **out** from each machine, so no inbound access to the
reporting machines is needed — only the server's port `8770` must be reachable.

---

## Part A — Set up the dashboard server (Linux, 192.168.1.225)

Do this **once**, on the Linux host that will run the dashboard. It runs as a
systemd service, so it starts on boot and restarts if it dies.

1. **Install Python 3 + git** (only PyYAML is a Python dep; the dashboard itself
   is stdlib). On Debian/Ubuntu:
   ```bash
   sudo apt-get update && sudo apt-get install -y python3 python3-yaml git
   ```

2. **Get the repo** (into your home dir):
   ```bash
   git clone https://github.com/FugginOld/topologygenerator.git ~/topologygenerator
   ```

3. **Install the service** — edit `User=` and the two paths to match this host,
   then enable it:
   ```bash
   cd ~/topologygenerator
   sed -e "s#/home/YOUR_USER/topologygenerator#$HOME/topologygenerator#g" -e "s/YOUR_USER/$USER/g" \
       systemd/topology-server.service | sudo tee /etc/systemd/system/topology-server.service >/dev/null
   sudo systemctl daemon-reload
   sudo systemctl enable --now topology-server
   sudo systemctl status topology-server        # should be "active (running)"
   journalctl -u topology-server -f             # watch its logs
   ```
   > The `sed` fills in your user + home path automatically; edit the unit by hand
   > if the repo lives elsewhere. To require a token from agents, uncomment the
   > `Environment=TOPO_TOKEN=…` line in the unit before enabling (see **Part E**).

4. **Open the firewall** for port `8770` (only if this host runs one):
   ```bash
   sudo ufw allow 8770/tcp        # ufw
   # or firewalld:  sudo firewall-cmd --permanent --add-port=8770/tcp && sudo firewall-cmd --reload
   ```

5. **Open the dashboard:** `http://192.168.1.225:8770` (or `http://localhost:8770`
   on the server itself).
   - Click **SCAN NETWORK** to map the LAN; **GENERATE** adds this server's own hardware map.
   - Other machines appear automatically once their agents report (Part B/C).

**To update later:** `cd ~/topologygenerator && git pull && sudo systemctl restart topology-server`.

> Prefer to keep the dashboard on **Windows**? `.\server.ps1` still works (opens
> the firewall + runs the server); make it persistent via Task Scheduler like the
> agent in **Part D**.

---

## Part B — Add a Linux reporting machine

### Fastest: one-line bootstrap

Fetches the repo (git **or** `curl`+`tar`), installs only the tools this host is
actually missing, and sets up **background reporting that survives reboots** — it
adapts to the host: a **systemd service** on most Linux, the boot **`go` script**
on **Unraid**, or foreground where neither is available. Same command everywhere:

```bash
curl -fsSL https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.sh | TOPO_SERVER=http://192.168.1.225:8770 bash
```

- **Name the card:** prepend `TOPO_NAME=proxmox-b` (see **Naming**). Defaults to the hostname.
- **One-time snapshot** (laptop/PC, no persistent service): add `TOPO_ONCE=1`.
- **Unraid:** clones to `/mnt/user/appdata`, persists via `/boot/config/go`; needs `python3` (no git — no NerdTools required).
- Prompts once for `sudo` where needed; runs directly if you're already root.

> Requires the GitHub repo to be **public**. If it's private, use the manual
> steps below with `git clone` and your credentials.
>
> **Tip:** from the dashboard's network map (**SCAN NETWORK**), right-click any
> host → *Generate machine topology* to get this exact command pre-filled for
> that box (or have the server SSH-scan it directly, if `remote_scan` is set).

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

> The **bootstrap one-liner already does this** (systemd on Linux, `go` script on
> Unraid). Use the steps below only for a hand-built install, or on Windows.

### Linux — systemd service (manual)

Bootstrap generates this unit for you; to do it by hand:

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

**On the server**, set the token in the service. Uncomment/edit the
`Environment=TOPO_TOKEN=…` line in `/etc/systemd/system/topology-server.service`,
then:
```bash
sudo systemctl daemon-reload && sudo systemctl restart topology-server
```
(Windows: `$env:TOPO_TOKEN = "pick-a-long-secret"` before `.\server.ps1`.)

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
If it hangs/refuses: the service isn't running
(`sudo systemctl status topology-server`), the firewall port (Part A.4) is
closed, or the IP is wrong.

**HUD stuck at zeros on the server itself?** You're likely viewing an old build.
`sudo systemctl restart topology-server` (or on Windows, Ctrl-C + rerun), then
hard-reload the page (Ctrl-F5).

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
| **Server** (Linux, 192.168.1.225) | dashboard + store | `topology-server` systemd service |
| **Reporting (Linux)** | agent (push) | `./report.sh` |
| **Reporting (Windows)** | agent (push) | `.\report.ps1` |

| File | Purpose |
|---|---|
| `systemd/topology-server.service` | run the dashboard as a persistent Linux service |
| `server.ps1` | start the dashboard on Windows (firewall + topology_server.py) |
| `renderers/html/topology_server.py` | dashboard server + ingest/telemetry API |
| `renderers/html/index.html` | the dashboard UI |
| `make_pc_topology.py` | Windows hardware scan |
| `make_linux_topology.py` | Linux hardware scan |
| `local_telemetry.py` | live CPU/net/disk/temp sampler (both OSes) |
| `topology_agent.py` | push topology + telemetry to the server |
| `report.sh` / `report.ps1` | run the agent (self-updating) |
| `bootstrap.sh` | one-liner install — adapts to host (systemd / Unraid go / snapshot), git-free |
| `systemd/topology-agent.service` | persistent Linux reporting (bootstrap installs this for you) |

**Server port:** `8770` (change with `--port`; the dashboard reads the same host
it's served from, so no client config needed).
