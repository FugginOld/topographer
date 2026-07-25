# Contributing

Homelab **network + hardware topology generator**: scanners and collectors gather a machine's or
a network's real topology, a pipeline normalizes + enriches it, and the dashboard draws it live.
Reporting agents push each host's map + telemetry to the dashboard server.

Domain vocabulary — **Topology, Collector, Card, Store, Widget** — is defined in
[CONTEXT.md](CONTEXT.md). Use those names. This file is operational: how to work in the repo.

## Pipeline

`collectors/*.py` + `scanners/*.py` gather raw dicts → `core/normalize.py` (dedup by MAC, reconcile
by IP/hostname, VLAN by subnet) → `core/enrich.py` (vendor via `core/oui.csv`, kind, aging) →
`renderers/card.py` view-model → `renderers/html/` (the dashboard).

Two dashboard subsystems live beside it, both server-side only (not in the agent bundle):
**Glances** (this host's live metrics — `core/glances.py`; remote hosts push via the agent) and the
**Widget Store** (`widgets/` + `renderers/html/widget_store.py`, `/api/widget-*` routes — see
[CONTEXT.md](CONTEXT.md) → Widget, `docs/prd/widget-store.md`, `docs/widgets-adding.md`).

## Commands

Install: `pip install -r requirements.txt`

- Dashboard server: `python renderers/html/topo_server.py [--port 8770]`
- Map this machine: `python scanners/make_pc_topo.py` (Windows) · `python scanners/make_linux_topo.py` (Linux)
- Map the network: `python scanners/make_network_topo.py`
- Report to a dashboard: `agent/report.sh http://<dashboard-ip>:8770`
- Run it as a container: `docker compose up -d` (see [docker-compose.yml](docker-compose.yml))

## Tests

Every check is offline and runs in seconds. This list is exactly what CI runs
([.github/workflows/ci.yml](.github/workflows/ci.yml)) — keep the two in sync.

```
python -m compileall -q .
python tests/test_pipeline.py                    # collectors -> normalize -> enrich, on fixtures
python tests/test_cards.py                       # card contract + network-cards adapter
python tests/test_routes.py                      # boots topo_server on a free port, hits its routes
python -m collectors.unifi                       # unifi transforms + the single GET/POST request path
python -m core.identity                          # node id rule + link endpoint resolution
python -m collectors.arpscan                     # arp-scan + nmap output parsers
python -m collectors.unifi_snmp                  # snmpwalk parser + FDB OID -> MAC decode
python -m collectors.docker                      # docker ps line parser
python -m collectors.tailscale                   # peer shaping + overlay mesh
python -m collectors.dns                         # hosts file + pi-hole custom dns rows
python -m collectors.pingsweep                   # arp -a table parser (win + posix)
python -m collectors.opnsense                    # vlan zones + lease/arp merge
python renderers/html/_guard.py                  # shared path-injection barrier
python renderers/html/pushcache.py               # push-freshness cache (live/stale/miss)
python renderers/html/store.py                   # store slug policy + save/load
python renderers/html/widget_store.py            # widget store CRUD round-trip
python renderers/html/icons.py                   # service-icon slug candidates
python renderers/html/report.py                  # diagnostic HTML report escaping
python renderers/html/pve_tiles.py               # proxmox guests -> dashboard tiles
python renderers/html/agent_bundle.py            # agent tar.gz/zip builder
python collectors/transport.py                   # shared collector HTTP (ssl ctx + get_json)
python -m widgets.net                            # widget SSRF guard
python -m widgets.engine                         # widget engine (auth/mapping)
python -m widgets.fetchers                       # widget stat parsers + collector-backed SSRF guard
python -m widgets.policy                         # widget config/secret policy (mask/whitelist/inherit)
python -m widgets.registry                       # catalog integrity + full_catalog merge
python scanners/make_linux_topo.py --selftest
python scanners/make_pc_topo.py --selftest       # perf-counter iface + cap vocabulary
bash tests/test_uninstall_unraid.sh              # uninstaller only removes its own boot 'go' lines
```

(`widgets/*` use relative imports — run via `python -m widgets.<mod>`, like `collectors/`.)
Also in CI: `bash -n *.sh tests/*.sh`; `.ps1` files must be ASCII; dashboard JS via `node --check`
on the extracted `<script>` block.

Non-trivial logic leaves one runnable check behind — an `assert`-based `__main__` self-check in the
module itself, or a small `tests/test_*.py`. No frameworks, no fixtures beyond `tests/fixtures/`.

## Gotchas that will bite you

These are not style preferences. Each one has broken something real.

1. **Never commit `config.yaml`** — it is gitignored and holds secrets (API keys, tokens). Keep real
   IPs / users / hostnames out of the tracked `config.example.yaml`; use placeholders.
2. **`scanners/make_linux_topo.py` imports nothing from this repo.** The dashboard's remote scan
   pipes it alone over SSH, so it must stay single-file self-contained (stdlib only). Verify:
   `grep -nE '^(from|import) (core|renderers|collectors|scanners|make_)' scanners/make_linux_topo.py`
   returns nothing. (Background: [CONTEXT.md](CONTEXT.md) → Card.)
3. **Collectors are stdlib-only** (`urllib`, not `requests`), return `[]` on any source failure
   (never raise), and must be registered in `scanners/make_network_topo.py`.
   ([CONTEXT.md](CONTEXT.md) → Collector.)
4. **`.ps1` files must be ASCII** — Windows PowerShell 5.1 reads BOM-less files as ANSI, so a stray
   non-ASCII byte corrupts them (CI enforces this). Bind PS named params with a hashtable splat, not
   an array (an array splat binds positionally).
5. **Dashboard `index.html` is served from disk per request** — HTML/CSS/JS edits need only a browser
   hard-refresh, no restart. Only Python changes need the server restarted.
6. **The dashboard's SVG builder uses `textContent`, never `innerHTML`,** for scanned or host-supplied
   strings — device labels are untrusted. Keep it that way.
7. **Restarting the server depends on how it was started.** `systemctl restart topo-server` only
   applies on a systemd Linux box. The production dashboard runs on **Unraid** (Slackware, no
   systemd), as a container: `docker restart topographer`. There is no `systemctl` on an Unraid
   host — never tell a user to run it there. The older native Unraid install (flash launcher + a
   boot `go` hook) is retired: `install.sh` refuses on Unraid and points at the container, while
   `uninstall.sh` still removes it so existing installs can migrate. Agents on Unraid are
   unaffected — `bootstrap.sh` still uses the `go` script for `topo-agent`.
8. **Reporting clients are NOT git checkouts.** A reporting host (e.g. a Pi agent) runs the *agent
   bundle* the dashboard serves — `bootstrap.sh` fetches `/agent.tar.gz` and extracts it (no `.git`),
   so `git pull` fails there. To ship agent-side code to a client: update + restart the dashboard
   **server** first (it builds the bundle from its own repo — `AGENT_PATHS` in
   `renderers/html/agent_bundle.py`), then re-run the bootstrap on the client to re-fetch.

## Style

- Python: stdlib-first, type hints, `from __future__ import annotations`. `requirements.txt` is
  intentionally tiny — don't add a dependency for what a few lines of stdlib can do.
- Match the terseness and comment density of the file you are editing.
- Pushes go straight to `main`; there is no feature-branch / PR flow on this repo.
