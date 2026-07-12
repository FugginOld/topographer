"""Host discovery by pinging every address in a subnet, then reading the ARP
cache for MACs. Pure stdlib + the OS `ping`/`arp` — no arp-scan, nmap, or sudo,
so this is the collector that works on a stock Windows box.

Config (config.yaml):
  pingsweep:
    enabled: true
    subnets: [10.0.10.0/24, 10.0.30.0/24]   # required; what to sweep
    timeout_ms: 500                          # per-host ping wait
    workers: 64                              # parallel pings
    resolve: true                            # reverse-DNS responders for names

MAC comes from the OS ARP cache (populated by the ping itself), so vendor is
filled downstream by enrich() via OUI. A host that answers ping but has no ARP
entry (routed/off-subnet) still shows up as an online node without a MAC.
"""
from __future__ import annotations

import ipaddress
import logging
import platform
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .base import Collector
from core.schema import now_iso

log = logging.getLogger("collector.pingsweep")

_IS_WIN = platform.system() == "Windows"
# any "aa:bb:.." or "aa-bb-.." 6-octet MAC
_MAC_RE = re.compile(r"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})")
_IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


def _local_ipv4() -> str | None:
    """This host's primary IPv4 (no traffic sent; just picks the egress iface)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class PingSweepCollector(Collector):
    name = "pingsweep"

    def collect(self) -> list[dict]:
        hosts = self._hosts()
        if not hosts:
            log.warning("pingsweep: no subnets configured, nothing to sweep")
            return []
        timeout = int(self.cfg.get("timeout_ms", 500))
        workers = int(self.cfg.get("workers", 64))
        log.info("pingsweep: %d addresses across %d workers", len(hosts), workers)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            alive = [ip for ip, up in zip(hosts, pool.map(
                lambda ip: self._ping(ip, timeout), hosts)) if up]

        arp = self._arp_table()
        resolve = self.cfg.get("resolve", True)
        ts = now_iso()
        nodes = []
        for ip in alive:
            mac = arp.get(ip)
            name = self._rdns(ip) if resolve else ip
            nodes.append({
                "ip": ip,
                "mac": mac,
                "name": name or ip,
                "online": True,
                "last_seen": ts,
            })
        log.info("pingsweep: %d hosts responded", len(nodes))
        return nodes

    def _hosts(self) -> list[str]:
        subnets = self.cfg.get("subnets") or []
        if not subnets:
            # zero-config: sweep this machine's own /24 so the dashboard button
            # works with no setup. ponytail: assumes /24; set subnets for other masks.
            ip = _local_ipv4()
            if ip:
                subnets = [f"{ip}/24"]
                log.info("pingsweep: no subnets configured, auto-sweeping %s", subnets[0])
        out: list[str] = []
        for subnet in subnets:
            try:
                net = ipaddress.ip_network(subnet, strict=False)
            except ValueError as e:
                log.warning("pingsweep: bad subnet %s: %s", subnet, e)
                continue
            # .hosts() drops network/broadcast; /32 falls back to the address
            out.extend(str(h) for h in (net.hosts() or [net.network_address]))
        return out

    def _ping(self, ip: str, timeout_ms: int) -> bool:
        if _IS_WIN:
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
        else:
            # ponytail: posix -W is seconds (Linux); macOS wants -t, close enough
            cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
        try:
            r = subprocess.run(cmd, capture_output=True,
                               timeout=timeout_ms / 1000 + 2)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _rdns(self, ip: str) -> str | None:
        try:
            return socket.gethostbyaddr(ip)[0]
        except (OSError, socket.herror):
            return None

    def _arp_table(self) -> dict[str, str]:
        """ip -> normalized MAC from the OS ARP cache (`arp -a`)."""
        try:
            out = subprocess.run(["arp", "-a"], capture_output=True,
                                 text=True, timeout=15).stdout
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("pingsweep: arp -a failed: %s", e)
            return {}
        table: dict[str, str] = {}
        for line in out.splitlines():
            ipm, macm = _IP_RE.search(line), _MAC_RE.search(line)
            if ipm and macm:
                table[ipm.group(1)] = macm.group(1).replace("-", ":").lower()
        return table


if __name__ == "__main__":  # ponytail: self-check, no framework
    c = PingSweepCollector({"subnets": ["127.0.0.0/30"], "resolve": False})
    hosts = c._hosts()
    assert hosts == ["127.0.0.1", "127.0.0.2"], hosts
    assert c._ping("127.0.0.1", 500) is True, "loopback must answer ping"
    line = "  10.0.30.10   aa-bb-cc-dd-ee-ff   dynamic"
    t = _IP_RE.search(line).group(1), _MAC_RE.search(line).group(1)
    assert t == ("10.0.30.10", "aa-bb-cc-dd-ee-ff"), t
    print("pingsweep self-check ok")
