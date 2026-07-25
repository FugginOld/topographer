"""Live host discovery via arp-scan (preferred) or nmap -sn fallback.

Requires one of:
  - arp-scan  (sudo apt install arp-scan)   -- gives IP+MAC+vendor in one shot
  - nmap      (sudo apt install nmap)        -- IP+MAC, no vendor

Config (config.yaml):
  arpscan:
    enabled: true
    interfaces: [eth0.10, eth0.30]     # optional; arp-scan --localnet per if
    subnets: [10.0.30.0/24]            # optional; explicit targets
    use_sudo: true
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

from .base import Collector
from core.schema import now_iso

log = logging.getLogger("collector.arpscan")

# arp-scan line: "10.0.30.10\taa:bb:cc:dd:ee:ff\tVendor string"
_ARP_RE = re.compile(
    r"^(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F:]{17})\s*(.*)$"
)
# nmap -sn "Nmap scan report for host (10.0.30.10)" + "MAC Address: AA:.. (Vendor)"
_NMAP_IP = re.compile(r"Nmap scan report for (?:(\S+) )?\(?(\d{1,3}(?:\.\d{1,3}){3})\)?")
_NMAP_MAC = re.compile(r"MAC Address: ([0-9A-Fa-f:]{17}) \((.*)\)")


def parse_arpscan(text: str, ts: str) -> list[dict]:
    """arp-scan output -> raw node dicts. Ignores its banner and summary lines."""
    out = []
    for line in (text or "").splitlines():
        m = _ARP_RE.match(line.strip())
        if not m:
            continue
        ip, mac, vendor = m.group(1), m.group(2), (m.group(3) or "").strip()
        out.append({"ip": ip, "mac": mac, "name": ip,
                    "vendor": vendor or None, "online": True, "last_seen": ts})
    return out


def parse_nmap(text: str, ts: str) -> list[dict]:
    """`nmap -sn` output -> raw node dicts. A host is announced by its scan-report
    line and its MAC arrives on a *later* line, so each host stays open until the
    next one starts — the one bit of state in this file."""
    nodes: list[dict] = []
    cur = None
    for line in (text or "").splitlines():
        ipm = _NMAP_IP.search(line)
        if ipm:
            cur = {"ip": ipm.group(2), "name": ipm.group(1) or ipm.group(2),
                   "online": True, "last_seen": ts}
            nodes.append(cur)
        macm = _NMAP_MAC.search(line)
        if macm and cur is not None:
            cur["mac"] = macm.group(1)
            cur["vendor"] = macm.group(2)
    return nodes


class ArpScanCollector(Collector):
    name = "arpscan"

    def collect(self) -> list[dict]:
        if shutil.which("arp-scan"):
            return self._tag(self._arpscan())
        if shutil.which("nmap"):
            return self._tag(self._nmap())
        log.warning("neither arp-scan nor nmap found on PATH")
        return []

    def _run(self, cmd: list[str]) -> str:
        if self.cfg.get("use_sudo"):
            cmd = ["sudo", "-n", *cmd]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, check=False
            )
            if out.returncode != 0:
                log.warning("%s exited %s: %s", cmd[0], out.returncode, out.stderr.strip())
            return out.stdout
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("failed to run %s: %s", cmd, e)
            return ""

    def _targets(self) -> list[list[str]]:
        cmds = []
        for iface in self.cfg.get("interfaces", []):
            cmds.append(["arp-scan", "--localnet", f"--interface={iface}"])
        for subnet in self.cfg.get("subnets", []):
            cmds.append(["arp-scan", subnet])
        if not cmds:  # default: whole local net on default iface
            cmds.append(["arp-scan", "--localnet"])
        return cmds

    def _arpscan(self) -> list[dict]:
        seen: dict[str, dict] = {}
        ts = now_iso()
        for cmd in self._targets():                 # dedup across runs stays here
            for n in parse_arpscan(self._run(cmd), ts):
                seen[n["mac"].lower()] = n
        return list(seen.values())

    def _nmap(self) -> list[dict]:
        ts = now_iso()
        nodes: list[dict] = []
        for subnet in (self.cfg.get("subnets") or ["--localnet"]):
            nodes += parse_nmap(self._run(["nmap", "-sn", subnet]), ts)
        return nodes


if __name__ == "__main__":   # ponytail: the two parsers, on canned output
    import os
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "tests", "fixtures", "arpscan_sample.txt")
    rows = parse_arpscan(open(fixture, encoding="utf-8").read(), "T")
    assert [r["ip"] for r in rows] == ["10.0.30.1", "10.0.30.10", "10.0.30.11"], rows
    assert rows[1]["mac"] == "dc:a6:32:11:22:33" and rows[1]["vendor"] == "Raspberry Pi Trading Ltd"
    assert rows[0]["vendor"] == "(Unknown)" and rows[0]["name"] == "10.0.30.1"
    assert all(r["online"] and r["last_seen"] == "T" for r in rows)
    assert parse_arpscan("", "T") == [] and parse_arpscan("garbage lines", "T") == []

    NMAP = """Starting Nmap 7.93
Nmap scan report for rpi5b (10.0.30.10)
Host is up (0.0021s latency).
MAC Address: DC:A6:32:11:22:33 (Raspberry Pi Trading)
Nmap scan report for 10.0.30.11
Host is up (0.0018s latency).
MAC Address: 3C:FD:FE:44:55:66 (Intel Corporate)
Nmap scan report for 10.0.30.99
Host is up.
Nmap done: 256 IP addresses (3 hosts up)"""
    hosts = parse_nmap(NMAP, "T")
    assert [h["ip"] for h in hosts] == ["10.0.30.10", "10.0.30.11", "10.0.30.99"], hosts
    assert hosts[0]["name"] == "rpi5b" and hosts[0]["mac"] == "DC:A6:32:11:22:33"
    assert hosts[1]["name"] == "10.0.30.11"                  # no reverse name -> the ip
    assert "mac" not in hosts[2], hosts[2]                   # the local host reports no MAC
    # a MAC line before any host must not attach to the previous scan's last host
    assert parse_nmap("MAC Address: AA:BB:CC:DD:EE:FF (X)", "T") == []
    print("collectors/arpscan parser self-check ok")
