"""Widget Type catalog — the declarative list the store UI browses and the server
validates against. Each Type binds a config-field schema to a fetcher.

field: {name, label, type: text|url|password|number|bool, required?, secret?}
Adding a Type = one CATALOG entry + one fetcher (docs/prd/widget-store.md, Phase 2).
"""
from __future__ import annotations

import json
import os

from . import fetchers

_CATALOG_JSON = os.path.join(os.path.dirname(__file__), "catalog.json")

# shared field set for the url + API-key services (Servarr, SABnzbd, Tautulli, …)
_ARR_FIELDS = [
    {"name": "url", "label": "URL (http://host:port)", "type": "url", "required": True},
    {"name": "key", "label": "API key", "type": "password", "required": True, "secret": True},
]

CATALOG = [
    {
        "id": "pihole", "label": "Pi-hole", "category": "Network", "icon": "pi-hole",
        "desc": "DNS queries + blocked (Pi-hole v5 and v6).",
        "fields": [
            {"name": "url", "label": "Pi-hole URL", "type": "url", "required": True},
            {"name": "token", "label": "App password (v6) / API token (v5)", "type": "password", "secret": True},
        ],
        "fetch": fetchers.pihole,
    },
    {
        "id": "proxmox", "label": "Proxmox VE", "category": "System", "icon": "proxmox",
        "desc": "Node health plus each node's VMs / LXC, with CPU + memory.",
        "config_key": "proxmox",     # inherit config.yaml's proxmox block when the form is blank
        "fields": [
            {"name": "url", "label": "PVE URL", "type": "url"},
            {"name": "token", "label": "API token (user@realm!id=secret)", "type": "password", "secret": True},
            {"name": "verify_tls", "label": "Verify TLS", "type": "bool"},
            {"name": "node", "label": "Node (optional)", "type": "text"},
        ],
        "fetch": fetchers.proxmox,
    },
    {
        "id": "unifi", "label": "UniFi", "category": "Network", "icon": "unifi",
        "desc": "WAN status, client count, live throughput.",
        "config_key": "unifi",       # inherit config.yaml's unifi block when the form is blank
        "fields": [
            {"name": "url", "label": "Gateway URL", "type": "url"},
            {"name": "api_key", "label": "API key", "type": "password", "secret": True},
            {"name": "site", "label": "Site", "type": "text"},
            {"name": "verify_tls", "label": "Verify TLS", "type": "bool"},
        ],
        "fetch": fetchers.unifi,
    },
    # ── media stack: url + API key each (Settings -> General/Security in the app).
    # sonarr/radarr/bazarr/seerr are engine-driven now (widgets/definitions.py); only
    # the ones needing non-engine logic (aggregation / coercion) stay hand-coded here.
    {
        "id": "prowlarr", "label": "Prowlarr", "category": "Downloads", "icon": "prowlarr",
        "desc": "Indexers, grabs, queries.", "fields": _ARR_FIELDS, "fetch": fetchers.prowlarr,
    },
    {
        "id": "sabnzbd", "label": "SABnzbd", "category": "Downloads", "icon": "sabnzbd",
        "desc": "Queue size, speed, time left.", "fields": _ARR_FIELDS, "fetch": fetchers.sabnzbd,
    },
    {
        "id": "tautulli", "label": "Tautulli", "category": "Media", "icon": "tautulli",
        "desc": "Active Plex streams + bandwidth.", "fields": _ARR_FIELDS, "fetch": fetchers.tautulli,
    },
]

# engine-driven widgets (data-defined, no hand-written fetcher) — see engine.py
from . import definitions, engine  # noqa: E402
CATALOG += [engine.entry(d) for d in definitions.DEFS]

_BY_ID = {t["id"]: t for t in CATALOG}


def get(type_id: str) -> dict | None:
    return _BY_ID.get(type_id)


def field_names(type_id: str) -> list[str]:
    return [f["name"] for f in (_BY_ID.get(type_id) or {}).get("fields", [])]


def secret_fields(type_id: str) -> list[str]:
    return [f["name"] for f in (_BY_ID.get(type_id) or {}).get("fields", []) if f.get("secret")]


def catalog_public() -> list[dict]:
    """Catalog minus the fetch callables — JSON-safe for the browser."""
    return [{k: v for k, v in t.items() if k != "fetch"} for t in CATALOG]


def full_catalog() -> list[dict]:
    """The whole browsable store: every Homepage widget from catalog.json, merged
    with our built ones. Built types carry their real field schema (installable);
    unbuilt types are listed for reference (built=False, no fetcher, can't install)."""
    try:
        allw = (json.load(open(_CATALOG_JSON, encoding="utf-8")) or {}).get("widgets") or []
    except Exception:
        allw = []
    built = {t["id"]: t for t in CATALOG}
    out, seen = [], set()
    for w in allw:
        wid = w.get("id")
        if not wid or wid in seen:
            continue
        seen.add(wid)
        b = built.get(wid)
        if b:
            out.append({"id": wid, "label": b["label"], "category": b.get("category") or w.get("category") or "Other",
                        "icon": b.get("icon") or wid, "built": True, "beta": bool(b.get("beta")),
                        "desc": b.get("desc") or w.get("note") or "", "shows": w.get("shows") or [], "doc": w.get("doc"),
                        "fields": [dict(f) for f in b["fields"]], "config_key": b.get("config_key")})
        else:
            out.append({"id": wid, "label": w.get("title") or wid, "category": w.get("category") or "Other",
                        "icon": wid, "built": False, "desc": w.get("note") or "",
                        "shows": w.get("shows") or [], "doc": w.get("doc"), "config": w.get("config") or []})
    for wid, b in built.items():                         # our built types not in the cache (e.g. seerr)
        if wid not in seen:
            out.append({"id": wid, "label": b["label"], "category": b.get("category") or "Other",
                        "icon": b.get("icon") or wid, "built": True, "beta": bool(b.get("beta")),
                        "desc": b.get("desc") or "", "shows": [], "doc": None,
                        "fields": [dict(f) for f in b["fields"]], "config_key": b.get("config_key")})
    return out


def fetch(type_id: str, config: dict) -> dict:
    """Live stats for one configured widget. Never raises — a bad fetcher must not
    500 the dashboard (defence-in-depth on top of the fetchers' own guards)."""
    t = _BY_ID.get(type_id)
    if not t:
        return {}
    try:
        return t["fetch"](config or {}) or {}
    except Exception:
        return {}


if __name__ == "__main__":   # ponytail: catalog integrity + JSON-safety, offline
    import json as _json
    ids = [t["id"] for t in CATALOG]
    assert len(ids) == len(set(ids)), "duplicate widget id"
    for t in CATALOG:
        assert t["fields"] and callable(t["fetch"]), t["id"]
        assert fetch(t["id"], {}) == {}, f"{t['id']} not graceful on empty config"   # no url -> {}
    assert secret_fields("proxmox") == ["token"]
    assert field_names("unifi")[0] == "url"
    _json.dumps(catalog_public())            # must be serialisable (no callables)
    assert fetch("nope", {}) == {}
    fc = full_catalog()                      # merged store: all cached + our built ones
    assert len(fc) >= len(CATALOG) and sum(1 for w in fc if w["built"]) == len(CATALOG)
    _json.dumps(fc)                          # JSON-safe (no fetch callables leaked)
    print(f"widgets/registry self-check ok ({len(CATALOG)} built / {len(fc)} in store)")
