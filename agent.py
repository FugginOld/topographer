#!/usr/bin/env python3
"""Report this machine's hardware topology to a central dashboard server.

Runs the OS-appropriate generator (make_pc_topology.py on Windows,
make_linux_topology.py on Linux) and POSTs the JSON to the server's
/api/ingest. Each host stores as one stable entry (re-pushes overwrite),
so schedule this to keep the map fresh:

    Windows : Task Scheduler -> python agent.py --server http://dash.lan:8770
    Linux   : systemd timer / cron -> same command

    python agent.py --server http://HOST:8770 [--name NAME] [--token SECRET]
    python agent.py --server http://HOST:8770 --file out/topologies/x.json   # re-push, no scan

--name defaults to this machine's hostname. --token must match the server's
TOPO_TOKEN env var if it sets one. Needs make_*_topology.py alongside this file.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import telemetry as _tele   # noqa: E402  same sampler topology_server.py uses

GENERATOR = "make_pc_topology.py" if sys.platform.startswith("win") else "make_linux_topology.py"


def generate(name: str) -> dict:
    fd, out = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, GENERATOR), "--out", out, "--name", name],
                           capture_output=True, text=True)
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            sys.exit((r.stderr or r.stdout or f"{GENERATOR} produced nothing").strip())
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        if os.path.exists(out):
            os.remove(out)


def _post(server: str, path: str, obj: dict, token: str, timeout: int = 30) -> dict:
    body = json.dumps(obj).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Token"] = token
    req = urllib.request.Request(server.rstrip("/") + path, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def push(server: str, topo: dict, token: str) -> dict:
    return _post(server, "/api/ingest", topo, token)


def report(server: str, name: str, token: str, interval: float, topo_every: float) -> None:
    """Daemon: push topology, then push live telemetry every `interval` seconds,
    re-scanning the topology every `topo_every` seconds."""
    try:
        tid = push(server, generate(name), token)["id"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if e.code == 404:
            sys.exit(f"server returned 404 for /api/ingest — {server} is running an OLD topology_server.py.\n"
                     f"On the server: stop it, `git pull`, and restart (.\\server.ps1 or python renderers/html/topology_server.py).")
        sys.exit(f"server rejected the push: HTTP {e.code} {detail}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"could not reach {server}: {e}  (is topology_server.py running? firewall open on 8770?)")
    print(f"reporting '{name}' (id={tid}) to {server} every {interval}s; Ctrl-C to stop")
    last_topo = time.monotonic()
    while True:
        try:
            _post(server, "/api/telemetry", {"host": tid, **_tele.sample()}, token, timeout=10)
        except Exception as e:      # keep the loop alive across transient failures
            print("telemetry push failed:", e)
        if time.monotonic() - last_topo >= topo_every:
            try:
                push(server, generate(name), token); last_topo = time.monotonic()
            except Exception as e:
                print("topology re-push failed:", e)
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True, help="dashboard base URL, e.g. http://dash.lan:8770")
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--token", default=os.environ.get("TOPO_TOKEN", ""))
    ap.add_argument("--file", help="push an existing topology JSON instead of scanning")
    ap.add_argument("--report", action="store_true",
                    help="stay running: push live telemetry on an interval (per-host HUD)")
    ap.add_argument("--interval", type=float, default=3.0, help="telemetry push seconds (--report)")
    ap.add_argument("--topo-every", type=float, default=300.0, help="re-scan topology seconds (--report)")
    args = ap.parse_args()

    if args.report:
        return report(args.server, args.name, args.token, args.interval, args.topo_every)

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            topo = json.load(fh)
        topo["name"] = args.name or topo.get("name")
    else:
        topo = generate(args.name)

    try:
        res = push(args.server, topo, args.token)
    except urllib.error.HTTPError as e:
        sys.exit(f"server rejected push: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"could not reach {args.server}: {e}")
    print(f"pushed '{topo.get('name')}' ({len(topo.get('nodes', []))} modules) -> {args.server}  {res}")


if __name__ == "__main__":
    main()
