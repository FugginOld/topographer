# HOWTO — Multi-Machine Topology Dashboard

Map the real hardware of every machine on your network and watch them live from
one dashboard.

- **One dashboard server** (a Linux host on your LAN) runs `topology_server.py`
  as a systemd service, stores every machine's topology, and shows the live HUD.
- **Each reporting machine** runs a small **agent** that scans its own hardware
  and pushes its topology + live telemetry to the server.

```text
   Windows PC ─┐
   Linux box  ─┼──►  http://<dashboard-ip>:8770   (topology_server.py service on the Linux host)
   Proxmox    ─┘      dashboard lists every host, live HUD per host
```

Nothing is installed globally; everything is Python 3 stdlib plus a few Linux
CLI tools. Data pushes **out** from each machine, so no inbound access to the
reporting machines is needed — only the server's port `8770` must be reachable.

---

## Part A — Set up the dashboard server (Linux)

Do this **once**, on the Linux host that will run the dashboard. `install.sh`
sets up a systemd service, so it starts on boot and restarts if it dies.

1. **Get the repo:**
   ```bash
   git clone https://github.com/FugginOld/topologygenerator.git ~/topologygenerator
   cd ~/topologygenerator
   ```

2. **Install:**
   ```bash
   ./install.sh
   # to require a shared token from agents:  TOPO_TOKEN=pick-a-secret ./install.sh   (see Part E)
   ```
   It installs any missing deps, writes + enables the service (filling in your
   user and paths), opens the firewall where present, and **prints your dashboard
   URL and the `TOPO_SERVER=…` line to give agents** — both use this host's own
   detected IP, so there's nothing to hardcode.

3. **Open the dashboard** at the URL it printed (e.g. `http://192.168.1.50:8770`,
   or `http://localhost:8770` on the server itself).
   - Click **SCAN NETWORK** to map the LAN; **GENERATE** adds this server's own hardware map.
   - Other machines appear automatically once their agents report (Part B/C).

**Update:** `git pull && ./install.sh`.  **Remove:** `./uninstall.sh`.

