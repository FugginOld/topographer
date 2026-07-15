#!/usr/bin/env python3
"""Serve the topology dashboard + a store of saved topologies on :8770.

Browsers block fetch() over file://, so the dashboard needs a real HTTP origin.
This serves renderers/html/ plus a tiny API over out/topologies/*.json:

    GET  /api/list            -> [{id,name,generated,nodes,vlans}, ...]
    GET  /t/<id>.json         -> a saved topology
    POST /api/generate {name} -> run the hardware scanner, save as a new topology
    POST /api/delete   {id}   -> remove a saved topology

    python renderers/html/topo_server.py [--port 8770]

GENERATE runs scanners/make_pc_topo.py to map this PC's hardware fabric.
"""
from __future__ import annotations

import argparse
import http.server
import io
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from urllib.parse import urlparse, parse_qs

import store   # topology persistence (same dir) — see store.py / CONTEXT.md

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


INGEST_TOKEN = os.environ.get("TOPO_TOKEN", "")   # optional shared secret for /api/ingest

# The reporting agent's file set — served to fresh machines by bootstrap so they
# fetch the client from THIS server, not github. Union of both OSes' needs: the
# Windows scanner (make_pc_topo.py) pulls renderers/card.py; the Linux one is
# self-contained. Extra files on the other OS are a few harmless KB.
AGENT_PATHS = ["agent", "scanners/make_linux_topo.py", "scanners/make_pc_topo.py",
               "scanners/scan_services.py", "core/__init__.py", "core/local_telemetry.py",
               "renderers/__init__.py", "renderers/card.py"]


def _agent_files():
    """(arcname, abspath) for every real file under AGENT_PATHS, __pycache__ skipped."""
    for rel in AGENT_PATHS:
        p = os.path.join(ROOT, rel)
        if os.path.isdir(p):
            for dirpath, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    ap = os.path.join(dirpath, f)
                    yield os.path.relpath(ap, ROOT).replace("\\", "/"), ap
        elif os.path.isfile(p):
            yield rel, p


def agent_bundle(fmt: str) -> bytes:
    """The agent file set as a tar.gz ('tar') or zip. No top-level dir prefix, so
    the client extracts straight into its install dir (no --strip-components)."""
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, ap in _agent_files():
                z.write(ap, arc)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for arc, ap in _agent_files():
                tar.add(ap, arcname=arc)
    return buf.getvalue()

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
GENERATOR = "scanners/make_pc_topo.py" if sys.platform.startswith("win") else "scanners/make_linux_topo.py"


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


def _local_services() -> dict:
    """Probe THIS (server) machine's containers/services directly — no push needed."""
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scanners", "scan_services.py")],
                           capture_output=True, text=True, timeout=30)
        return {**json.loads(r.stdout or "{}"), "live": True}
    except Exception as e:
        return {"error": f"local service probe failed: {e}"}


def host_services(host_id: str) -> dict:
    """Containers / compose stacks / services for a machine's dashboard. The
    server's own machine is probed directly; a remote host serves whatever its
    agent last reported (push model, like telemetry)."""
    if host_id and not _is_remote(host_id):
        return _local_services()
    h = _host_svc.get(store.stable_slug(host_id))
    if h and time.monotonic() - h["t"] < SVC_FRESH:
        return {**h["data"], "live": True}
    if h:
        return {**h["data"], "stale": True}       # show last-known even if old
    return {"error": "no services reported for this host yet — its agent reports "
                     "containers/services automatically once updated (git pull or "
                     "re-run bootstrap on that machine)."}


# ── service icons: match a container to a dashboard-icons logo, fetched once from
# the CDN and cached to disk so the dashboard serves it locally (offline-warm).
ICON_DIR = os.path.join(ROOT, "out", "icons")        # gitignored cache
ICON_CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons"
_ICON_FMTS = (("svg", "image/svg+xml"), ("png", "image/png"), ("webp", "image/webp"))
_VENDORS = {"binhex", "linuxserver", "lscr", "ghcr", "hotio", "ls"}   # prefixes to drop


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _icon_slugs(name: str, image: str) -> list:
    """dashboard-icons slug candidates from a container name / image, best first."""
    out = []
    img_base = _slug((image or "").split("/")[-1].split(":")[0])   # jellyfin <- lscr.io/.../jellyfin:latest
    for s in (_slug(name), img_base):
        if not s:
            continue
        parts = s.split("-")
        out.append(s)
        if len(parts) > 1 and parts[0] in _VENDORS:
            out.append("-".join(parts[1:]))          # binhex-plex -> plex
        out.append(parts[-1])                        # last token as a final guess
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s); uniq.append(s)
    return uniq


