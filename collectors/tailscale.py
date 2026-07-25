"""Tailscale overlay plane via `tailscale status --json`.

Tags online tailnet peers with "ts" and emits overlay links between them so the
renderer can draw the mesh that cuts across VLANs. Matches peers back to L2 hosts
by hostname during normalize (best-effort; MAC isn't visible over the tailnet).

Config:
  tailscale:
    enabled: true
    binary: tailscale        # or full path
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess

from .base import Collector
from core.schema import now_iso

log = logging.getLogger("collector.tailscale")


def peers_to_items(data: dict, ts: str) -> list[dict]:
    """`tailscale status --json` -> overlay nodes + a logical full mesh between the
    online ones. Self is a peer too. A peer with no usable name is skipped; the L2
    match back to a real host is by name, so the name is the whole point."""
    peers = list((data.get("Peer") or {}).values())
    self_node = data.get("Self")
    if self_node:
        peers.append(self_node)

    items: list[dict] = []
    online_names: list[str] = []
    for p in peers:
        name = (p.get("HostName") or p.get("DNSName", "").split(".")[0] or "").strip()
        if not name:
            continue
        ips = p.get("TailscaleIPs") or []
        online = bool(p.get("Online"))
        items.append({"name": name, "ip": ips[0] if ips else None, "tags": ["ts"],
                      "online": online, "last_seen": ts})
        if online:
            online_names.append(name)

    for i in range(len(online_names)):
        for j in range(i + 1, len(online_names)):
            items.append({"kind": "link", "src": online_names[i], "dst": online_names[j],
                          "linkkind": "overlay"})
    return items


class TailscaleCollector(Collector):
    name = "tailscale"

    def collect(self) -> list[dict]:
        binary = self.cfg.get("binary", "tailscale")
        if not shutil.which(binary):
            log.warning("tailscale binary not found")
            return []
        try:
            out = subprocess.run(
                [binary, "status", "--json"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            data = json.loads(out.stdout or "{}")
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
            log.warning("tailscale status failed: %s", e)
            return []

        return self._tag(peers_to_items(data, now_iso()))


if __name__ == "__main__":   # ponytail: peer shaping + mesh, offline
    DATA = {
        "Self": {"HostName": "laptop", "TailscaleIPs": ["100.64.0.1"], "Online": True},
        "Peer": {
            "k1": {"HostName": "rpi5b", "TailscaleIPs": ["100.64.0.2"], "Online": True},
            "k2": {"DNSName": "nas.tail1234.ts.net.", "TailscaleIPs": ["100.64.0.3"], "Online": False},
            "k3": {"HostName": "", "DNSName": "", "Online": True},          # unnamed -> skipped
        },
    }
    items = peers_to_items(DATA, "T")
    nodes = [i for i in items if i.get("kind") != "link"]
    links = [i for i in items if i.get("kind") == "link"]
    assert sorted(n["name"] for n in nodes) == ["laptop", "nas", "rpi5b"], nodes
    assert next(n for n in nodes if n["name"] == "nas")["online"] is False
    assert next(n for n in nodes if n["name"] == "nas")["ip"] == "100.64.0.3"
    assert all("ts" in n["tags"] for n in nodes)
    # mesh spans the ONLINE peers only (rpi5b + laptop), so exactly one edge
    assert len(links) == 1, links
    assert {links[0]["src"], links[0]["dst"]} == {"rpi5b", "laptop"}, links
    assert links[0]["linkkind"] == "overlay"
    assert peers_to_items({}, "T") == []
    print("collectors/tailscale parser self-check ok")
