"""Reshape a Topology into dashboard Cards — see CONTEXT.md § Card.

The network adapter: WAN → gateway → VLAN → host, with guests (Proxmox VMs /
Docker containers) nested under their host. One of three Card producers; the
other two are the hardware scanners. Kept beside `card`, not inside it, so the
Card contract stays a pure, dependency-free type.
"""
from __future__ import annotations

import ipaddress

from renderers.card import Card


def node_meta(n: dict) -> dict:
    """Detail rows for a node: collector-supplied meta first, then basics."""
    basics = {k: v for k, v in (("ip", n.get("ip")), ("mac", n.get("mac")),
              ("vendor", n.get("vendor")), ("type", n.get("kind")),
              ("online", "yes" if n.get("online") else "no"),
              ("last seen", n.get("last_seen"))) if v}
    return {**(n.get("meta") or {}), **basics}


def from_topology(d: dict) -> list[dict]:
    """Topology dict (zones/nodes) → dashboard card tree. WAN → gateway → VLAN →
    host when a gateway is known, else a synthetic NETWORK root (ping-sweep-only)."""
    zones = d.get("zones", [])
    znets = []
    for z in zones:
        try:
            znets.append((z, ipaddress.ip_network(z.get("subnet", ""), strict=False)))
        except ValueError:
            znets.append((z, None))

    nodes = d.get("nodes", [])
    gw = next((n for n in nodes if n.get("kind") == "firewall"), None)
    wan = next((n for n in nodes if n.get("kind") == "wan"), None)
    skip = {id(gw), id(wan)}                      # placed explicitly, not as hosts

    cards: list[dict] = []
    if gw:
        gw_id = gw.get("id") or gw.get("mac") or gw.get("ip")
        if wan:
            cards.append(Card(id="wan", label="WAN / INTERNET", kind="wan", cls="mgmt",
                              sub=wan.get("ip") or "", meta=node_meta(wan)).to_dict())
        cards.append(Card(id=gw_id, parent=("wan" if wan else None),
                          label=gw.get("name") or gw_id, sub=gw.get("ip") or "",
                          cls="mgmt", kind="firewall", meta=node_meta(gw)).to_dict())
        zone_root = gw_id
    else:
        cards.append(Card(id="net", label="NETWORK", kind="root").to_dict())
        zone_root = "net"

    for z in zones:
        cards.append(Card(id=f"vlan{z['vid']}", parent=zone_root, label=z["name"],
                          sub=z.get("subnet", ""), cls=z.get("cls", "unknown"), kind="zone",
                          meta={"vlan": z["vid"], "subnet": z.get("subnet", ""),
                                "policy": z.get("policy", "")}).to_dict())

    for n in nodes:
        if id(n) in skip:
            continue
        ip, parent, cls = n.get("ip"), zone_root, None
        for z, net in znets:
            try:
                if net and ip and ipaddress.ip_address(ip) in net:
                    parent, cls = f"vlan{z['vid']}", z.get("cls", "unknown")
                    break
            except ValueError:
                pass
        card = Card(id=n.get("id") or ip, parent=parent, label=n.get("name") or ip,
                    sub=n.get("ip") or "", cls=cls, kind=n.get("kind", "host"),
                    meta=node_meta(n)).to_dict()
        if n.get("host"):
            card["_host"] = n["host"]     # nest under its host below (proxmox VM / docker container)
        cards.append(card)

    # nest guests under their host node, matched by hostname
    by_label: dict[str, str] = {}
    for c in cards:
        by_label.setdefault((c.get("label") or "").lower(), c["id"])
    for c in cards:
        h = c.pop("_host", None)
        if h and by_label.get(h.lower()) and by_label[h.lower()] != c["id"]:
            c["parent"] = by_label[h.lower()]
    return cards
