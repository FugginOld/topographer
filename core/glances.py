"""Glances REST-API reader + installer, stdlib-only.

Shared by the dashboard server (proxies its own machine's Glances) and the
reporting agent (each remote Linux host fetches its *own* localhost Glances and
pushes the compact result, like telemetry/services). `fetch()` returns the same
compact tile-grid shape the dashboard consumes; `ensure()` makes Glances exist
and its web/REST server run on this Linux host, installing it if missing.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
import urllib.request

DEFAULT_URL = "http://localhost:61208"


# ── REST fetch + parse into the compact shape the dashboard's tile grid wants ──
def _get(base: str, ver, path: str):
    with urllib.request.urlopen(f"{base}/api/{ver}/{path}", timeout=3) as r:
        return json.loads(r.read())


def _gb(b):
    return round(b / 1073741824, 1) if isinstance(b, (int, float)) else None


def _rate(d: dict, rate_key: str, delta_key: str) -> float:
    """bytes/sec: prefer Glances' *_rate_per_sec, else delta-over-interval."""
    r = d.get(rate_key)
    if isinstance(r, (int, float)):
        return float(r)
    delta, dt = d.get(delta_key), d.get("time_since_update")
    if isinstance(delta, (int, float)) and isinstance(dt, (int, float)) and dt > 0:
        return delta / dt
    return 0.0


def _mem(d):
    return {"percent": round(d.get("percent", 0)), "total_gb": _gb(d.get("total")),
            "used_gb": _gb(d.get("used")), "free_gb": _gb(d.get("free"))}


def _system(d):
    return {"hostname": d.get("hostname", ""), "distro": d.get("linux_distro") or d.get("os_name", ""),
            "kernel": d.get("os_version", "")}


def _net(nets):
    """busiest up interface -> {iface, tx_bps, rx_bps}, skipping loopback/virtual."""
    best, score = None, -1.0
    for n in nets or []:
        name = n.get("interface_name", "")
        if not n.get("is_up", True) or name == "lo" or name.startswith(("veth", "br-", "docker")):
            continue
        tx, rx = _rate(n, "bytes_sent_rate_per_sec", "tx"), _rate(n, "bytes_recv_rate_per_sec", "rx")
        if tx + rx >= score:
            best, score = {"iface": name, "tx_bps": tx, "rx_bps": rx}, tx + rx
    return best


def _disk(disks):
    rd = wr = 0.0
    for d in disks or []:
        rd += _rate(d, "read_bytes_rate_per_sec", "read_bytes")
        wr += _rate(d, "write_bytes_rate_per_sec", "write_bytes")
    return {"read_bps": rd, "write_bps": wr}


def _sensor(sensors):
    """CPU temp with warn/crit thresholds; prefer a package/cpu/core label, else hottest."""
    cands = [s for s in sensors or []
             if str(s.get("unit", "")).upper() == "C" and isinstance(s.get("value"), (int, float))]
    if not cands:
        return None
    pref = [s for s in cands if re.search(r"package|cpu|tctl|core|coretemp", str(s.get("label", "")), re.I)]
    s = max(pref or cands, key=lambda x: x["value"])
    return {"value": round(s["value"]), "warn": s.get("warning"), "crit": s.get("critical")}


def _procs(plist):
    """top 5 processes by CPU -> [{name, cpu, mem_mb}]."""
    def cpu(p): return p.get("cpu_percent") or 0
    out = []
    for p in sorted(plist or [], key=cpu, reverse=True)[:5]:
        nm = p.get("name") or ((p.get("cmdline") or [""])[0])
        mi = p.get("memory_info")
        rss = mi.get("rss") if isinstance(mi, dict) else (mi[0] if isinstance(mi, (list, tuple)) and mi else None)
        out.append({"name": str(nm)[:24], "cpu": round(cpu(p), 1),
                    "mem_mb": round(rss / 1048576) if isinstance(rss, (int, float)) else None})
    return out


def _fetch(base: str, ver) -> dict:
    try:
        ql = _get(base, ver, "quicklook")                # cpu/mem/swap % in one call
    except Exception:
        return {}                                        # unreachable or wrong API version
    out = {"cpu": round(ql.get("cpu", 0)), "cpu_name": ql.get("cpu_name", "")}
    for key, path, fn in (("mem", "mem", _mem), ("system", "system", _system),
                          ("net", "network", _net), ("diskio", "diskio", _disk),
                          ("temp", "sensors", _sensor), ("procs", "processlist", _procs)):
        try:
            v = fn(_get(base, ver, path))
            if v is not None:
                out[key] = v
        except Exception:
            pass                                         # optional section; skip on miss
    return out


