#!/usr/bin/env python3
"""Enumerate this host's containers / compose stacks / running services, as JSON.

Piped alone over SSH by the dashboard (exactly like make_linux_topo.py), so it
must stay single-file self-contained — stdlib only, imports nothing from the repo.
Uses the docker/podman CLI (no socket libs) and systemctl; degrades to empty lists.

    python3 scan_services.py            # -> {"engine","containers":[...],"services":[...]}
    python3 scan_services.py --selftest # parse check, no docker needed
"""
import json
import shutil
import subprocess
import sys

# tab-separated so names/images/status parse cleanly; .Label extracts the compose
# project and (on Unraid) the container's icon URL + Web UI template
_FMT = ('{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}'
        '\t{{.Label "com.docker.compose.project"}}\t{{.Ports}}'
        '\t{{.Label "net.unraid.docker.icon"}}\t{{.Label "net.unraid.docker.webui"}}')


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
                     "ports": p[5] if len(p) > 5 else "",
                     "icon": p[6] if len(p) > 6 else "",       # Unraid: net.unraid.docker.icon
                     "webui": p[7] if len(p) > 7 else "",      # Unraid: net.unraid.docker.webui
                     "engine": engine})
    return rows


def parse_stats(out: str) -> dict:
    """`docker/podman stats --no-stream` tab output -> {name: {cpu, mem, memp}}.
    Only running containers appear; mem keeps just the used side of 'used / limit',
    memp is the memory percent (of the container limit, or host RAM if unlimited)."""
    s = {}
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 3 and p[0]:
            s[p[0]] = {"cpu": p[1].strip(), "mem": p[2].split(" / ")[0].strip(),
                       "memp": p[3].strip() if len(p) > 3 else ""}
    return s


def containers() -> tuple:
    engine = "docker" if shutil.which("docker") else ("podman" if shutil.which("podman") else None)
    if not engine:
        return None, []
    cons = parse_ps(_run([engine, "ps", "-a", "--format", _FMT]), engine)
    stats = parse_stats(_run([engine, "stats", "--no-stream", "--format",
                              '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}']))
    for c in cons:
        c.update(stats.get(c["name"], {}))     # running containers gain cpu/mem
    return engine, cons


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
        sample = ("web\tnginx:latest\trunning\tUp 2 hours\tmystack\t0.0.0.0:80->80/tcp"
                  "\thttp://icon.png\thttp://[IP]:[PORT:80]\n"
                  "db\tpostgres:16\texited\tExited (0) 1h ago\tmystack\t\n"
                  "stray\tredis\trunning\tUp 5m\t\t6379/tcp\n")
        r = parse_ps(sample, "docker")
        assert len(r) == 3 and r[0]["name"] == "web" and r[0]["project"] == "mystack", r
        assert r[0]["icon"] == "http://icon.png" and r[0]["webui"] == "http://[IP]:[PORT:80]", r
        assert r[1]["state"] == "exited" and r[2]["project"] == "" and r[2]["icon"] == "", r
        st = parse_stats("web\t3.20%\t1.2GiB / 15.6GiB\t7.69%\ndb\t0.00%\t20MiB / 15.6GiB\t0.13%\n")
        assert st["web"] == {"cpu": "3.20%", "mem": "1.2GiB", "memp": "7.69%"}, st
        print("scan_services self-check ok")
        sys.exit()
    main()
