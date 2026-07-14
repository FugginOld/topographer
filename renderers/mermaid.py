"""Render a Topology to Mermaid flowchart text.

Mermaid is version-controllable and renders on GitHub, so a committed
out/topo.mmd (from a *sanitized* run) makes the repo self-documenting.
VLANs become subgraphs; overlay links render dashed.
"""
from __future__ import annotations

import re

from core.schema import Topology


def _safe(s: str) -> str:
    return re.sub(r"\W", "_", s or "n")


def render(topo: Topology) -> str:
    lines = ["flowchart TB", "  WAN((WAN)) --> FW[firewall]"]
    zone_of = {z.vid: z for z in topo.zones}

    # group nodes by vlan
    by_vlan: dict[int | None, list] = {}
    for n in topo.nodes:
        by_vlan.setdefault(n.vlan, []).append(n)

    for vid, nodes in sorted(by_vlan.items(), key=lambda kv: (kv[0] is None, kv[0])):
        z = zone_of.get(vid)
        title = f"VLAN {vid} {z.name}" if z else "unassigned"
        lines.append(f"  subgraph {_safe(title)}[\"{title}\"]")
        for n in nodes:
            state = "" if n.online else " ⚠"
            lines.append(f"    {_safe(n.id)}[\"{n.name}{state}\"]")
        lines.append("  end")
        if z:
            lines.append(f"  FW --> {_safe(title)}")

    for l in topo.links:
        arrow = "-.->" if l.kind == "overlay" else "-->"
        label = f"|{l.port}|" if l.port else ""
        lines.append(f"  {_safe(l.src)} {arrow}{label} {_safe(l.dst)}")

    return "\n".join(lines) + "\n"


def write(topo: Topology, path: str) -> None:
    with open(path, "w") as fh:
        fh.write(render(topo))
