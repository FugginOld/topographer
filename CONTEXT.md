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
into the WAN → gateway → VLAN → host tree) and `make_pc_topology.py` (validates
its output against `Card` at the `build()` boundary). The third,
`make_linux_topology.py`, emits the same contract **inline**: `scan_host` pipes
that file *alone* over SSH to hosts without the repo, so it must stay single-file
self-contained and can't import `Card`. It's the deliberate exception — keep it
in sync with `Card`.