> Prefer to keep the dashboard on **Windows**? `.\server\server.ps1` still works (opens
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
curl -fsSL https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.sh | TOPO_SERVER=http://<dashboard-ip>:8770 bash
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

# 3. start reporting (the server URL is required — ./install.sh printed it)
./agent/report.sh http://<dashboard-ip>:8770             # name = hostname
./agent/report.sh http://<dashboard-ip>:8770 proxmox-b   # server + name
```

Leave it running — it pushes live telemetry every 3s and re-scans the topology
every 5 min. The machine now appears in the dashboard sidebar. To keep it running
across reboots, see **Part D**.

### Naming

The dashboard keeps **one card per name**, defaulting to the machine's
**hostname**. Re-running an agent **refreshes that card in place** (so you don't
pile up stale copies). But machines that share a hostname — common with cloned
Proxmox/VM templates (`localhost`, `debian`, `pve`…) — would **overwrite each
other's card**. Give each such machine a distinct name:

- Linux: `./agent/report.sh http://<dashboard-ip>:8770 NAME` or `TOPO_NAME=NAME ./agent/report.sh`
- Windows: `.\agent\report.ps1 -Name NAME`

Machines with already-unique hostnames need no name.

---

## Part C — Add a Windows reporting machine

### Fastest: one-line bootstrap (PowerShell)

In PowerShell (no admin needed). Fetches the repo (downloads a zip if git isn't
installed), ensures Python, and installs a persistent scheduled task that reports
at every logon — the Windows counterpart of Part B's one-liner:

```powershell
$env:TOPO_SERVER="http://<dashboard-ip>:8770"; irm https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.ps1 | iex
```

- **Name the card:** set `$env:TOPO_NAME="my-pc"` before the one-liner (defaults to hostname).
- **No Python?** It installs it via `winget`, then asks you to reopen PowerShell and re-run.
- **Remove later:** `.\agent\report.ps1 -Uninstall` (from the install dir, default `%LocalAppData%\topologygenerator`).

### Manual

```powershell
git clone https://github.com/FugginOld/topologygenerator.git
cd topologygenerator
.\agent\report.ps1 -Server http://<dashboard-ip>:8770            # run once (name = hostname)
.\agent\report.ps1 -Install -Server http://<dashboard-ip>:8770   # persist (scheduled task, see Part D)
```

The server URL is required (`-Server` or `$env:TOPO_SERVER`). If PowerShell blocks
the script, allow local scripts once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

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

### Windows — scheduled task

From the repo, run once in PowerShell:

```powershell
.\agent\report.ps1 -Install -Server http://<dashboard-ip>:8770
```

That registers a hidden scheduled task that reports at every logon (and restarts
if it dies), then starts it immediately. Remove it with `.\agent\report.ps1 -Uninstall`.

> Runs at **logon** — fine for a desktop you sign into. For an always-on /
> headless box that should report before anyone logs in, open Task Scheduler and
> change the **TopologyAgent** task's trigger to **At startup** with "Run whether
> user is logged on or not."

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
(Windows: `$env:TOPO_TOKEN = "pick-a-long-secret"` before `.\server\server.ps1`.)

**On each reporting machine**, provide the same token:
```bash
TOPO_TOKEN="pick-a-long-secret" ./agent/report.sh        # Linux
```
```powershell
$env:TOPO_TOKEN = "pick-a-long-secret"; .\agent\report.ps1   # Windows
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
curl http://<dashboard-ip>:8770/api/list      # should return JSON
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
python3 agent/topology_agent.py --server http://<dashboard-ip>:8770        # Linux
python agent/topology_agent.py --server http://<dashboard-ip>:8770         # Windows
```
Add `--report` (what `agent/report.sh`/`agent/report.ps1` do) to also stream live telemetry.

---

## Upgrading an existing install

The tooling now lives in subfolders (`scanners/`, `agent/`, `server/`), so paths
baked into old systemd units / launchers changed.

- **Dashboard server:** unaffected — it runs `renderers/html/topology_server.py`
  (unchanged). Just `cd ~/topologygenerator && git pull && ./install.sh` (or
  `sudo systemctl restart topology-server`).
- **Reporting machines:** the old agent unit / Unraid `go` line pointed at
  `report.sh` at the repo root, now `agent/report.sh`. **Re-run the bootstrap
  one-liner** on each machine (it regenerates the unit with the new path):
  ```bash
  curl -fsSL https://raw.githubusercontent.com/FugginOld/topologygenerator/main/bootstrap.sh | TOPO_SERVER=http://<dashboard-ip>:8770 bash
  ```
  New installs need nothing special.

---

## Reference — what runs where

| Machine | Runs | Command |
|---|---|---|
| **Server** (Linux) | dashboard + store | `./install.sh` → `topology-server` service |
| **Reporting (Linux)** | agent (push) | `./agent/report.sh` |
| **Reporting (Windows)** | agent (push) | `.\agent\report.ps1` |

| File | Purpose |
|---|---|
| `install.sh` / `uninstall.sh` | set up / remove the dashboard server as a Linux service |
| `systemd/topology-server.service` | the dashboard service unit (install.sh writes it for you) |
| `server/server.ps1` | start the dashboard on Windows (firewall + topology_server.py) |
| `renderers/html/topology_server.py` | dashboard server + ingest/telemetry API |
| `renderers/html/index.html` | the dashboard UI |
| `scanners/make_pc_topology.py` | Windows hardware scan |
| `scanners/make_linux_topology.py` | Linux hardware scan |
| `core/local_telemetry.py` | live CPU/net/disk/temp sampler (both OSes) |
| `agent/topology_agent.py` | push topology + telemetry to the server |
| `agent/report.sh` / `agent/report.ps1` | run the agent (self-updating) |
| `bootstrap.sh` | one-liner install — adapts to host (systemd / Unraid go / snapshot), git-free |
| `systemd/topology-agent.service` | persistent Linux reporting (bootstrap installs this for you) |

**Server port:** `8770` (change with `--port`; the dashboard reads the same host
it's served from, so no client config needed).
