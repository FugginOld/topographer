# CLAUDE.md

Homelab **network + hardware topology generator**: scanners and collectors gather a machine's or
a network's real topology, a pipeline normalizes + enriches it, and renderers draw it (Mermaid,
static SVG, and a live web dashboard). Reporting agents push each host's map + telemetry to the
dashboard server.

Domain vocabulary — **Topology, Collector, Card, Store** — is defined in `CONTEXT.md`. Use those
names. This file is operational guidance (how to work in the repo), not the glossary.

## Pipeline

`collectors/*.py` + `scanners/*.py` gather raw dicts → `core/normalize.py` (dedup by MAC, reconcile
by IP/hostname, VLAN by subnet) → `core/enrich.py` (vendor via `core/oui.csv`, kind, aging) →
`renderers/card.py` view-model → `renderers/{mermaid,static_svg,html}`.

## Commands

Install: `pip install -r requirements.txt`

- Dashboard server: `python renderers/html/topo_server.py [--port 8770]`
- Map this machine: `python scanners/make_pc_topo.py` (Windows) · `python scanners/make_linux_topo.py` (Linux)
- Map the network: `python scanners/make_network_topo.py`
- Report to a dashboard: `agent/report.sh http://<dashboard-ip>:8770`

Tests — exactly what CI runs, all offline:
```
python -m compileall -q .
python tests/test_pipeline.py
python tests/test_cards.py
python renderers/html/store.py                  # store path-injection barrier
python scanners/make_linux_topo.py --selftest
```
Also in CI: `bash -n *.sh`; `.ps1` files must be ASCII; dashboard JS via `node --check` on the
extracted `<script>` block.

## Hard rules (these prevent real breakage)

1. **Never commit `config.yaml`** — it is gitignored and holds secrets (API keys, tokens). Keep real
   IPs / users / hostnames out of the tracked `config.example.yaml`; use placeholders.
2. **`scanners/make_linux_topo.py` imports nothing from this repo.** The dashboard's remote scan
   pipes it alone over SSH, so it must stay single-file self-contained (stdlib only). Verify:
   `grep -nE '^(from|import) (core|renderers|collectors|scanners|make_)' scanners/make_linux_topo.py`
   returns nothing. (Background: `CONTEXT.md` → Card.)
3. **Collectors are stdlib-only** (`urllib`, not `requests`), return `[]` on any source failure
   (never raise), and must be registered in `scanners/make_network_topo.py`. (`CONTEXT.md` → Collector.)
4. **`.ps1` files must be ASCII** — Windows PowerShell 5.1 reads BOM-less files as ANSI, so a stray
   non-ASCII byte corrupts them (CI enforces this). Bind PS named params with a hashtable splat, not
   an array (an array splat binds positionally).
5. **Dashboard `index.html` is served from disk per request** — HTML/CSS/JS edits need only a browser
   hard-refresh, no restart. Only Python changes need `systemctl restart topo-server`.
6. **The dashboard's SVG builder uses `textContent`, never `innerHTML`,** for scanned or host-supplied
   strings — device labels are untrusted. Keep it that way.
7. **Not every server host has systemd** — rule #5's `systemctl restart topo-server` only applies on a
   systemd Linux box. The production dashboard runs on **Unraid** (Slackware, no systemd), where you
   restart the process itself: kill it (`ps aux | grep '[t]opo_server'` → `kill <pid>`) and relaunch it
   the way it's started there (Docker container `docker restart <name>`, or its User Scripts entry).
   Never tell the user to `systemctl` on the Unraid host.

## Conventions

- **Pushes go straight to `main`** — there is no feature-branch / PR flow. Commit only when asked, and
  end commit messages with the `Co-Authored-By` trailer.
- **Reporting clients are NOT git checkouts.** A reporting host (e.g. the Pi agents) runs the *agent
  bundle* the dashboard serves — `bootstrap.sh` fetches `/agent.tar.gz` and extracts it (no `.git`), so
  `git pull` fails there. To ship agent-side code to a client: update + restart the dashboard **server**
  first (it builds the bundle from its own repo — `AGENT_PATHS` in `topo_server.py`), then re-run the
  bootstrap on the client to re-fetch. Never tell the user to `git pull` on a client.
- Python: stdlib-first, type hints, `from __future__ import annotations`. `requirements.txt` is
  intentionally tiny — don't add a dependency for what a few lines of stdlib can do.
- Match the terseness and comment density of the file you are editing.
