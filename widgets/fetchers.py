"""Widget fetchers: a service's API -> a compact stats dict for its panel.

Collector contract: stdlib-only, NEVER raise, return {} on any source failure.
Reuse a `collectors/` client for transport/auth where one exists; otherwise a few
lines of urllib. Keep the returned keys small and display-agnostic — the client
decides layout.
"""
from __future__ import annotations

import json
import urllib.parse

from . import net

_get_json = net.get_json     # SSRF-guarded (private targets only) + unverified TLS; None on failure


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _pihole_base(url: str) -> str:
    base = (url or "").rstrip("/")
    return base[: -len("/admin")] if base.endswith("/admin") else base


def _v6_stats(summary: dict, blocking: dict | None) -> dict:
    """Pure map of Pi-hole v6 /stats/summary (+ /dns/blocking) -> panel stats."""
    q = (summary or {}).get("queries") or {}
    if "total" not in q:
        return {}
    g = (summary or {}).get("gravity") or {}
    out = {}
    if blocking and blocking.get("blocking"):
        out["status"] = blocking["blocking"]
    out.update({
        "queries": int(_num(q.get("total"))),
        "blocked": int(_num(q.get("blocked"))),
        "block_pct": round(_num(q.get("percent_blocked")), 1),
        "blocklist": int(_num(g.get("domains_being_blocked"))),
    })
    return out


def _v5_stats(d: dict) -> dict:
    if not isinstance(d, dict) or "dns_queries_today" not in d:
        return {}
    return {
        "status": d.get("status", "unknown"),
        "queries": int(_num(d.get("dns_queries_today"))),
        "blocked": int(_num(d.get("ads_blocked_today"))),
        "block_pct": round(_num(d.get("ads_percentage_today")), 1),
        "blocklist": int(_num(d.get("domains_being_blocked"))),
    }


def _pihole_v6(base: str, token: str) -> dict:
    """Pi-hole v6 REST API: POST /api/auth -> SID, GET /api/stats/summary, then
    release the session (v6 caps concurrent sessions, so we must log out)."""
    api = base + "/api"
    body = json.dumps({"password": token or ""}).encode()
    sess = _get_json(api + "/auth", headers={"Content-Type": "application/json"}, data=body, method="POST")
    sess = (sess or {}).get("session") or {}
    sid = sess.get("sid") if sess.get("valid") else None   # unprotected -> None, try unauthenticated
    hdr = {"X-FTL-SID": sid} if sid else {}
    try:
        summary = _get_json(api + "/stats/summary", headers=hdr)
        if summary is None:
            return {}
        return _v6_stats(summary, _get_json(api + "/dns/blocking", headers=hdr))
    finally:
        if sid:                                            # log out so we don't leak sessions each poll
            _get_json(api + "/auth", headers={"X-FTL-SID": sid}, method="DELETE", timeout=5)


def _pihole_v5(base: str, token: str) -> dict:
    q = urllib.parse.urlencode({"summaryRaw": "", "auth": token or ""})
    return _v5_stats(_get_json(f"{base}/admin/api.php?{q}"))


def pihole(cfg: dict) -> dict:
    """Pi-hole stats — v6 REST API (/api) first, falling back to the v5 api.php."""
    base = _pihole_base(cfg.get("url"))
    if not base:
        return {}
    token = cfg.get("token", "")
    return _pihole_v6(base, token) or _pihole_v5(base, token)


def _guarded(cfg: dict) -> bool:
    """A widget url is user-supplied, but the collector-backed Types below reach the
    network through `collectors/transport.py`, which is deliberately NOT SSRF-guarded
    (admin config.yaml urls). With config.yaml credentials layered under a widget's
    url, an unguarded public target would be handed the real API token. Check here
    so every widget fetch still passes the private-only guard."""
    return net.allowed(str(cfg.get("url") or ""))


def proxmox(cfg: dict) -> dict:
    """Cluster resource summary via the PVE collector's authenticated transport."""
    if not _guarded(cfg):
        return {}
    try:
        from collectors.proxmox import ProxmoxCollector
        res = ProxmoxCollector(cfg)._get("/cluster/resources") or []   # reuse token+TLS transport
    except Exception:
        return {}
    if not res:
        return {}
    run = lambda t: sum(1 for r in res if r.get("type") == t and r.get("status") == "running")
    tot = lambda t: sum(1 for r in res if r.get("type") == t)
    nodes = [r for r in res if r.get("type") == "node"]
    maxcpu = sum(_num(n.get("maxcpu")) for n in nodes) or 1
    maxmem = sum(_num(n.get("maxmem")) for n in nodes) or 1
    return {
        "nodes": sum(1 for n in nodes if n.get("status") == "online"),
        "vms": run("qemu"), "vms_total": tot("qemu"),
        "lxc": run("lxc"), "lxc_total": tot("lxc"),
        "cpu_pct": round(sum(_num(n.get("cpu")) for n in nodes) / maxcpu * 100, 1),
        "mem_pct": round(sum(_num(n.get("mem")) for n in nodes) / maxmem * 100, 1),
    }


