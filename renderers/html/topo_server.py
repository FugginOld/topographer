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
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse, parse_qs

import agent_bundle   # agent tar.gz/zip builder (same dir) — see agent_bundle.py
import icons          # service-icon CDN cache (same dir) — see icons.py
import store          # topology persistence (same dir) — see store.py / CONTEXT.md
import widget_store    # per-host widget instances (same dir) — see widget_store.py
from pushcache import PushCache   # per-host push-freshness cache (same dir)

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


def _up_status(secs) -> str:
    """PVE uptime seconds -> docker-style 'Up N unit' so the tile's uptime parser reads it."""
    secs = int(secs or 0)
    for n, word in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if secs >= n:
            v = secs // n
            return f"Up {v} {word}{'s' if v != 1 else ''}"
    return "Up"


def _gib(b) -> str:
    """Bytes -> '301.24 MiB' / '16.01 GiB', matching the PVE summary panel."""
    b = float(b or 0)
    for unit, div in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if b >= div:
            return f"{b / div:.2f} {unit}"
    return f"{int(b)} B"


def _guest_ips(col, node: str, kind: str, vmid) -> list[str]:
    """All non-loopback IPv4s of a running guest (lxc /interfaces, qemu agent). []
    when the container's off or the VM has no guest agent."""
    ips = []
    try:
        if kind == "lxc":
            for i in col._get(f"/nodes/{node}/lxc/{vmid}/interfaces") or []:
                ip = (i.get("inet") or "").split("/")[0]
                if ip and not ip.startswith("127."):
                    ips.append(ip)
        else:
            data = col._get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}
            for iface in data.get("result", []):
                for a in iface.get("ip-addresses", []):
                    ip = a.get("ip-address", "")
                    if a.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
                        ips.append(ip)
    except Exception:
        return []
    seen = set()
    return [x for x in ips if not (x in seen or seen.add(x))]   # dedupe, keep order


def _guest_detail(col, g: dict) -> list[list]:
    """PVE summary rows for a guest tile, ordered like the PVE panel. Cheap fields
    come from /cluster/resources; SWAP / Unprivileged / IPs need a per-guest call
    (running guests only), each best-effort."""
    kind, node, vmid = g["type"], g.get("node"), g.get("vmid")
    running = g.get("status") == "running"
    cpu, maxcpu = (g.get("cpu") or 0) * 100, int(g.get("maxcpu") or 0)
    mem, maxmem = g.get("mem") or 0, g.get("maxmem") or 0
    disk, maxdisk = g.get("disk") or 0, g.get("maxdisk") or 0
    unpriv, swap_row, ips = None, None, []
    if running:
        if kind == "lxc":
            cur = col._get(f"/nodes/{node}/lxc/{vmid}/status/current") or {}
            mxs = cur.get("maxswap") or 0
            if mxs:
                swap_row = ["SWAP usage", f"{(cur.get('swap') or 0) / mxs * 100:.2f}% "
                                          f"({_gib(cur.get('swap'))} of {_gib(mxs)})"]
            cf = col._get(f"/nodes/{node}/lxc/{vmid}/config") or {}
            if "unprivileged" in cf:
                unpriv = "Yes" if cf.get("unprivileged") else "No"
        ips = _guest_ips(col, node, kind, vmid)
    rows = [["Status", g.get("status") or "unknown"],
            ["HA State", g.get("hastate") or "none"],
            ["Node", node or "—"]]
    if unpriv is not None:
        rows.append(["Unprivileged", unpriv])
    if maxcpu:
        rows.append(["CPU usage", f"{cpu:.2f}% of {maxcpu} CPU{'s' if maxcpu != 1 else ''}"])
    if maxmem:
        rows.append(["Memory usage", f"{mem / maxmem * 100:.2f}% ({_gib(mem)} of {_gib(maxmem)})"])
    if swap_row:
        rows.append(swap_row)
    if disk and maxdisk:                                    # qemu often reports disk=0 (can't see inside) -> skip
        rows.append(["Bootdisk size", f"{disk / maxdisk * 100:.2f}% ({_gib(disk)} of {_gib(maxdisk)})"])
    if ips:
        rows.append(["IPs", ", ".join(ips[:6])])
    return rows


