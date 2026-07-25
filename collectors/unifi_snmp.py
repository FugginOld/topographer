"""Switch physical topology via SNMP (LLDP-MIB + BRIDGE-MIB FDB).

This is the one source that yields *physical port* edges -- which host is on
which switch port -- that ARP/DHCP cannot infer. Shells out to net-snmp's
snmpwalk (ubiquitous on homelab hosts); no python SNMP dependency required.

Requires: snmp package (`apt install snmp`), SNMP enabled on the switch.

Config:
  unifi_snmp:
    enabled: true
    switches:
      - {host: 10.0.10.2, community: public, version: 2c, name: core-sw01}

Emits a switch node per device plus link{linkkind:l2, port} edges to neighbors
seen in the forwarding database. LLDP neighbors (other switches/APs) become
uplink edges.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

from .base import Collector
from core.schema import norm_mac, now_iso

log = logging.getLogger("collector.unifi_snmp")

# OIDs
LLDP_REM_SYSNAME = "1.0.8802.1.1.2.1.4.1.1.9"     # lldpRemSysName
LLDP_REM_PORTID = "1.0.8802.1.1.2.1.4.1.1.7"      # lldpRemPortId
DOT1D_FDB = "1.3.6.1.2.1.17.4.3.1.2"              # dot1dTpFdbPort (mac -> bridge port)
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"               # ifName


def parse_walk(text: str) -> list[tuple[str, str]]:
    """snmpwalk -On output -> (oid, value) pairs. Drops the type prefix
    ("STRING: eth0") and surrounding quotes; ignores anything without " = "."""
    pairs = []
    for line in (text or "").splitlines():
        if " = " not in line:
            continue
        k, v = line.split(" = ", 1)
        pairs.append((k.strip(), v.split(":", 1)[-1].strip().strip('"')))
    return pairs


def mac_from_fdb_oid(oid: str, base: str) -> str | None:
    """A BRIDGE-MIB FDB entry encodes the learned MAC in its OID tail as six
    decimal octets. Returns the normalized MAC, or None if the tail isn't one."""
    octs = oid.rsplit(base, 1)[-1].strip(".").split(".")
    if len(octs) < 6:
        return None
    try:
        return norm_mac(":".join(f"{int(o):02x}" for o in octs[-6:]))
    except ValueError:
        return None


class UnifiSnmpCollector(Collector):
    name = "unifi_snmp"

    def collect(self) -> list[dict]:
        if not shutil.which("snmpwalk"):
            log.warning("snmpwalk not found; install net-snmp to get port topology")
            return []
        items: list[dict] = []
        for sw in self.cfg.get("switches", []):
            items.extend(self._walk_switch(sw))
        return self._tag(items)

    def _snmpwalk(self, sw: dict, oid: str) -> list[tuple[str, str]]:
        cmd = [
            "snmpwalk", "-v", str(sw.get("version", "2c")),
            "-c", sw.get("community", "public"), "-On",
            sw["host"], oid,
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("snmpwalk %s %s failed: %s", sw["host"], oid, e)
            return []
        return parse_walk(out.stdout)

    def _walk_switch(self, sw: dict) -> list[dict]:
        sw_id = sw.get("name", sw["host"])
        ts = now_iso()
        items: list[dict] = [{
            "name": sw_id, "ip": sw["host"], "nodekind": "switch",
            "online": True, "last_seen": ts, "id_hint": sw_id,
        }]

        # ifIndex -> port name
        ifnames = {k.rsplit(".", 1)[-1]: v for k, v in self._snmpwalk(sw, IF_NAME)}

        # FDB: bridge port -> MACs learned. OID tail encodes the MAC (6 decimal octets)
        for oid, port in self._snmpwalk(sw, DOT1D_FDB):
            mac = mac_from_fdb_oid(oid, DOT1D_FDB)
            if not mac:
                continue
            portname = ifnames.get(port, f"port{port}")
            # link from switch to whatever host owns this MAC (resolved in normalize)
            items.append({
                "kind": "link", "src": sw_id, "dst": mac,
                "linkkind": "l2", "port": portname,
            })
            # ensure the endpoint exists as at least a stub node keyed by mac
            items.append({"mac": mac, "name": mac, "last_seen": ts, "online": True})

        # LLDP neighbors -> uplink edges (switch<->switch/AP)
        names = dict(self._snmpwalk(sw, LLDP_REM_SYSNAME))
        for oid, neigh in names.items():
            if neigh:
                items.append({"name": neigh, "nodekind": "switch", "last_seen": ts})
                items.append({"kind": "link", "src": sw_id, "dst": neigh,
                              "linkkind": "uplink"})
        return items


if __name__ == "__main__":   # ponytail: the OID decode + walk parser, offline
    WALK = """.1.3.6.1.2.1.31.1.1.1.1.1 = STRING: Port 1
.1.3.6.1.2.1.31.1.1.1.1.7 = STRING: "Port 7"
not an assignment line
.1.3.6.1.2.1.31.1.1.1.1.9 = INTEGER: 42"""
    pairs = parse_walk(WALK)
    assert len(pairs) == 3, pairs
    assert pairs[0] == (".1.3.6.1.2.1.31.1.1.1.1.1", "Port 1")
    assert pairs[1][1] == "Port 7", pairs[1]          # quotes stripped
    assert pairs[2][1] == "42"                        # type prefix dropped
    assert parse_walk("") == []

    # 220.166.50.17.34.51 -> dc:a6:32:11:22:33
    oid = DOT1D_FDB + ".220.166.50.17.34.51"
    assert mac_from_fdb_oid(oid, DOT1D_FDB) == "dc:a6:32:11:22:33", mac_from_fdb_oid(oid, DOT1D_FDB)
    # real FDB oids carry the vlan/bridge id first; the MAC is the LAST six octets
    assert mac_from_fdb_oid(DOT1D_FDB + ".1.220.166.50.17.34.51", DOT1D_FDB) == "dc:a6:32:11:22:33"
    assert mac_from_fdb_oid(DOT1D_FDB + ".1.2.3", DOT1D_FDB) is None          # too short
    assert mac_from_fdb_oid(DOT1D_FDB + ".1.2.3.4.5.zz", DOT1D_FDB) is None   # not decimal
    assert mac_from_fdb_oid("", DOT1D_FDB) is None
    print("collectors/unifi_snmp parser self-check ok")
