"""Proxmox VE nodes, VMs, and LXC containers via the PVE API.

Passive network discovery only ever sees the Proxmox *host* (its physical NIC);
guests behind NAT or internal bridges never present a MAC to the gateway, so
UniFi/arp/ping can't find them. This asks the PVE API directly, so every VM and
container shows up — nested under its host, with status, MAC, and IP where known.

Config:
  proxmox:
    enabled: true
    url: https://192.168.1.235:8006
    token: "topology@pam!ro=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"  # API token:
    verify_tls: false            # Datacenter > Permissions > API Tokens (PVEAuditor role)
    # node: pve                  # optional; auto-discovered from /nodes otherwise

Stdlib only (urllib) — no extra dependency. Read-only: only GETs the API.
"""
from __future__ import annotations

import logging
import re

from . import transport
from .base import Collector
from core.schema import norm_mac, now_iso

log = logging.getLogger("collector.proxmox")

_MAC = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


def mac_from_net(netcfg: str) -> str | None:
    """MAC out of a PVE net line (qemu 'virtio=AA:..,bridge=..' / lxc 'hwaddr=AA:..')."""
    m = _MAC.search(netcfg or "")
    return norm_mac(m.group(1)) if m else None


def ip_from_lxc_net(netcfg: str) -> str | None:
    """Static IP from an lxc net line ('...,ip=10.0.0.5/24,...'); None for dhcp/unset."""
    m = re.search(r"\bip=([0-9.]+)", netcfg or "")
    return m.group(1) if m and m.group(1) != "dhcp" else None


def ip_from_agent(data: dict | None) -> str | None:
    """First non-loopback IPv4 from a qemu guest-agent network-get-interfaces reply."""
    for iface in (data or {}).get("result", []):
        for a in iface.get("ip-addresses", []):
            ip = a.get("ip-address", "")
            if a.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
                return ip
    return None


class ProxmoxCollector(Collector):
    name = "proxmox"

    def _get(self, path: str):
        url = self.cfg["url"].rstrip("/") + "/api2/json" + path
        d = transport.get_json(url, verify=self.cfg.get("verify_tls", False),
                               headers={"Authorization": f"PVEAPIToken={self.cfg.get('token', '')}"})
        return d.get("data") if isinstance(d, dict) else None   # PVE wraps in {data: ...}

    def _guest(self, node: str, kind: str, g: dict, ts: str) -> list[dict]:
        vmid = g.get("vmid")
        name = g.get("name") or f"{kind}{vmid}"
        running = g.get("status") == "running"
        cfg = self._get(f"/nodes/{node}/{kind}/{vmid}/config") or {}
        net0 = cfg.get("net0", "")
        item = {
            "name": name,
            "nodekind": "container" if kind == "lxc" else "host",
            "host": node,                       # nest under the PVE host (by name)
            "online": running,
            "last_seen": ts,
            "tags": ["lxc" if kind == "lxc" else "vm"],
        }
        mac = mac_from_net(net0)
        if mac:
            item["mac"] = mac
        ip = ip_from_lxc_net(net0) if kind == "lxc" else (
            ip_from_agent(self._get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"))
            if running else None)
        if ip:
            item["ip"] = ip
        return [item, {"kind": "link", "src": node, "dst": name, "linkkind": "host"}]

    def collect(self) -> list[dict]:
        ts = now_iso()
        node_cfg = self.cfg.get("node")
        nodes = [node_cfg] if node_cfg else [n.get("node") for n in (self._get("/nodes") or [])]
        items: list[dict] = []
        for node in filter(None, nodes):
            # host node (name-only): normalize folds it into the LAN client with
            # the same hostname, so guests nest under the real host node
            items.append({"name": node, "nodekind": "server", "online": True,
                          "last_seen": ts, "tags": ["proxmox"]})
            for vm in self._get(f"/nodes/{node}/qemu") or []:
                items += self._guest(node, "qemu", vm, ts)
            for ct in self._get(f"/nodes/{node}/lxc") or []:
                items += self._guest(node, "lxc", ct, ts)
        return self._tag(items)


if __name__ == "__main__":  # ponytail: parser self-check, no live PVE needed
    assert mac_from_net("virtio=52:54:00:AB:CD:EF,bridge=vmbr0,firewall=1") == "52:54:00:ab:cd:ef"
    assert mac_from_net("name=eth0,bridge=vmbr0,hwaddr=AA:BB:CC:11:22:33,ip=10.0.0.5/24") == "aa:bb:cc:11:22:33"
    assert mac_from_net("no mac here") is None
    assert ip_from_lxc_net("name=eth0,hwaddr=AA:BB:CC:11:22:33,ip=192.168.2.50/24") == "192.168.2.50"
    assert ip_from_lxc_net("name=eth0,ip=dhcp") is None
    agent = {"result": [{"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
                        {"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "192.168.1.90"}]}]}
    assert ip_from_agent(agent) == "192.168.1.90"
    assert ip_from_agent({"result": []}) is None
    print("proxmox parser self-check ok")
