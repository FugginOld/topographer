#!/usr/bin/env python3
"""Serve the topology dashboard + a store of saved topologies on :8770.

Browsers block fetch() over file://, so the dashboard needs a real HTTP origin.
This serves renderers/html/ plus a tiny API over out/topologies/*.json:

    GET  /api/list            -> [{id,name,generated,nodes,vlans}, ...]
    GET  /t/<id>.json         -> a saved topology
    POST /api/generate {name} -> run the hardware scanner, save as a new topology
    POST /api/delete   {id}   -> remove a saved topology

    python renderers/html/topology_server.py [--port 8770]

GENERATE runs scanners/make_pc_topology.py to map this PC's hardware fabric.
"""
from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse, parse_qs

import store   # topology persistence (same dir) — see store.py / CONTEXT.md

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


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


# persistence (the store_path barrier, save/load/list/delete, slug policy) lives
# in store.py. What stays here is the sidebar *row* shaping — a view concern that
# needs server_ip, so it doesn't belong in the store.

def _list_row(tid: str, d: dict) -> dict:
    """One sidebar row from a loaded topology."""
    # the network map is saved as id "network"; honour the kind tag too so
    # pre-tag files still categorize correctly without a re-scan
    is_network = d.get("kind") == "network" or tid == "network"
    return {
        "id": tid,
        "name": d.get("name") or tid,
        "generated": d.get("generated", ""),
        "modules": len(d.get("nodes", [])),
        "kind": "network" if is_network else "host",
        # reported hosts carry their push origin; a local machine card has none,
        # so fall back to this server's own IP. The network map gets no IP.
        "ip": "" if is_network else (d.get("source") or server_ip()),
    }


def list_rows() -> list[dict]:
    rows = []
    for tid in store.ids():
        try:
            rows.append(_list_row(tid, store.load(tid)))
        except Exception:
            pass  # skip unreadable/corrupt files rather than 500
    rows.sort(key=lambda e: e["generated"], reverse=True)
    return rows


# pick the collector for whatever OS the dashboard is served from
GENERATOR = "scanners/make_pc_topology.py" if sys.platform.startswith("win") else "scanners/make_linux_topology.py"