def icon_bytes(name: str, image: str):
    """(content_type, bytes) for a service icon, cached on disk; None if unavailable.
    A '.miss' marker negative-caches unknowns so we don't re-hit the CDN each render."""
    import urllib.request
    os.makedirs(ICON_DIR, exist_ok=True)
    key = _slug(name) or _slug(image) or "x"
    for ext, ctype in _ICON_FMTS + (("miss", ""),):
        p = os.path.join(ICON_DIR, f"{key}.{ext}")
        if os.path.exists(p):
            if ext == "miss":
                return None
            with open(p, "rb") as fh:
                return ctype, fh.read()
    for slug in _icon_slugs(name, image):
        for ext, ctype in _ICON_FMTS:
            try:
                with urllib.request.urlopen(f"{ICON_CDN}/{ext}/{slug}.{ext}", timeout=4) as r:
                    if r.status == 200:
                        data = r.read()
                        with open(os.path.join(ICON_DIR, f"{key}.{ext}"), "wb") as fh:
                            fh.write(data)
                        return ctype, data
            except Exception:
                continue                             # 404 / offline -> try next candidate
    try:
        open(os.path.join(ICON_DIR, f"{key}.miss"), "wb").close()   # ponytail: permanent until out/icons cleared
    except OSError:
        pass
    return None


# ── Glances widget: proxy a Glances REST API (its API sends no CORS headers, so
# the browser can't read it directly) into a compact system-metrics card. Only
# the dashboard host's own Glances for now; enable in config.yaml:
#   glances: {enabled: true, url: "http://localhost:61208", version: 4}
_glances_cache = {"t": 0.0, "data": {}}


def _gl_get(base: str, ver, path: str):
    import urllib.request
    with urllib.request.urlopen(f"{base}/api/{ver}/{path}", timeout=3) as r:
        return json.loads(r.read())


def _glances_fetch(base: str, ver) -> dict:
    try:
        ql = _gl_get(base, ver, "quicklook")             # cpu/mem/swap % in one call
    except Exception:
        return {}                                        # unreachable or wrong API version
    out = {"cpu": round(ql.get("cpu", 0)), "mem": round(ql.get("mem", 0)),
           "swap": round(ql.get("swap", 0)), "cpu_name": ql.get("cpu_name", "")}
    try:    out["load"] = _gl_get(base, ver, "load").get("min1")
    except Exception: pass
    try:    out["uptime"] = _gl_get(base, ver, "uptime")            # v4 returns "N days, h:m:s"
    except Exception: pass
    try:                                                           # hottest °C sensor
        temps = [s.get("value") for s in _gl_get(base, ver, "sensors")
                 if str(s.get("unit", "")).upper() == "C" and isinstance(s.get("value"), (int, float))]
        if temps: out["temp"] = round(max(temps))
    except Exception: pass
    return out


def glances_stats(host_id: str) -> dict:
    cfg = _cfg_block("glances")
    if not cfg.get("enabled") or (host_id and _is_remote(host_id)):
        return {}                                        # off, or a remote host (not wired yet)
    if time.monotonic() - _glances_cache["t"] < 2.0:
        return _glances_cache["data"]
    base = (cfg.get("url") or "http://localhost:61208").rstrip("/")
    ver = cfg.get("version", 4)
    data = _glances_fetch(base, ver) or (_glances_fetch(base, 3) if ver != 3 else {})   # old Glances speaks /api/3
    _glances_cache.update(t=time.monotonic(), data=data)
    return data


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
    script = os.path.join(ROOT, "scanners", "make_linux_topo.py")
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
        [sys.executable, "scanners/make_network_topo.py", "--config", cfg_path, "--outdir", "out"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:   # else a stale out/topo.json would mask the failure
        raise RuntimeError((r.stderr or r.stdout or "scan failed").strip()[-800:])
    src = os.path.join(ROOT, "out", "topo.json")
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
_host_svc: dict[str, dict] = {}    # host id -> {"t": monotonic, "data": containers/services}
SVC_FRESH = 900.0                  # services push slowly (~5 min); 15 min stays "live"


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

    def _send_bytes(self, ctype: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # agent client + its installer, served from this server (not github) so a
        # fresh machine bootstraps entirely from the dashboard it reports to.
        if self.path == "/agent.tar.gz":
            return self._send_bytes("application/gzip", agent_bundle("tar"))
        if self.path == "/agent.zip":
            return self._send_bytes("application/zip", agent_bundle("zip"))
        if self.path in ("/bootstrap.sh", "/bootstrap.ps1"):
            try:
                with open(os.path.join(ROOT, self.path.lstrip("/")), "rb") as fh:
                    return self._send_bytes("text/plain; charset=utf-8", fh.read())
            except OSError:
                return self._send(404, {"error": "not found"})
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
        if self.path.split("?")[0] == "/icon":
            q = parse_qs(urlparse(self.path).query)
            ic = icon_bytes(q.get("name", [""])[0], q.get("image", [""])[0])
            return self._send_bytes(*ic) if ic else self._send(404, {"error": "no icon"})
        if self.path.split("?")[0] == "/api/glances":
            host = parse_qs(urlparse(self.path).query).get("host", [""])[0]
            try:
                return self._send(200, glances_stats(host))
            except Exception as e:
                return self._send(200, {"error": str(e)})
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
            if self.path == "/api/ingest-services":
                if INGEST_TOKEN and self.headers.get("X-Token") != INGEST_TOKEN:
                    return self._send(403, {"error": "bad or missing token"})
                b = self._body()
                hid = store.stable_slug(b.get("host", ""))
                if not hid:
                    return self._send(400, {"error": "missing host"})
                _host_svc[hid] = {"t": time.monotonic(),
                                  "data": {k: b.get(k) for k in ("engine", "containers", "services")}}
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
