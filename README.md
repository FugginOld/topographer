# topologygenerator

[![CI](https://github.com/FugginOld/topologygenerator/actions/workflows/ci.yml/badge.svg)](https://github.com/FugginOld/topologygenerator/actions/workflows/ci.yml)

Two topology tools in one repo:

1. **Network topology** — auto-discover a homelab network from live sources,
   normalize into one canonical model, render as an animated HTML dashboard,
   Graphviz SVG, or git-diffable Mermaid. *(the original pipeline, below)*
2. **Hardware topology + fleet dashboard** — scan each machine's real hardware
   fabric (CPU, RAM, PCIe lanes, NVMe, NICs with link state, USB, displays) and
   watch every machine on your network from **one live dashboard**, with per-host
   CPU/net/disk telemetry. *See **[HOWTO.md](HOWTO.md)** for the full setup.*

The repo is the **tooling**. Your actual topology (IPs, MACs, hostnames, VLAN
policy) is a *build artifact* and is gitignored — see [Security](#security).

## Hardware topology + fleet dashboard

Map the real hardware of every machine on your network and watch them live:

```
   Windows PC ─┐  ./report.sh  (or report.ps1)
   Linux box  ─┼──►  topology_server.py on one host  ──►  live dashboard, one card per machine
   Proxmox    ─┘     (POST /api/ingest + telemetry)
```

- One **server** runs `python renderers/html/topology_server.py`; open `http://HOST:8770`.
- Each machine runs an **agent** that scans its own hardware and pushes its
  topology and live telemetry (`report.sh` on Linux, `report.ps1` on Windows, or
  the `bootstrap.sh` one-liner for a fresh Debian box).
- Scanners: `make_pc_topology.py` (Windows, PnP/CIM) and `make_linux_topology.py`
  (Linux, sysfs/proc). Live metrics: `telemetry.py` (real CPU temp on Linux).

Full step-by-step — server firewall, each reporting machine, persistence,
naming, tokens, troubleshooting — is in **[HOWTO.md](HOWTO.md)**.

## How it works

```
sources ──▶ collectors ──▶ topology.json ──▶ renderers
(live)      (read-only)     (canonical)       (html / svg / mermaid)
```

Every collector is read-only and emits raw dicts. `core/normalize.py` merges
them into one `Topology` (deduped by MAC, then reconciled by IP and hostname),
`core/enrich.py` adds vendor/kind, and renderers consume only the canonical
model — so you can add outputs without touching collection.

## Quick start

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml     # edit — this file is gitignored
python make_network_topology.py --config config.yaml
python renderers/html/topology_server.py         # http://localhost:8770
```

With nothing configured but `arpscan` (the default), you already get a live host
map. Enable more collectors in `config.yaml` as you go.

## Collectors

| Collector    | Source                          | Adds                                    | Needs                |
|--------------|---------------------------------|-----------------------------------------|----------------------|
| `arpscan`    | `arp-scan` / `nmap -sn`         | live hosts, MAC, vendor                 | arp-scan or nmap     |
| `opnsense`   | OPNsense REST API               | **VLAN zones**, DHCP names, ARP         | API key/secret       |
| `unifi_snmp` | SNMP LLDP + FDB                 | **physical switch-port edges**, uplinks | net-snmp, SNMP on sw |
| `tailscale`  | `tailscale status --json`       | overlay mesh (tagged `ts`)              | tailscale            |
| `docker`     | `docker ps` over SSH            | containers nested under hosts           | key-based SSH        |
| `dns`        | Pi-hole / hosts file            | friendly names                          | —                    |

MAC is the primary join key; nodes are then reconciled by IP and hostname so a
host seen by three collectors collapses into one.

## Renderers

- **`renderers/html/`** — animated dashboard (VLAN zones, firewall hub, Tailscale
  overlay toggle, click-to-isolate). Reads `topology.json`; refreshes every 30s.
- **`renderers/static_svg.py`** — Graphviz `dot` → SVG/PNG for README/wiki.
- **`renderers/mermaid.py`** — `topology.mmd`, renders on GitHub, diffs cleanly.

## Automation

Runs on a homelab host, not GitHub Actions (Actions can't reach your LAN). See
`systemd/` for a timer that regenerates the map every 10 minutes. If you want
history/diffing, snapshot each `topology.json` into SQLite and diff runs.

## Security

Your generated topology is a map of your network. **Keep it out of git.**
`.gitignore` already excludes `config.yaml`, `out/`, `*.raw.json`, and
`topology.json`. Recommended: keep this repo **private**. If public, only ever
commit the tooling and `config.example.yaml` (dummy values). One accidental
`git add -A` of a build artifact leaks the whole layout.

## Layout

```
collectors/   read-only network source adapters (one file per source)
core/         schema.py · normalize.py · enrich.py · oui.csv
renderers/    html/ (dashboard + topology_server.py) · static_svg.py · mermaid.py
systemd/      timer/service units + topology-agent.service
tests/        fixtures + end-to-end pipeline test
make_network_topology.py   network topology orchestrator

# hardware topology + fleet dashboard (see HOWTO.md)
make_pc_topology.py     Windows hardware scan (PnP/CIM)
make_linux_topology.py  Linux hardware scan (sysfs/proc)
telemetry.py            shared live CPU/net/disk/temp sampler
agent.py                push topology + telemetry to the server
report.sh · report.ps1  run the agent (self-updating)
server.ps1              start the dashboard (firewall + topology_server.py)
bootstrap.sh            fresh-Debian one-liner
```

## Tests

```bash
python tests/test_pipeline.py      # no live network; uses fixtures
```

## License

MIT