def _proxmox_guests(host_id: str) -> list[dict]:
    """VMs / LXC shaped as container tiles for a PVE host's dashboard (project =
    node, so the dashboard groups them under the node). A host is a PVE host if its
    name matches a node name, or its reporting IP is the configured PVE endpoint
    (then the whole cluster is shown). [] otherwise / when Proxmox is off."""
    cfg = _cfg_block("proxmox")
    if not cfg.get("enabled") or not cfg.get("url"):
        return []
    try:
        d = store.load(host_id) or {}
    except (OSError, ValueError):
        d = {}
    name_key = (d.get("name") or "").strip().lower().split(".")[0]     # hostname portion of the machine name
    host_ip = (d.get("source") or "").strip()                          # reporting IP (empty for a local card)
    url_ip = (urlparse(cfg["url"]).hostname or "").strip()
    is_endpoint = bool(host_ip) and host_ip == url_ip                  # this host IS the PVE API box
    if not name_key and not is_endpoint:
        return []
    try:
        from collectors.proxmox import ProxmoxCollector
        col = ProxmoxCollector(cfg)
        res = col._get("/cluster/resources") or []                     # cluster-wide, single call
    except Exception:
        return []
    matched = next((r.get("node") for r in res
                    if r.get("type") == "node" and (r.get("node") or "").lower() == name_key), None)
    if matched:
        want = lambda g: g.get("node") == matched                      # name match -> just that node
    elif is_endpoint:
        want = lambda g: True                                          # endpoint IP -> the whole cluster
    else:
        return []
    out = []
    for g in res:
        if g.get("type") not in ("qemu", "lxc") or not want(g):
            continue
        running, vmid, maxmem = g.get("status") == "running", g.get("vmid"), g.get("maxmem") or 0
        out.append({
            "name": g.get("name") or f"{g['type']}{vmid}",
            "image": ("VM" if g["type"] == "qemu" else "LXC") + (f" · {vmid}" if vmid else ""),
            "state": "running" if running else "stopped",
            "status": _up_status(g.get("uptime")) if running else (g.get("status") or "stopped"),
            "project": g.get("node"),                                  # groups under the node in the dashboard
            "cpu": f"{(g.get('cpu') or 0) * 100:.1f}%" if running else "",
            "memp": f"{(g.get('mem') or 0) / maxmem * 100:.1f}%" if running and maxmem else "",
            "detail": _guest_detail(col, g),                           # PVE summary rows shown under the graph
            "engine": "proxmox",
        })
    out.sort(key=lambda c: (c["state"] != "running", c["project"] or "", c["name"].lower()))
    return out


def host_services(host_id: str) -> dict:
    """Containers / compose stacks / services for a machine's dashboard. The
    server's own machine is probed directly; a remote host serves whatever its
    agent last reported (push model, like telemetry). A PVE host also gets its
    VMs / LXC folded in as tiles, grouped by node."""
    guests = _proxmox_guests(host_id)
    base = _local_services() if (host_id and not _is_remote(host_id)) else _pushed_svc.get(host_id)
    if guests:
        merged = dict(base) if isinstance(base, dict) and not base.get("error") else {}
        merged["containers"] = (merged.get("containers") or []) + guests
        merged.setdefault("engine", "proxmox")
        merged["live"] = True
        return merged
    return base or {                               # live/stale last-known, else the how-to
        "error": "no services reported for this host yet — its agent reports "
                 "containers/services automatically once updated (git pull or "
                 "re-run bootstrap on that machine)."}


# ── Glances widget: a compact system-metrics tile grid. The dashboard's own
# machine is proxied from its local Glances REST API (config.yaml glances block,
# since that API sends no CORS headers the browser can't read it directly). A
# remote host serves whatever its reporting agent last pushed — push model, like
# telemetry/services, so remote Linux hosts light up with no per-host config.
#   glances: {enabled: true, url: "http://localhost:61208", version: 4}
_glances_cache = {"t": 0.0, "data": {}}   # this server's OWN glances proxy (brief cache), not a push
_pushed_gl = PushCache(15.0, store.stable_slug)   # remote hosts' pushed glances metrics


def glances_stats(host_id: str) -> dict:
    if host_id and _is_remote(host_id):                  # remote host: serve its agent's push
        return _pushed_gl.get(host_id) or {}
    cfg = _cfg_block("glances")                          # this machine: proxy its local Glances
    if not cfg.get("enabled"):
        return {}
    if time.monotonic() - _glances_cache["t"] < 2.0:
        return _glances_cache["data"]
    data = glances.fetch(cfg.get("url") or glances.DEFAULT_URL, cfg.get("version", 4))
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
from core import glances                     # noqa: E402  Glances reader (also used by the agent)
from widgets import registry as wreg         # noqa: E402  widget Type catalog + fetchers