def _cfg_block(key: str) -> dict:
    """A top-level block from config.yaml, or {} if absent/unparseable."""
    path = os.path.join(ROOT, "config.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        return (yaml.safe_load(open(path, encoding="utf-8")) or {}).get(key) or {}
    except Exception:
        return {}


def _remote_scan_cfg() -> dict:
    return _cfg_block("remote_scan")


def gateway_dashboard() -> dict:
    """Live gateway stats for the dashboard panel, from the UniFi API (the key
    already in config.yaml). {'error': ...} when UniFi is off or unreachable."""
    cfg = _cfg_block("unifi")
    if not cfg.get("enabled"):
        return {"error": "UniFi is not enabled in config.yaml (set unifi.enabled: true)."}
    try:
        from collectors.unifi import UnifiCollector
        return UnifiCollector(cfg).dashboard()
    except Exception as e:
        return {"error": f"gateway query failed: {e}"}


def gateway_speedtest() -> dict:
    """Kick off a WAN speedtest on the gateway; the result appears in a later
    /api/gwdash poll (speedtest_status Running -> Idle with new xput values)."""
    cfg = _cfg_block("unifi")
    if not cfg.get("enabled"):
        return {"error": "UniFi is not enabled in config.yaml."}
    try:
        from collectors.unifi import UnifiCollector
        UnifiCollector(cfg).run_speedtest()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def host_services(host: str) -> dict:
    """SSH into a Linux host and enumerate its containers / stacks / services for
    the machine dashboard. Same SSH machinery + IP validation as scan_host."""
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        return {"error": "invalid host — must be an IP address"}
    cfg = _remote_scan_cfg()
    if not cfg.get("enabled"):
        return {"error": "remote SSH scan is off (set remote_scan in config.yaml to enable it)"}
    user = (cfg.get("hosts") or {}).get(host) or cfg.get("user")
    if not user:
        return {"error": f"no SSH user for {host} — set remote_scan.user or remote_scan.hosts['{host}']"}
    opts = str(cfg.get("ssh_opts", "-o ConnectTimeout=8 -o BatchMode=yes")).split()
    py = cfg.get("python", "python3")
    script = os.path.join(ROOT, "scanners", "scan_services.py")
    cmd = ["ssh", *opts, f"{user}@{host}", f"{shlex.quote(str(py))} -"]
    try:
        with open(script, encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdin=fh, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": f"ssh failed: {e}"}
    if r.returncode != 0:
        return {"error": (r.stderr or "ssh scan failed").strip()[-300:]}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": "probe returned no JSON (is python3 on the host?)"}


def scan_host(host: str, name: str) -> dict:
    """SSH into a Linux host, run the hardware scanner over the pipe, ingest the
    result as a machine card. Raises with the agent fallback if SSH isn't set up."""
    # host must be a literal IP — blocks ssh option injection (e.g. a leading
    # '-' becoming -oProxyCommand=...). Rebind to the parsed IP's canonical text
    # so only a validated address (never the raw input) reaches the command.
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        raise RuntimeError("invalid host — must be an IP address")
    cfg = _remote_scan_cfg()
    # short reason only; the dashboard builds the bootstrap commands itself
    agent_hint = "remote SSH scan is off (set remote_scan in config.yaml to enable it)"
    if not cfg.get("enabled"):
        raise RuntimeError(agent_hint)
    # per-host user override, else the default user
    user = (cfg.get("hosts") or {}).get(host) or cfg.get("user")
    if not user:
        raise RuntimeError(f"no SSH user for {host} — set remote_scan.user "
                           f"or remote_scan.hosts['{host}'] in config.yaml")
    opts = str(cfg.get("ssh_opts", "-o ConnectTimeout=8 -o BatchMode=yes")).split()
    py = cfg.get("python", "python3")
    script = os.path.join(ROOT, "scanners", "make_linux_topology.py")
    # Only the validated IP and trusted-config values (user/py/opts) reach the
    # command. The user-supplied name is NOT passed to the remote shell — we
    # stamp it onto the result server-side after the scan returns.
    remote = f"{shlex.quote(str(py))} - --stdout"
    cmd = ["ssh", *opts, f"{user}@{host}", remote]
    try:
        with open(script, encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdin=fh, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"ssh failed: {e}\n\n{agent_hint}")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ssh scan failed").strip()[-400:] + f"\n\n{agent_hint}")
    try:
        topo = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("remote scan returned no JSON (is python3 on the host?)\n\n" + agent_hint)
    topo["name"] = name              # our label, applied here — never in the ssh command
    return store.save(topo, host)    # source=host -> card shows its IP


def generate(name: str) -> dict:
    """Read this host's hardware fabric and save it as a new topology."""
    tid = store.slug(name)
    os.makedirs(store.STORE, exist_ok=True)
    out = store.path(tid)
    r = subprocess.run(
        [sys.executable, GENERATOR, "--out", out, "--name", name],
        cwd=ROOT, capture_output=True, text=True,
    )
    if not os.path.exists(out):
        raise RuntimeError((r.stderr or r.stdout or f"{GENERATOR} produced nothing").strip()[-800:])
    return {"id": tid, "name": name}


# the network → dashboard-card reshaping lives in renderers/network_cards.py
# (from_topology); the card contract itself is renderers/card.py (see CONTEXT.md)


def generate_network(subnet: str | None = None) -> dict:
    """Ping-sweep the network and save it as the 'network' topology card. Uses
    config.yaml if present (honours the user's zones/collectors), else a throwaway
    config that enables pingsweep with auto subnet detection — zero setup."""
    cfg_path = os.path.join(ROOT, "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(ROOT, "out", ".netscan.yaml")
        subs = f"[{subnet}]" if subnet else "[]"
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write("offline_after_minutes: 30\npingsweep:\n  enabled: true\n"
                     f"  subnets: {subs}\n  resolve: true\n")
    r = subprocess.run(
        [sys.executable, "scanners/make_network_topology.py", "--config", cfg_path, "--outdir", "out"],
        cwd=ROOT, capture_output=True, text=True,
    )
    src = os.path.join(ROOT, "out", "topology.json")
    if not os.path.exists(src):
        raise RuntimeError((r.stderr or r.stdout or "scan produced nothing").strip()[-800:])
    with open(src, encoding="utf-8") as fh:
        d = json.load(fh)
    from renderers.network_cards import from_topology
    cards = from_topology(d)
    topo = {"name": "Network", "kind": "network", "generated": d.get("generated"), "nodes": cards}
    out = {"id": "network", "name": "Network", "nodes": len(cards)}
    # no gateway collector produced a router? fingerprint it and suggest one
    if not any(c.get("kind") == "firewall" for c in cards):
        from core.detect import detect_gateway
        g = detect_gateway()
        if g and g["kind"] != "generic":
            topo["hint"] = out["hint"] = g["hint"]
    store.save(topo)                 # name "Network" -> id "network"
    return out


sys.path.insert(0, ROOT)    # repo root — where core/ and the scanners live
from core import local_telemetry as _tele   # noqa: E402  shared local-metrics sampler

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
        return "source" in store.load(tid)
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
            return self._send(200, list_rows())
        if self.path.split("?")[0] == "/api/telemetry":
            host = parse_qs(urlparse(self.path).query).get("host", [""])[0]
            try:
                return self._send(200, host_telemetry(host) if host else telemetry_local())
            except Exception as e:
                return self._send(200, {**_tele.ZERO, "error": str(e)})
        if self.path.split("?")[0] == "/api/gwdash":
            return self._send(200, gateway_dashboard())
        if self.path.split("?")[0] == "/api/services":
            host = parse_qs(urlparse(self.path).query).get("host", [""])[0]
            return self._send(200, host_services(host))
        if self.path.startswith("/t/"):
            tid = os.path.basename(self.path.split("?")[0])[:-5]  # strip .json
            try:
                return self._send(200, store.load(tid))
            except (ValueError, OSError):
                return self._send(404, {"error": "not found"})
        return super().do_GET()

    def do_POST(self):
        try:
            if self.path == "/api/gwspeedtest":
                return self._send(200, gateway_speedtest())
            if self.path == "/api/generate":
                name = (self._body().get("name") or "topology").strip()
                return self._send(200, generate(name))
            if self.path == "/api/generate-network":
                subnet = (self._body().get("subnet") or "").strip() or None
                return self._send(200, generate_network(subnet))
            if self.path == "/api/scan-host":
                b = self._body()
                host = (b.get("host") or "").strip()
                name = (b.get("name") or host).strip()
                if not host:
                    return self._send(400, {"error": "missing host"})
                try:
                    return self._send(200, scan_host(host, name))
                except RuntimeError as e:
                    # server_ip so the client builds a LAN-reachable bootstrap
                    # command (its own location may be localhost). ssh_tried tells
                    # the client whether this was a real SSH failure (show why) or
                    # just remote_scan being off (the normal manual-add flow).
                    return self._send(502, {"error": str(e), "server_ip": server_ip(),
                                            "ssh_tried": bool(_remote_scan_cfg().get("enabled"))})
            if self.path == "/api/delete":
                store.delete(self._body().get("id", ""))   # bad/absent id is a no-op
                return self._send(200, {"ok": True})
            if self.path == "/api/ingest":
                if INGEST_TOKEN and self.headers.get("X-Token") != INGEST_TOKEN:
                    return self._send(403, {"error": "bad or missing token"})
                topo = self._body()
                if not isinstance(topo, dict) or not topo.get("nodes"):
                    return self._send(400, {"error": "body must be a topology with a 'nodes' array"})
                return self._send(200, store.save(topo, self.client_address[0]))
            if self.path == "/api/telemetry":
                if INGEST_TOKEN and self.headers.get("X-Token") != INGEST_TOKEN:
                    return self._send(403, {"error": "bad or missing token"})
                b = self._body()
                hid = store.stable_slug(b.get("host", ""))
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
    os.makedirs(store.STORE, exist_ok=True)
    with http.server.ThreadingHTTPServer(("", args.port), Handler) as httpd:
        print(f"dashboard on http://localhost:{args.port}  (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
