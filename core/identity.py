"""Node identity: the id rule, and the alias table that resolves link endpoints.

A Node's id is MAC if known, else IP, else name (`node_id`). But collectors
address links by whatever identifier *their* source holds — Proxmox and Docker
nest guests under the host's **name** (`proxmox.py:85`, `docker.py:71`), SNMP
names switches (`unifi_snmp.py:94`), ARP knows only MACs. Normalize also *folds*
duplicate nodes away, deleting ids other collectors already referenced.

Both mismatches used to delete edges silently. `build_index` collects every
identifier each node answers to, `resolve_links` rewrites endpoints through it.

Ambiguity is refused, not guessed: when two nodes answer to the same name, that
name resolves to nothing rather than to the wrong node. A real node id always
wins over an alias.
"""
from __future__ import annotations

from typing import Iterable, Mapping

from .schema import Link, Node, norm_mac


def node_id(mac: str | None = None, ip: str | None = None, name: str | None = None) -> str:
    """The id rule, in one place: MAC (normalized), else IP, else name."""
    m = norm_mac(mac)
    if m:
        return m
    if ip:
        return str(ip)
    return str(name or "unknown")


def _ref(value) -> str:
    return str(value or "").strip().lower()


def build_index(nodes: Iterable[Node], aliases: Mapping[str, str] | None = None) -> dict[str, str]:
    """Every identifier -> the node id that answers to it.

    Each node claims its id, MAC, IP and name. `aliases` adds folded-away ids
    (old id -> surviving id) so links written before the fold still land.
    """
    ids: dict[str, str] = {}
    claimed: dict[str, str] = {}
    ambiguous: set[str] = set()

    for n in nodes:
        ids[_ref(n.id)] = n.id
        for value in (n.id, n.mac, n.ip, n.name):
            key = _ref(value)
            if not key:
                continue
            prev = claimed.get(key)
            if prev is None:
                claimed[key] = n.id
            elif prev != n.id:
                ambiguous.add(key)          # two nodes, one name -> resolve to neither

    index = {k: v for k, v in claimed.items() if k not in ambiguous}
    for old, new in (aliases or {}).items():
        key = _ref(old)
        if key and key not in ids:          # never shadow a live node id
            index[key] = new
    index.update(ids)                       # an actual id always wins
    return index


def resolve_links(links: Iterable[Link], index: Mapping[str, str]) -> list[Link]:
    """Rewrite each endpoint to a real node id; drop links that can't resolve."""
    out: list[Link] = []
    for l in links:
        src, dst = index.get(_ref(l.src)), index.get(_ref(l.dst))
        if not src or not dst or src == dst:
            continue                        # unknown endpoint, or a self-loop after folding
        l.src, l.dst = src, dst
        out.append(l)
    return out


if __name__ == "__main__":   # ponytail: the resolution policy, offline
    def n(nid, **kw):
        return Node(id=nid, name=kw.pop("name", nid), **kw)

    assert node_id(mac="DC-A6-32-11-22-33") == "dc:a6:32:11:22:33"
    assert node_id(ip="10.0.0.1") == "10.0.0.1"          # no mac -> ip
    assert node_id(mac="nonsense", ip="10.0.0.1") == "10.0.0.1"   # unusable mac -> ip
    assert node_id(name="pve") == "pve"
    assert node_id() == "unknown"

    host = n("aa:bb:cc:00:00:01", name="pve", ip="10.0.30.5", mac="aa:bb:cc:00:00:01")
    guest = n("aa:bb:cc:00:00:02", name="vm100", mac="aa:bb:cc:00:00:02")
    idx = build_index([host, guest], {"pve-old": host.id})
    assert idx["pve"] == host.id                          # by name
    assert idx["10.0.30.5"] == host.id                    # by ip
    assert idx["pve-old"] == host.id                      # by fold alias
    assert idx["vm100"] == guest.id

    links = resolve_links([Link(src="pve", dst="vm100", kind="host")], idx)
    assert len(links) == 1 and links[0].src == host.id and links[0].dst == guest.id

    # ambiguity refuses rather than guesses
    twin_a, twin_b = n("mac:1", name="nas", mac="mac:1"), n("mac:2", name="nas", mac="mac:2")
    amb = build_index([twin_a, twin_b])
    assert "nas" not in amb, amb
    assert resolve_links([Link(src="nas", dst="mac:1")], amb) == []

    # a real node id outranks another node's alias
    aliased = build_index([n("switch01"), n("10.0.10.2", name="switch01")])
    assert aliased["switch01"] == "switch01"

    # self-loops (both endpoints folded into one node) are dropped
    assert resolve_links([Link(src="pve", dst="10.0.30.5")], idx) == []
    print("core/identity self-check ok")