_tele_cache = {"t": 0.0, "data": dict(_tele.ZERO)}   # this server's OWN metrics (brief cache), not a push
_pushed_tele = PushCache(15.0, store.stable_slug)    # remote hosts' pushed telemetry
_pushed_svc = PushCache(900.0, store.stable_slug)    # remote hosts' pushed services (push slowly, ~5 min)


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
    pushed = _pushed_tele.get(tid)
    if pushed and pushed.get("live"):
        return pushed
    if not _is_remote(tid):
        return {**telemetry_local(), "live": True}
    return {**_tele.ZERO, "stale": True}   # remote + no fresh push -> zeros (not stale data, by design)


def export_bundle(tid: str) -> dict:
    """Everything the server knows about one host, for a diagnostic download: the
    stored topology plus its live/last-pushed telemetry, services and glances. Each
    source is best-effort — a failing one becomes an {"error": ...} stub so the
    bundle is always complete enough to attach to a bug report."""
    def _try(fn):
        try:
            return fn()
        except Exception as e:
            return {"error": str(e)}
    return {
        "id": tid,
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "topology": _try(lambda: store.load(tid)),
        "telemetry": _try(lambda: host_telemetry(tid)),
        "services": _try(lambda: host_services(tid)),
        "glances": _try(lambda: glances_stats(tid)),
    }


def export_all() -> dict:
    """Whole-dashboard diagnostic: one bundle per stored host."""
    return {"exported": time.strftime("%Y-%m-%dT%H:%M:%S"), "server": server_ip(),
            "hosts": [export_bundle(tid) for tid in store.ids()]}


# ── Widget Store: per-host service widgets. Config (incl. secrets) lives in
# widget_store (gitignored out/); the server does every outbound API call and
# masks secret fields on the way out, so keys never reach the browser. ──────────
_widget_cache: dict[str, dict] = {}   # "host|id" -> {"t": monotonic, "data": stats}
WIDGET_FRESH = 20.0                   # seconds a fetched sample is reused


def _widget_public(w: dict) -> dict:
    """A stored widget with secret config fields blanked for the browser (a
    parallel 'secret_set' flags which ones actually have a value stored)."""
    secrets = set(wreg.secret_fields(w.get("type", "")))
    cfg = w.get("config", {})
    return {"id": w["id"], "type": w["type"], "position": w.get("position", 0),
            "interval": w.get("interval"),
            "config": {k: ("" if k in secrets else v) for k, v in cfg.items()},
            "secret_set": {k: bool(cfg.get(k)) for k in secrets}}


def _effective_config(w: dict) -> dict:
    """Config the fetcher actually runs with: the widget's form values layered
    over the matching config.yaml collector block (Type.config_key), so a
    proxmox/unifi widget inherits already-configured credentials when left blank."""
    t = wreg.get(w.get("type", ""))
    base = _cfg_block(t["config_key"]) if t and t.get("config_key") else {}
    form = {k: v for k, v in (w.get("config") or {}).items() if v not in (None, "")}
    return {**base, **form}                              # form overrides config.yaml; blanks inherit


def widgets_list(host: str) -> list[dict]:
    """This host's widgets: masked config + short-cached live data."""
    if not host:
        return []
    out = []
    for w in widget_store.list_for(host):
        key = f"{host}|{w['id']}"
        ttl = w.get("interval") or WIDGET_FRESH             # per-widget refresh, else global
        c = _widget_cache.get(key)
        if not (c and time.monotonic() - c["t"] < ttl):
            c = {"t": time.monotonic(), "data": wreg.fetch(w.get("type", ""), _effective_config(w))}
            _widget_cache[key] = c
        out.append({**_widget_public(w), "data": c["data"]})
    return out


