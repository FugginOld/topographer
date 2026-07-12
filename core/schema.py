"""Canonical topology model.

Every collector produces raw source-specific dicts; core.normalize merges them
into these three types, and every renderer consumes only these. The join key
across all sources is the MAC address (normalized lowercase, colon-separated).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


# node.kind values renderers know how to draw
KINDS = {"firewall", "switch", "ap", "host", "server", "container", "iot", "unknown"}

# zone.cls drives the color band in the HTML renderer
ZONE_CLASSES = {"mgmt", "lan", "srv", "adsb", "iot", "guest", "dmz", "unknown"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_mac(mac: Optional[str]) -> Optional[str]:
    """Lowercase, colon-separated, or None. Accepts aa-bb, aabb.ccdd.eeff, etc."""
    if not mac:
        return None
    hexstr = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    if len(hexstr) != 12:
        return None
    return ":".join(hexstr[i : i + 2] for i in range(0, 12, 2))


@dataclass
class Zone:
    vid: int
    name: str
    subnet: str                       # CIDR, e.g. "10.0.30.0/24"
    policy: str = ""
    cls: str = "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Node:
    id: str                           # stable id: mac if known else ip
    name: str
    kind: str = "unknown"
    ip: Optional[str] = None
    mac: Optional[str] = None
    vlan: Optional[int] = None        # vid the node lives in
    host: Optional[str] = None        # parent node id (e.g. container -> docker host)
    vendor: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    online: bool = True
    last_seen: Optional[str] = None
    sources: list[str] = field(default_factory=list)  # which collectors saw it
    meta: dict = field(default_factory=dict)  # source-specific extras (gateway model/firmware/WAN...)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Link:
    src: str                          # node id
    dst: str                          # node id
    kind: str = "l2"                  # uplink | l2 | l3 | overlay | host
    port: Optional[str] = None        # switch port if known (from LLDP/FDB)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Topology:
    zones: list[Zone] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    generated: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "generated": self.generated,
            "zones": [z.to_dict() for z in self.zones],
            "nodes": [n.to_dict() for n in self.nodes],
            "links": [l.to_dict() for l in self.links],
        }

    def dump(self, path: str) -> None:
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "Topology":
        with open(path) as fh:
            d = json.load(fh)
        t = cls(generated=d.get("generated", now_iso()))
        t.zones = [Zone(**z) for z in d.get("zones", [])]
        t.nodes = [Node(**n) for n in d.get("nodes", [])]
        t.links = [Link(**l) for l in d.get("links", [])]
        return t
