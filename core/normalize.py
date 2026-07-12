"""Merge raw collector output into one canonical Topology.

Raw shape each collector emits (all keys optional except where noted):
  {"kind": "node|link|zone", ...fields...}

Nodes are deduplicated by MAC when available, else by IP. Zone membership is
assigned by testing each node IP against zone subnets.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable

from .schema import Node, Link, Zone, Topology, norm_mac


def _zone_for_ip(ip: str | None, zones: list[Zone]) -> int | None:
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for z in zones:
        try:
            if addr in ipaddress.ip_network(z.subnet, strict=False):
                return z.vid
        except ValueError:
            continue
    return None


def _node_key(raw: dict) -> str:
    mac = norm_mac(raw.get("mac"))
    if mac:
        return f"mac:{mac}"
    if raw.get("ip"):
        return f"ip:{raw['ip']}"
    return f"name:{raw.get('name', 'unknown')}"


def normalize(raw_items: Iterable[dict], zones_raw: Iterable[dict]) -> Topology:
    zones = [Zone(**z) for z in zones_raw]
    topo = Topology(zones=zones)

    node_by_key: dict[str, Node] = {}
    links: list[Link] = []

    for raw in raw_items:
        kind = raw.get("kind")
        if kind == "link":
            links.append(
                Link(
                    src=raw["src"],
                    dst=raw["dst"],
                    kind=raw.get("linkkind", "l2"),
                    port=raw.get("port"),
                )
            )
            continue

        # treat everything else as a node
        key = _node_key(raw)
        mac = norm_mac(raw.get("mac"))
        src = raw.get("source", "unknown")

        if key in node_by_key:
            n = node_by_key[key]
            # merge: prefer more specific / non-empty values
            n.name = raw.get("name") or n.name
            n.ip = raw.get("ip") or n.ip
            n.mac = mac or n.mac
            n.kind = raw.get("nodekind") or (n.kind if n.kind != "unknown" else "unknown")
            n.vendor = raw.get("vendor") or n.vendor
            n.host = raw.get("host") or n.host
            for t in raw.get("tags", []):
                if t not in n.tags:
                    n.tags.append(t)
            if src not in n.sources:
                n.sources.append(src)
            if raw.get("online") is not None:
                n.online = n.online or bool(raw["online"])
            n.last_seen = raw.get("last_seen") or n.last_seen
            if raw.get("meta"):
                n.meta.update(raw["meta"])
        else:
            n = Node(
                id=key.split(":", 1)[1],
                name=raw.get("name") or raw.get("ip") or key,
                kind=raw.get("nodekind", "unknown"),
                ip=raw.get("ip"),
                mac=mac,
                vendor=raw.get("vendor"),
                host=raw.get("host"),
                tags=list(raw.get("tags", [])),
                online=bool(raw.get("online", True)),
                last_seen=raw.get("last_seen"),
                sources=[src],
                meta=dict(raw.get("meta") or {}),
            )
            node_by_key[key] = n

    # reconcile: fold ip-only nodes into a mac-keyed node sharing the same IP
    # (e.g. DNS/tailscale records that lacked a MAC when first seen)
    mac_by_ip = {n.ip: n for n in node_by_key.values()
                 if n.mac and n.ip}
    for key in list(node_by_key.keys()):
        n = node_by_key[key]
        if n.mac or not n.ip:
            continue
        target = mac_by_ip.get(n.ip)
        if target and target is not n:
            # merge n into the mac-keyed target, keep the better name
            if n.name and (not target.name or target.name == target.ip):
                target.name = n.name
            for t in n.tags:
                if t not in target.tags:
                    target.tags.append(t)
            for s in n.sources:
                if s not in target.sources:
                    target.sources.append(s)
            target.online = target.online or n.online
            del node_by_key[key]

    # reconcile by hostname: fold name-only nodes (tailscale peers, etc.) into a
    # richer node with the same name so overlay tags land on the real host
    rich_by_name = {n.name.lower(): n for n in node_by_key.values()
                    if (n.mac or n.ip) and n.name}
    for key in list(node_by_key.keys()):
        n = node_by_key.get(key)
        if not n or n.mac or n.ip:
            continue
        target = rich_by_name.get((n.name or "").lower())
        if target and target is not n:
            for t in n.tags:
                if t not in target.tags:
                    target.tags.append(t)
            for s in n.sources:
                if s not in target.sources:
                    target.sources.append(s)
            target.online = target.online or n.online
            del node_by_key[key]

    # assign VLAN membership
    for n in node_by_key.values():
        if n.vlan is None:
            n.vlan = _zone_for_ip(n.ip, zones)

    topo.nodes = list(node_by_key.values())
    # keep only links whose endpoints resolved to known nodes
    ids = {n.id for n in topo.nodes}
    topo.links = [l for l in links if l.src in ids and l.dst in ids]
    return topo
