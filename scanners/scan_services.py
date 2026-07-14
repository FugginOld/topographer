#!/usr/bin/env python3
"""Enumerate this host's containers / compose stacks / running services, as JSON.

Piped alone over SSH by the dashboard (exactly like make_linux_topology.py), so it
must stay single-file self-contained — stdlib only, imports nothing from the repo.
Uses the docker/podman CLI (no socket libs) and systemctl; degrades to empty lists.

    python3 scan_services.py            # -> {"engine","containers":[...],"services":[...]}
    python3 scan_services.py --selftest # parse check, no docker needed
"""
import json
import shutil
import subprocess
import sys

# tab-separated so names/images/status parse cleanly; .Label extracts the compose project
_FMT = ('{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}'
        '\t{{.Label "com.docker.compose.project"}}\t{{.Ports}}')


def _run(cmd: list) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


def parse_ps(out: str, engine: str) -> list:
    """docker/podman `ps` tab output -> list of container dicts."""
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) < 4 or not p[0]:
            continue
        rows.append({"name": p[0], "image": p[1], "state": p[2], "status": p[3],
                     "project": p[4] if len(p) > 4 else "",
                     "ports": p[5] if len(p) > 5 else "", "engine": engine})
    return rows


def containers() -> tuple:
    engine = "docker" if shutil.which("docker") else ("podman" if shutil.which("podman") else None)
    if not engine:
        return None, []
    return engine, parse_ps(_run([engine, "ps", "-a", "--format", _FMT]), engine)


def services() -> list:
    if not shutil.which("systemctl"):
        return []
    out = _run(["systemctl", "list-units", "--type=service", "--state=running",
                "--no-legend", "--no-pager", "--plain"])
    names = []
    for line in out.splitlines():
        unit = line.split(None, 1)[0] if line.split() else ""
        if unit.endswith(".service"):
            names.append(unit[:-len(".service")])
    return names[:60]


def main() -> None:
    engine, cons = containers()
    print(json.dumps({"engine": engine, "containers": cons, "services": services()}))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sample = ("web\tnginx:latest\trunning\tUp 2 hours\tmystack\t0.0.0.0:80->80/tcp\n"
                  "db\tpostgres:16\texited\tExited (0) 1h ago\tmystack\t\n"
                  "stray\tredis\trunning\tUp 5m\t\t6379/tcp\n")
        r = parse_ps(sample, "docker")
        assert len(r) == 3 and r[0]["name"] == "web" and r[0]["project"] == "mystack", r
        assert r[1]["state"] == "exited" and r[2]["project"] == "", r
        print("scan_services self-check ok")
        sys.exit()
    main()