def _clean_config(wtype: str, config: dict, prev: dict | None = None) -> dict:
    """Whitelist to the Type's known fields; a blank/absent secret preserves the
    prior stored value (so the browser never has to echo a key back to keep it)."""
    secrets, prev = set(wreg.secret_fields(wtype)), (prev or {})
    out = {}
    for k in wreg.field_names(wtype):
        v = (config or {}).get(k)
        if k in secrets and not v:
            if prev.get(k):
                out[k] = prev[k]
        elif v is not None:
            out[k] = v
    return out


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

    def _send_download(self, filename: str, obj) -> None:
        # filename is sanitized to a safe charset -> no Content-Disposition header injection
        fn = re.sub(r"[^A-Za-z0-9._-]", "-", filename) or "export.json"
        body = json.dumps(obj, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", f'attachment; filename="{fn}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # agent client + its installer, served from this server (not github) so a
        # fresh machine bootstraps entirely from the dashboard it reports to.
        if self.path == "/agent.tar.gz":
            return self._send_bytes("application/gzip", agent_bundle.bundle("tar"))
        if self.path == "/agent.zip":
            return self._send_bytes("application/zip", agent_bundle.bundle("zip"))
        if self.path in ("/bootstrap.sh", "/bootstrap.ps1"):
            # constant target from a literal map (not derived from the request path)
            fname = {"/bootstrap.sh": "bootstrap.sh", "/bootstrap.ps1": "bootstrap.ps1"}[self.path]
            try:
                with open(os.path.join(ROOT, fname), "rb") as fh:
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
            ic = icons.icon_bytes(q.get("name", [""])[0], q.get("image", [""])[0])
            return self._send_bytes(*ic) if ic else self._send(404, {"error": "no icon"})
        if self.path.split("?")[0] == "/api/glances":
            host = parse_qs(urlparse(self.path).query).get("host", [""])[0]
            try:
                return self._send(200, glances_stats(host))
            except Exception as e:
                return self._send(200, {"error": str(e)})
        if self.path.split("?")[0] == "/api/widget-catalog":
            return self._send(200, wreg.full_catalog())
        if self.path.split("?")[0] == "/api/widgets":
            host = store.stable_slug(parse_qs(urlparse(self.path).query).get("host", [""])[0])
            return self._send(200, widgets_list(host))
        if self.path.split("?")[0] == "/api/export":
            tid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if tid:   # single host: store.load inside export_bundle is path-guarded
                return self._send_download(f"topo-{tid}-{time.strftime('%Y%m%d')}.json",
                                           export_bundle(tid))
            return self._send_download(f"dashboard-{time.strftime('%Y%m%d')}.json", export_all())
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
                _pushed_tele.put(hid, data)
                return self._send(200, {"ok": True, "host": hid})
            if self.path == "/api/ingest-services":
                if INGEST_TOKEN and self.headers.get("X-Token") != INGEST_TOKEN:
                    return self._send(403, {"error": "bad or missing token"})
                b = self._body()
                hid = store.stable_slug(b.get("host", ""))
                if not hid:
                    return self._send(400, {"error": "missing host"})
                _pushed_svc.put(hid, {k: b.get(k) for k in ("engine", "containers", "services")})
                return self._send(200, {"ok": True, "host": hid})
            if self.path == "/api/ingest-glances":
                if INGEST_TOKEN and self.headers.get("X-Token") != INGEST_TOKEN:
                    return self._send(403, {"error": "bad or missing token"})
                b = self._body()
                hid = store.stable_slug(b.get("host", ""))
                if not hid:
                    return self._send(400, {"error": "missing host"})
                data = {k: v for k, v in b.items() if k != "host"}   # the compact metrics dict
                _pushed_gl.put(hid, data)
                return self._send(200, {"ok": True, "host": hid})
            if self.path == "/api/widget-add":
                b = self._body()
                host, wtype = store.stable_slug(b.get("host", "")), b.get("type", "")
                if not host or not wreg.get(wtype):
                    return self._send(400, {"error": "missing host or unknown widget type"})
                cfg = _clean_config(wtype, b.get("config", {}))
                return self._send(200, _widget_public(widget_store.add(host, wtype, cfg, b.get("interval"))))
            if self.path == "/api/widget-update":
                b = self._body()
                host, wid = store.stable_slug(b.get("host", "")), b.get("id", "")
                cur = next((w for w in widget_store.list_for(host) if w["id"] == wid), None)
                if not cur:
                    return self._send(404, {"error": "no such widget"})
                cfg = _clean_config(cur["type"], b.get("config", {}), cur.get("config"))
                _widget_cache.pop(f"{host}|{wid}", None)          # re-fetch with the new config
                return self._send(200, _widget_public(widget_store.update(host, wid, cfg, b.get("interval"))))
            if self.path == "/api/widget-test":
                b = self._body()
                host, wid = store.stable_slug(b.get("host", "")), b.get("id", "")
                w = next((x for x in widget_store.list_for(host) if x["id"] == wid), None)
                if not w:
                    return self._send(404, {"error": "no such widget"})
                data = wreg.fetch(w.get("type", ""), _effective_config(w))   # fresh, bypass cache
                _widget_cache[f"{host}|{wid}"] = {"t": time.monotonic(), "data": data}
                return self._send(200, {"ok": bool(data), "empty": not data, "data": data})
            if self.path == "/api/widget-delete":
                b = self._body()
                host, wid = store.stable_slug(b.get("host", "")), b.get("id", "")
                widget_store.delete(host, wid)
                _widget_cache.pop(f"{host}|{wid}", None)
                return self._send(200, {"ok": True})
            if self.path == "/api/widget-reorder":
                b = self._body()
                widget_store.reorder(store.stable_slug(b.get("host", "")), b.get("order") or [])
                return self._send(200, {"ok": True})
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
