"""OPNsense collector -- the authoritative source for VLAN zones + DHCP names.

Uses the OPNsense REST API (key/secret as basic auth). Create a key under
System > Access > Users > (user) > API keys.

Config:
  opnsense:
    enabled: true
    url: https://10.0.10.1
    key: "..."          # keep in config.yaml (gitignored), never commit
    secret: "..."
    verify_tls: false   # true if the firewall has a trusted cert
    zone_map:           # map interface/description -> zone class + policy
      SERVERS: {cls: srv, policy: "selective"}
      IOT:     {cls: iot, policy: "no lateral"}

Endpoints used (stable across recent OPNsense):
  /api/interfaces/vlan_settings/searchItem      VLAN tag definitions
  /api/dhcpv4/leases/searchLease                DHCP leases (names + MAC + IP)
  /api/diagnostics/interface/getArp             ARP table (live presence)
"""
from __future__ import annotations

import ipaddress
import logging

from . import transport
from .base import Collector
from core.schema import now_iso

log = logging.getLogger("collector.opnsense")


def vlan_rows_to_zones(rows: list, zone_map: dict) -> list[dict]:
    """VLAN settings rows -> zones. A row without a numeric tag is skipped; the
    zone_map is consulted by upper-case description first, then verbatim."""
    zones = []
    for row in rows or []:
        try:
            vid = int(row.get("tag") or 0)
        except (TypeError, ValueError):
            continue
        desc = (row.get("descr") or row.get("description") or f"VLAN{vid}").strip()
        meta = (zone_map or {}).get(desc.upper(), (zone_map or {}).get(desc, {}))
        zones.append({"vid": vid, "name": desc,
                      "subnet": row.get("subnet", ""),   # often filled from iface cfg
                      "policy": meta.get("policy", ""),
                      "cls": meta.get("cls", "unknown")})
    return zones


def merge_leases_arp(lease_rows: list, arp, ts: str) -> list[dict]:
    """DHCP leases + the ARP table -> raw nodes, keyed mac-else-ip. Leases give
    names, ARP proves presence and fills gaps. `arp` arrives either as a bare list
    or wrapped in {"rows": [...]} depending on the OPNsense version."""
    nodes: dict[str, dict] = {}
    for row in lease_rows or []:
        mac = (row.get("mac") or "").lower()
        ip = row.get("address") or row.get("ip")
        if not (mac or ip):
            continue
        nodes[mac or ip] = {
            "ip": ip, "mac": mac or None,
            "name": row.get("hostname") or row.get("descr") or ip,
            "online": (row.get("status") or "").lower() == "online",
            "last_seen": ts,
        }
    entries = arp if isinstance(arp, list) else (arp or {}).get("rows", [])
    for entry in entries or []:
        mac = (entry.get("mac") or "").lower()
        ip = entry.get("ip")
        if not (mac or ip):
            continue
        node = nodes.setdefault(mac or ip, {"ip": ip, "mac": mac or None,
                                            "name": entry.get("hostname") or ip})
        node["online"] = True
        node["last_seen"] = ts
        node.setdefault("vendor", entry.get("manufacturer"))
    return list(nodes.values())


class OPNsenseCollector(Collector):
    name = "opnsense"

    def _auth(self):
        return (self.cfg["key"], self.cfg["secret"])

    def _get(self, path: str):
        return transport.get_json(self.cfg["url"].rstrip("/") + path,
                                  basic_auth=self._auth(), verify=self.cfg.get("verify_tls", False))

    def _post(self, path: str, payload: dict | None = None):
        return transport.get_json(self.cfg["url"].rstrip("/") + path, json_body=payload or {},
                                  basic_auth=self._auth(), verify=self.cfg.get("verify_tls", False))

    # ---- zones from VLAN settings ----
    def zones(self) -> list[dict]:
        data = self._post("/api/interfaces/vlan_settings/searchItem", {"current": 1, "rowCount": 500})
        return vlan_rows_to_zones((data or {}).get("rows", []), self.cfg.get("zone_map", {}))

    def collect(self) -> list[dict]:
        ts = now_iso()
        leases = self._post("/api/dhcpv4/leases/searchLease", {"current": 1, "rowCount": 2000})
        arp = self._get("/api/diagnostics/interface/getArp") or []
        return self._tag(merge_leases_arp((leases or {}).get("rows", []), arp, ts))


if __name__ == "__main__":   # ponytail: zone + lease/arp mapping, offline
    z = vlan_rows_to_zones(
        [{"tag": "30", "descr": "SERVERS", "subnet": "10.0.30.0/24"},
         {"tag": "40", "description": "ADS-B"},
         {"tag": "x"},                       # unparseable tag -> skipped
         {"tag": 50}],                       # no description -> VLAN50
        {"SERVERS": {"cls": "srv", "policy": "selective"}})
    assert [x["vid"] for x in z] == [30, 40, 50], z
    assert z[0]["cls"] == "srv" and z[0]["policy"] == "selective"
    assert z[1]["cls"] == "unknown" and z[2]["name"] == "VLAN50", z

    LEASES = [{"mac": "AA:BB:CC:00:00:01", "address": "10.0.30.5", "hostname": "pve",
               "status": "online"},
              {"address": "10.0.30.6", "descr": "printer"},          # lease with no mac
              {}]                                                     # nothing usable
    for arp in ([{"mac": "AA:BB:CC:00:00:02", "ip": "10.0.30.7", "manufacturer": "Intel"}],
                {"rows": [{"mac": "AA:BB:CC:00:00:02", "ip": "10.0.30.7",
                           "manufacturer": "Intel"}]}):               # both wire shapes
        out = merge_leases_arp(LEASES, arp, "T")
        by_ip = {n["ip"]: n for n in out}
        assert len(out) == 3, out
        assert by_ip["10.0.30.5"]["mac"] == "aa:bb:cc:00:00:01"       # lowercased
        assert by_ip["10.0.30.5"]["online"] is True
        assert by_ip["10.0.30.6"]["name"] == "printer" and by_ip["10.0.30.6"]["online"] is False
        assert by_ip["10.0.30.7"]["vendor"] == "Intel", by_ip
    # a lease seen in ARP is marked live, not duplicated
    out = merge_leases_arp([{"mac": "AA:BB:CC:00:00:09", "address": "10.0.30.9"}],
                           [{"mac": "aa:bb:cc:00:00:09", "ip": "10.0.30.9"}], "T")
    assert len(out) == 1 and out[0]["online"] is True, out
    assert merge_leases_arp([], None, "T") == []
    print("collectors/opnsense mapping self-check ok")
