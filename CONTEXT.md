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

Owned by `renderers/card.py` (a pure, zero-dependency dataclass). Three adapters
produce Cards: the two hardware scanners (`make_pc_topology.py`,
`make_linux_topology.py`) and `renderers/network_cards.py::from_topology`, which
reshapes a `Topology` into the WAN → gateway → VLAN → host card tree. The card
contract lives in one place, not implicitly across a JS reader and three writers.
