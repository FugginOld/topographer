"""Friendly names from Pi-hole custom DNS or a hosts file.

Emits name-only enrichment records keyed by IP so bare-IP nodes from arp-scan
get readable labels during normalize.

Config:
  dns:
    enabled: true
    hosts_files: [/etc/pihole/custom.list, /etc/hosts]
    # or pull from Pi-hole API:
    pihole_url: http://10.0.10.5/admin
    pihole_token: "..."
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse

from . import transport
from .base import Collector

log = logging.getLogger("collector.dns")

_HOSTLINE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+(\S+)")


def parse_hosts(text: str) -> dict[str, str]:
    """A hosts file -> {ip: short name}. Comments are skipped and an FQDN is cut to
    its first label, so the map joins on the same names other collectors report."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _HOSTLINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).split(".")[0]
    return out


def pihole_rows(payload) -> dict[str, str]:
    """Pi-hole customdns rows -> {ip: short name}. Anything unexpected -> {}."""
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    return {r[0]: r[1].split(".")[0] for r in rows
            if isinstance(r, (list, tuple)) and len(r) >= 2}


class DnsCollector(Collector):
    name = "dns"

    def collect(self) -> list[dict]:
        mapping: dict[str, str] = {}
        for path in self.cfg.get("hosts_files", []):
            mapping.update(self._parse_hosts(path))
        if self.cfg.get("pihole_url"):
            mapping.update(self._pihole())
        return self._tag(
            [{"ip": ip, "name": name} for ip, name in mapping.items()]
        )

    def _parse_hosts(self, path: str) -> dict[str, str]:
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as fh:
                return parse_hosts(fh.read())
        except OSError as e:
            log.warning("could not read %s: %s", path, e)
            return {}

    def _pihole(self) -> dict[str, str]:
        auth = urllib.parse.quote(self.cfg.get("pihole_token", ""))
        url = self.cfg["pihole_url"].rstrip("/") + f"/api.php?customdns&action=get&auth={auth}"
        return pihole_rows(transport.get_json(url, timeout=10))


if __name__ == "__main__":   # ponytail: the two name sources, offline
    HOSTS = """# comment line
127.0.0.1	localhost
10.0.30.10	rpi5b.lan rpi5b
   # indented comment
10.0.30.11  nas.home.arpa
garbage"""
    m = parse_hosts(HOSTS)
    assert m["10.0.30.10"] == "rpi5b", m          # FQDN cut to its first label
    assert m["10.0.30.11"] == "nas", m
    assert m["127.0.0.1"] == "localhost"
    assert parse_hosts("") == {}

    assert pihole_rows({"data": [["10.0.30.5", "pve.lan"], ["10.0.30.6", "nas"]]}) == {
        "10.0.30.5": "pve", "10.0.30.6": "nas"}
    assert pihole_rows({"data": [["short"]]}) == {}       # malformed row skipped
    assert pihole_rows(None) == {} and pihole_rows([]) == {}
    print("collectors/dns parser self-check ok")
