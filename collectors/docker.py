"""Docker container detail on server hosts, over SSH.

Adds container nodes (host= the docker host id) so the renderer can nest
containers under fugginnas / ollama / docker hosts. Read-only: runs `docker ps`
and `docker network inspect`.

Config:
  docker:
    enabled: true
    hosts:
      - {ssh: joe@10.0.30.10, name: fugginnas}
      - {ssh: joe@10.0.30.11, name: ollama-01}
    ssh_opts: "-o ConnectTimeout=8 -o BatchMode=yes"

BatchMode requires key-based SSH (no password prompt). Keep credentials in your
ssh config/agent, not here.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess

from .base import Collector
from core.schema import now_iso

log = logging.getLogger("collector.docker")

_PS_FMT = '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}"}'


def parse_ps(text: str, hid: str, ts: str) -> list[dict]:
    """`docker ps` JSON-per-line -> a container node + a link nesting it under its
    host. Malformed lines are skipped: the remote shell can interleave warnings."""
    items: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        cname = c.get("name", "?")
        items.append({"name": f"{cname}", "nodekind": "container", "host": hid,
                      "online": "Up" in c.get("status", ""), "last_seen": ts,
                      "tags": ["container"]})
        items.append({"kind": "link", "src": hid, "dst": cname, "linkkind": "host"})
    return items


class DockerCollector(Collector):
    name = "docker"

    def _ssh(self, target: str, remote_cmd: str) -> str:
        opts = shlex.split(self.cfg.get("ssh_opts", "-o ConnectTimeout=8 -o BatchMode=yes"))
        cmd = ["ssh", *opts, target, remote_cmd]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=25, check=False)
            if out.returncode != 0:
                log.warning("ssh %s: %s", target, out.stderr.strip()[:200])
            return out.stdout
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("ssh %s failed: %s", target, e)
            return ""

    def collect(self) -> list[dict]:
        items: list[dict] = []
        ts = now_iso()
        for host in self.cfg.get("hosts", []):
            target, hid = host["ssh"], host.get("name", host["ssh"])
            items += parse_ps(self._ssh(target, f"docker ps --format '{_PS_FMT}'"), hid, ts)
        return self._tag(items)


if __name__ == "__main__":   # ponytail: the ps line parser, offline
    OUT = ("""{"id":"a1","name":"pihole","image":"pihole/pihole","status":"Up 3 days"}
"""
           """warning: something on stderr got interleaved
"""
           """{"id":"b2","name":"stopped-one","image":"x","status":"Exited (0) 2 days ago"}
"""
           """
""")
    items = parse_ps(OUT, "nas", "T")
    nodes = [i for i in items if i.get("kind") != "link"]
    links = [i for i in items if i.get("kind") == "link"]
    assert [n["name"] for n in nodes] == ["pihole", "stopped-one"], nodes
    assert nodes[0]["online"] is True and nodes[1]["online"] is False
    assert all(n["host"] == "nas" and n["last_seen"] == "T" for n in nodes)
    assert [(l["src"], l["dst"]) for l in links] == [("nas", "pihole"), ("nas", "stopped-one")]
    assert parse_ps("", "nas", "T") == [] and parse_ps("not json", "nas", "T") == []
    print("collectors/docker parser self-check ok")