def unifi(cfg: dict) -> dict:
    """WAN + client + gateway summary via the UniFi collector's dashboard()."""
    if not _guarded(cfg):
        return {}
    try:
        from collectors.unifi import UnifiCollector
        d = UnifiCollector(cfg).dashboard() or {}
    except Exception:
        return {}
    if not d:
        return {}
    gw, wan, tp, cl = d.get("gateway", {}), d.get("wan", {}), d.get("throughput", {}), d.get("clients", {})
    return {
        "wan_up": bool(wan.get("up")),
        "wan_status": wan.get("status") or "?",
        "clients": cl.get("total"),
        "rx_bps": tp.get("rx_bps"), "tx_bps": tp.get("tx_bps"),
        "gw_cpu": gw.get("cpu"), "gw_mem": gw.get("mem"),
    }


# ── media stack (Servarr + friends): each returns a compact stats dict ────────
# sonarr/radarr/bazarr/seerr are pure engine shape -> widgets/definitions.py DEFS.
# What stays here needs logic the engine can't express: prowlarr (sum across
# indexers), sabnzbd (string->number coercion), tautulli (nested + scale).
def prowlarr(cfg: dict) -> dict:
    base, key = (cfg.get("url") or "").rstrip("/"), cfg.get("key", "")
    if not base:
        return {}
    idx = _get_json(f"{base}/api/v1/indexer", {"X-Api-Key": key})
    if idx is None:
        return {}
    stats = _get_json(f"{base}/api/v1/indexerstats", {"X-Api-Key": key}) or {}
    ilist = (stats.get("indexers") if isinstance(stats, dict) else stats) or []
    return {
        "indexers": len(idx) if isinstance(idx, list) else None,
        "enabled": sum(1 for i in idx if i.get("enable")) if isinstance(idx, list) else None,
        "grabs": int(sum(_num(i.get("numberOfGrabs")) for i in ilist)),
        "queries": int(sum(_num(i.get("numberOfQueries")) for i in ilist)),
    }


def sabnzbd(cfg: dict) -> dict:
    base, key = (cfg.get("url") or "").rstrip("/"), cfg.get("key", "")
    if not base:
        return {}
    d = _get_json(f"{base}/api?mode=queue&output=json&apikey={urllib.parse.quote(key)}")
    q = d.get("queue") if isinstance(d, dict) else None
    if not isinstance(q, dict):
        return {}
    return {
        "status": q.get("status"),
        "queued": int(_num(q.get("noofslots"))),
        "speed_kbps": int(_num(q.get("kbpersec"))),
        "mb_left": round(_num(q.get("mbleft")), 1),
        "time_left": q.get("timeleft"),
    }


def tautulli(cfg: dict) -> dict:
    base, key = (cfg.get("url") or "").rstrip("/"), cfg.get("key", "")
    if not base:
        return {}
    d = _get_json(f"{base}/api/v2?apikey={urllib.parse.quote(key)}&cmd=get_activity")
    data = ((d or {}).get("response") or {}).get("data")
    if not isinstance(data, dict):
        return {}
    return {
        "streams": int(_num(data.get("stream_count"))),
        "transcodes": int(_num(data.get("stream_count_transcode"))),
        "bandwidth_mbps": round(_num(data.get("total_bandwidth")) / 1000, 1),
    }


if __name__ == "__main__":   # ponytail: pure response->stats mapping, offline
    v6 = _v6_stats({"queries": {"total": 10000, "blocked": 3100, "percent_blocked": 31.0},
                    "gravity": {"domains_being_blocked": 120000}}, {"blocking": "enabled"})
    assert v6 == {"status": "enabled", "queries": 10000, "blocked": 3100,
                  "block_pct": 31.0, "blocklist": 120000}, v6
    assert _v6_stats({}, None) == {}                      # no queries -> empty
    assert _v5_stats({"dns_queries_today": 5, "ads_blocked_today": 1,
                      "ads_percentage_today": 20.0, "domains_being_blocked": 9,
                      "status": "enabled"})["queries"] == 5
    assert _pihole_base("http://10.0.10.5/admin") == "http://10.0.10.5"

    # The collector-backed Types delegate to collectors/transport.py, which is NOT
    # SSRF-guarded (its docstring says so) — and a widget url is user-supplied while
    # the config.yaml token is layered underneath it. Both must consult the guard
    # before any request leaves, or a public url harvests the real API token.
    seen = []
    _orig = net.allowed
    net.allowed = lambda u: (seen.append(u), False)[1]
    try:
        assert proxmox({"url": "https://public.example", "token": "secret"}) == {}
        assert unifi({"url": "https://public.example", "api_key": "secret"}) == {}
    finally:
        net.allowed = _orig
    assert seen == ["https://public.example"] * 2, f"guard not consulted: {seen}"
    print("widgets/fetchers pihole self-check ok")
