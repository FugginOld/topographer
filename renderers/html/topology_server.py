#!/usr/bin/env python3
"""Serve the topology dashboard + a store of saved topologies on :8770.

Browsers block fetch() over file://, so the dashboard needs a real HTTP origin.
This serves renderers/html/ plus a tiny API over out/topologies/*.json:

    GET  /api/list            -> [{id,name,generated,nodes,vlans}, ...]
    GET  /t/<id>.json         -> a saved topology
    POST /api/generate {name} -> run the hardware scanner, save as a new topology
    POST /api/delete   {id}   -> remove a saved topology

    python renderers/html/topology_server.py [--port 8770]

GENERATE runs make_pc_topology.py to map this PC's hardware fabric.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STORE = os.path.join(ROOT, "out", "topologies")


INGEST_TOKEN = os.environ.get("TOPO_TOKEN", "")   # optional shared secret for /api/ingest

_server_ip_cache: str | None = None


def server_ip() -> str:
    """This server's own LAN IPv4 (cached). Used for locally-generated cards,
    which have no push 'source'. No traffic sent; just picks the egress iface."""
    global _server_ip_cache
    if _server_ip_cache is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            _server_ip_cache = s.getsockname()[0]
        except OSError:
            _server_ip_cache = ""
        finally:
            s.close()
    return _server_ip_cache or ""


def store_path(tid: str) -> str:
    """Resolve <tid>.json inside STORE, refusing any id that escapes it.

    The single barrier for every user-supplied id: strip directory parts,
    then confirm the resolved file really sits directly in STORE (blocks
    '..', absolute paths, and symlink tricks). Raises ValueError otherwise.
    """
    name = os.path.basename(tid)
    # allowlist: an id is only ever a slug/hostname — reject anything else up
    # front, so no separators or traversal sequences can reach the filesystem.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("bad topology id")
    fp = os.path.realpath(os.path.join(STORE, name + ".json"))
    if os.path.dirname(fp) != os.path.realpath(STORE):
        raise ValueError("bad topology id")
    return fp


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "topology"
    return f"{s}-{int(time.time())}"  # timestamp keeps ids unique


def stable_slug(name: str) -> str:
    """No timestamp — one stable entry per host, so re-pushes overwrite."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "host"


def save_ingest(topo: dict, remote: str) -> dict:
    """Store a topology pushed by a remote agent, keyed by its name (hostname)."""
    name = (topo.get("name") or "host").strip()
    tid = stable_slug(name)
    topo["name"] = name
    topo["source"] = remote
    os.makedirs(STORE, exist_ok=True)
    with open(store_path(tid), "w", encoding="utf-8") as fh:
        json.dump(topo, fh, indent=2)
    return {"id": tid, "name": name}


def entry(path: str) -> dict:
    """One sidebar list row from a saved file (cheap read, tolerates junk)."""
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    fid = os.path.splitext(os.path.basename(path))[0]
    # the network map is saved as id "network"; also honour the kind tag so
    # pre-tag files still categorize correctly without a re-scan
    is_network = d.get("kind") == "network" or fid == "network"
    return {
        "id": fid,
        "name": d.get("name") or fid,
        "generated": d.get("generated", ""),
        "modules": len(d.get("nodes", [])),
        "kind": "network" if is_network else "host",
        # reported hosts carry their push origin; a local machine card has none,
        # so fall back to this server's own IP. The network map gets no IP.
        "ip": "" if is_network else (d.get("source") or server_ip()),
    }


def list_topos() -> list[dict]:
    if not os.path.isdir(STORE):
        return []
    out = []
    for f in sorted(os.listdir(STORE)):
        if f.endswith(".json"):
            try:
                out.append(entry(os.path.join(STORE, f)))
            except Exception:
                pass  # skip unreadable/corrupt files rather than 500
    out.sort(key=lambda e: e["generated"], reverse=True)
    return out


# pick the collector for whatever OS the dashboard is served from
GENERATOR = "make_pc_topology.py" if sys.platform.startswith("win") else "make_linux_topology.py"


def generate(name: str) -> dict:
    """Read this host's hardware fabric and save it as a new topology."""
    tid = slug(name)
    os.makedirs(STORE, exist_ok=True)
    out = store_path(tid)
    r = subprocess.run(
        [sys.executable, GENERATOR, "--out", out, "--name", name],
        cwd=ROOT, capture_output=True, text=True,
    )
    if not os.path.exists(out):
        raise RuntimeError((r.stderr or r.stdout or f"{GENERATOR} produced nothing").strip()[-800:])
    return {"id": tid, "name": name}