def fetch(base: str = DEFAULT_URL, ver=4) -> dict:
    """Compact metrics dict, or {} if Glances isn't reachable. Falls back to the
    old /api/3 shape when the configured version misses."""
    base = (base or DEFAULT_URL).rstrip("/")
    return _fetch(base, ver) or (_fetch(base, 3) if ver != 3 else {})


# ── install + launch (Linux agent side) ──────────────────────────────────────
def _reachable(base: str, ver=4) -> bool:
    base = (base or DEFAULT_URL).rstrip("/")
    for v in (ver, 3):
        try:
            _get(base, v, "quicklook")
            return True
        except Exception:
            pass
    return False


def _installed() -> bool:
    return importlib.util.find_spec("glances") is not None


def _has_pip() -> bool:
    return subprocess.run([sys.executable, "-m", "pip", "--version"],
                          capture_output=True).returncode == 0


def _pip_install(log, *pkgs) -> bool:
    """pip --user, retrying with --break-system-packages for PEP 668 distros."""
    if not _has_pip():                                    # Debian ships pip separately (python3-pip)
        try:                                             # unprivileged self-heal; often absent too
            subprocess.run([sys.executable, "-m", "ensurepip", "--user"], capture_output=True, timeout=120)
        except Exception:
            pass
        if not _has_pip():
            log("glances: pip unavailable — install it on this host (apt install python3-pip); skipping")
            return False
    err = ""
    for extra in ([], ["--break-system-packages"]):
        cmd = [sys.executable, "-m", "pip", "install", "--user", *extra, *pkgs]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                return True
            err = r.stderr or r.stdout or ""
        except Exception as e:
            err = str(e)
    log(f"glances: pip install {' '.join(pkgs)} failed — {err.strip()[-200:]}")
    return False


def _launch(base: str, ver, log) -> bool:
    try:                                                 # detached web server; survives us
        proc = subprocess.Popen([sys.executable, "-m", "glances", "-w"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        log(f"glances: launch failed — {e}")
        return False
    for _ in range(20):                                  # give the web server up to ~10s to bind
        if _reachable(base, ver):
            return True
        if proc.poll() is not None:                      # crashed on startup — don't wait it out
            return False
        time.sleep(0.5)
    return False


def ensure(base: str = DEFAULT_URL, ver=4, install: bool = True, log=lambda m: None) -> bool:
    """Linux only: make sure a Glances web/REST server answers at `base`, launching
    the local Glances and, if that won't serve, installing a complete upstream
    glances[web] (unless install=False). Best-effort — returns True iff reachable
    afterward, so callers can just skip pushing on False. `log` (e.g. print) gets
    one-line status notes.

    Detects/launches via the CURRENT interpreter (find_spec + `python -m glances`),
    never the `glances` console script — a `pip --user` install lands in
    ~/.local/bin, which isn't on PATH under systemd/cron, so `which glances` lies.

    ponytail: distro glances (e.g. Debian's python3-glances) is often web-crippled —
    stripped web-UI static assets + split-out jinja2/fastapi/uvicorn — so `-w` dies
    even though `import glances` works. Rather than chase each missing piece, we pip
    --user --ignore-installed the full upstream glances[web], which shadows the
    distro build in user-site. Install glances[web] by hand once and this is launch-only.
    """
    if not sys.platform.startswith("linux"):
        return _reachable(base, ver)                     # server-managed elsewhere
    if _reachable(base, ver):
        return True
    if _installed() and _launch(base, ver, log):         # a working glances is already here
        log("glances: web server up")
        return True
    if not install:
        log("glances: local web server won't start and --no-glances-install set; skipping")
        return False
    log("glances: installing upstream glances[web] (pip --user)…")
    if not _pip_install(log, "--ignore-installed", "glances[web]"):
        return False                                     # _pip_install logged why
    if _launch(base, ver, log):
        log("glances: web server up")
        return True
    log("glances: web server not answering — run `python3 -m glances -w` by hand to see why")
    return False


if __name__ == "__main__":   # smoke test: parse a canned quicklook shape offline
    assert _mem({"percent": 50.4, "total": 8 * 1073741824, "used": 4 * 1073741824})["percent"] == 50
    assert _net([{"interface_name": "lo", "tx": 9, "rx": 9},
                 {"interface_name": "eth0", "is_up": True, "bytes_sent_rate_per_sec": 100,
                  "bytes_recv_rate_per_sec": 200}])["iface"] == "eth0"
    assert _procs([{"name": "x", "cpu_percent": 3.0, "memory_info": {"rss": 2097152}}])[0]["mem_mb"] == 2
    assert _sensor([{"label": "Core 0", "unit": "C", "value": 61.6}])["value"] == 62
    print("core/glances.py selftest ok")
