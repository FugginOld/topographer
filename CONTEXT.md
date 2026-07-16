# Domain vocabulary

The ubiquitous language of this repo. Use these terms exactly in code, docs, and
review — they name the seams.

## Topology

The **canonical domain model**: `core/schema.py`'s `Node` / `Link` / `Zone` /
`Topology` dataclasses. Collectors emit raw dicts; `core/normalize.py` merges
them into one `Topology` (deduped by MAC, reconciled by IP and hostname);
`core/enrich.py` adds vendor/kind/aging. Every network renderer consumes only
this. It is the *model*, not a view.

## Collector

A **read-only source adapter**: one file under `collectors/`, subclassing
`Collector`, exposing `collect() -> list[dict]` (+ optional `zones()`). Ten of
them (UniFi, Proxmox, ping, arp, SNMP, SSH…) behind one flat interface — the
codebase's deepest seam. A down/absent source returns `[]`, never raises, so
enabling several is safe.

## Card

The **dashboard view-model** — the `{id, parent, label, sub, cls, kind, meta,
…}` node tree that `renderers/html/index.html` draws. Distinct from **Topology**:
Topology is the domain model, Card is the *view*. A Card carries a core set
(`id, parent, label, sub, cls, kind, meta`) plus optional animation fields the
hardware fabric uses (`cap, grp, up, fill, link, iface`).

Owned by `renderers/card.py` (a pure, zero-dependency dataclass). Two writers go
*through* it — `renderers/network_cards.py::from_topology` (reshapes a `Topology`
into the WAN → gateway → VLAN → host tree) and `scanners/make_pc_topo.py` (validates
its output against `Card` at the `build()` boundary). The third,
`scanners/make_linux_topo.py`, emits the same contract **inline**: `scan_host` pipes
that file *alone* over SSH to hosts without the repo, so it must stay single-file
self-contained and can't import `Card`. It's the deliberate exception — keep it
in sync with `Card`.

## Store

The **topology persistence** module (`renderers/html/store.py`): save / load /
list / delete over the guarded directory `out/topologies/*.json`, plus `path()`,
which delegates to the shared barrier **`_guard.guarded_path`** — the single
path-injection check every user-supplied id passes (basename → charset allowlist →
realpath containment), now in exactly one tested place. Deep and narrow: bytes
in/out, no presentation, so it imports nothing server-specific and its barrier is
unit-tested with no HTTP. `topology_server` depends on it one-way; the sidebar-row
shaping (`kind`/`ip`, which needs `server_ip`) stays a view helper in the server,
not in the store.

## Widget

A per-service dashboard panel (Pi-hole, Proxmox, qBittorrent…), Homepage-style —
distinct from **Card** (network node) and the Glances panel (this host's metrics).
Three nouns:

- **Widget Type** — code: `{id, label, category, icon, fields, fetch}` in
  `widgets/registry.py`. `fields` is the config-form schema; `fetch(cfg)` returns a
  small `{key: value}` stats dict (same contract as **Collector** — stdlib-only,
  never raises, `{}` on failure). Two kinds of Type: a hand-written fetcher in
  `widgets/fetchers.py`, or a declarative **definition** (`widgets/definitions.py`)
  run by the generic engine (`widgets/engine.py`: auth + endpoints + field mappings).
- **Widget** — a user-configured *instance* of a Type owned by one host: `{id,
  type, config, position, interval}`. Persisted by `renderers/html/widget_store.py`
  (shares the `_guard.guarded_path` barrier with **Store**, `out/widgets/<host>.json`). Config
  holds secrets — masked on read, never sent to the browser.
- **Widget Store** — the browsable catalog (`widgets/catalog.json`, all 155
  Homepage types) + the persistence + the `/api/widget-*` routes + the `index.html`
  UI. `built` types are installable; the rest list for reference. All widget HTTP
  goes through `widgets/net.py` (SSRF guard: private targets only).