def _node_meta(n: dict) -> dict:
    """Detail rows for a node: its collector-supplied meta first, then basics."""
    basics = {k: v for k, v in (("ip", n.get("ip")), ("mac", n.get("mac")),
              ("vendor", n.get("vendor")), ("type", n.get("kind")),
              ("online", "yes" if n.get("online") else "no"),
              ("last seen", n.get("last_seen"))) if v}
    return {**(n.get("meta") or {}), **basics}


def _network_cards(d: dict) -> list[dict]:
    """Reshape a network JSON into the dashboard's node/parent tree. When a
    gateway is known the spine is WAN -> Gateway -> VLAN -> host; otherwise it
    falls back to a synthetic NETWORK root (e.g. a ping-sweep-only scan)."""
    import ipaddress
    zones = d.get("zones", [])
    znets = []
    for z in zones:
        try:
            znets.append((z, ipaddress.ip_network(z.get("subnet", ""), strict=False)))
        except ValueError:
            znets.append((z, None))

    nodes = d.get("nodes", [])
    gw = next((n for n in nodes if n.get("kind") == "firewall"), None)
    wan = next((n for n in nodes if n.get("kind") == "wan"), None)
    skip = {id(gw), id(wan)}                      # placed explicitly, not as hosts

    cards = []
    if gw:
        gw_id = gw.get("id") or gw.get("mac") or gw.get("ip")
        if wan:
            cards.append({"id": "wan", "label": "WAN / INTERNET", "kind": "wan",
                          "cls": "mgmt", "sub": wan.get("ip") or "", "meta": _node_meta(wan)})
        cards.append({"id": gw_id, "parent": ("wan" if wan else None),
                      "label": gw.get("name") or gw_id, "sub": gw.get("ip") or "",
                      "cls": "mgmt", "kind": "firewall", "meta": _node_meta(gw)})
        zone_root = gw_id
    else:
        cards.append({"id": "net", "label": "NETWORK", "kind": "root"})
        zone_root = "net"

    for z in zones:
        cards.append({"id": f"vlan{z['vid']}", "parent": zone_root, "label": z["name"],
                      "sub": z.get("subnet", ""), "cls": z.get("cls", "unknown"), "kind": "zone",
                      "meta": {"vlan": z["vid"], "subnet": z.get("subnet", ""),
                               "policy": z.get("policy", "")}})

    for n in nodes:
        if id(n) in skip:
            continue
        ip, parent, cls = n.get("ip"), zone_root, None
        for z, net in znets:
            try:
                if net and ip and ipaddress.ip_address(ip) in net:
                    parent, cls = f"vlan{z['vid']}", z.get("cls", "unknown")
                    break
            except ValueError:
                pass
        cards.append({"id": n.get("id") or ip, "parent": parent,
                      "label": n.get("name") or ip, "sub": n.get("ip") or "",
                      "cls": cls, "kind": n.get("kind", "host"), "meta": _node_meta(n)})
    return cards


