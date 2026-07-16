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
        out: dict[str, str] = {}
        if not os.path.exists(path):
            return out
        try:
            with open(path) as fh:
                for line in fh:
                    if line.lstrip().startswith("#"):
                        continue
                    m = _HOSTLINE.match(line)
                    if m:
                        out[m.group(1)] = m.group(2).split(".")[0]
        except OSError as e:
            log.warning("could not read %s: %s", path, e)
        return out

    def _pihole(self) -> dict[str, str]:
        auth = urllib.parse.quote(self.cfg.get("pihole_token", ""))
        url = self.cfg["pihole_url"].rstrip("/") + f"/api.php?customdns&action=get&auth={auth}"
        d = transport.get_json(url, timeout=10)
        rows = d.get("data", []) if isinstance(d, dict) else []
        return {row[0]: row[1].split(".")[0] for row in rows if len(row) >= 2}