def generate_network(subnet: str | None = None) -> dict:
    """Ping-sweep the network and save it as the 'network' topology card. Uses
    config.yaml if present (honours the user's zones/collectors), else a throwaway
    config that enables pingsweep with auto subnet detection — zero setup."""
    os.makedirs(STORE, exist_ok=True)
    cfg_path = os.path.join(ROOT, "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(ROOT, "out", ".netscan.yaml")
        subs = f"[{subnet}]" if subnet else "[]"
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("offline_after_minutes: 30\npingsweep:\n  enabled: true\n"
                     f"  subnets: {subs}\n  resolve: true\n")
    r = subprocess.run(
        [sys.executable, "make_network_topology.py", "--config", cfg_path, "--outdir", "out"],
        cwd=ROOT, capture_output=True, text=True,
    )
    src = os.path.join(ROOT, "out", "topology.json")
    if not os.path.exists(src):
        raise RuntimeError((r.stderr or r.stdout or "scan produced nothing").strip()[-800:])
    with open(src, encoding="utf-8") as fh:
        d = json.load(fh)
    topo = {"name": "Network", "kind": "network",
            "generated": d.get("generated"), "nodes": _network_cards(d)}
    with open(store_path("network"), "w", encoding="utf-8") as fh:
        json.dump(topo, fh, indent=2)
    return {"id": "network", "name": "Network", "nodes": len(topo["nodes"])}


sys.path.insert(0, ROOT)    # repo root — where local_telemetry.py and the generators live
import local_telemetry as _tele   # noqa: E402  shared local-metrics sampler

_tele_cache = {"t": 0.0, "data": dict(_tele.ZERO)}
_host_tele: dict[str, dict] = {}   # host id -> {"t": monotonic, "data": {...}}
FRESH = 15.0                       # seconds a pushed sample stays "live"


def telemetry_local() -> dict:
    """This (server) machine's live metrics, cached briefly."""
    if time.monotonic() - _tele_cache["t"] < 1.5:
        return _tele_cache["data"]
    data = _tele.sample()
    _tele_cache.update(t=time.monotonic(), data=data)
    return data


def _is_remote(tid: str) -> bool:
    """A topology carrying a 'source' field was pushed by a remote agent."""
    try:
        with open(store_path(tid), encoding="utf-8") as fh:
            return "source" in json.load(fh)
    except (OSError, ValueError):
        return False


def host_telemetry(tid: str) -> dict:
    """Telemetry for the selected host: its pushed sample if fresh, else the
    server's own metrics for a local topology, else stale zeros for a remote
    host that isn't reporting."""
    h = _host_tele.get(tid)
    if h and time.monotonic() - h["t"] < FRESH:
        return {**h["data"], "live": True}
    if not _is_remote(tid):
        return {**telemetry_local(), "live": True}
    return {**_tele.ZERO, "stale": True}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def end_headers(self):
        # local dashboard: never let a browser cache stale HTML/JS
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path.split("?")[0] == "/api/list":
            return self._send(200, list_topos())
        if self.path.split("?")[0] == "/api/telemetry":
            host = parse_qs(urlparse(self.path).query).get("host", [""])[0]
            try:
                return self._send(200, host_telemetry(host) if host else telemetry_local())
            except Exception as e:
                return self._send(200, {**_tele.ZERO, "error": str(e)})
        if self.path.startswith("/t/"):
            tid = os.path.basename(self.path.split("?")[0])[:-5]  # strip .json
            try:
                fp = store_path(tid)
            except ValueError:
                return self._send(404, {"error": "not found"})
            if not os.path.isfile(fp):
                return self._send(404, {"error": "not found"})
            with open(fp, encoding="utf-8") as fh:
                return self._send(200, json.load(fh))
        return super().do_GET()

    def do_POST(self):
        try:
            if self.path == "/api/generate":
                name = (self._body().get("name") or "topology").strip()
                return self._send(200, generate(name))
            if self.path == "/api/generate-network":
                subnet = (self._body().get("subnet") or "").strip() or None
                return self._send(200, generate_network(subnet))
            if self.path == "/api/delete":
                try:
                    fp = store_path(self._body().get("id", ""))
                except ValueError:
                    return self._send(200, {"ok": True})  # nothing to delete
                if os.path.isfile(fp):
                    os.remove(fp)
                return self._send(200, {"ok": True})
            if self.path == "/api/ingest":
                if INGEST_TOKEN and self.headers.get("X-Token") != INGEST_TOKEN:
                    return self._send(403, {"error": "bad or missing token"})
                topo = self._body()
                if not isinstance(topo, dict) or not topo.get("nodes"):
                    return self._send(400, {"error": "body must be a topology with a 'nodes' array"})
                return self._send(200, save_ingest(topo, self.client_address[0]))
            if self.path == "/api/telemetry":
                if INGEST_TOKEN and self.headers.get("X-Token") != INGEST_TOKEN:
                    return self._send(403, {"error": "bad or missing token"})
                b = self._body()
                hid = stable_slug(b.get("host", ""))
                if not hid:
                    return self._send(400, {"error": "missing host"})
                data = {k: b.get(k, z) for k, z in _tele.ZERO.items()}
                _host_tele[hid] = {"t": time.monotonic(), "data": data}
                return self._send(200, {"ok": True, "host": hid})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "unknown route"})

    def log_message(self, *a):  # quieter console
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    os.makedirs(STORE, exist_ok=True)
    with http.server.ThreadingHTTPServer(("", args.port), Handler) as httpd:
        print(f"dashboard on http://localhost:{args.port}  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
