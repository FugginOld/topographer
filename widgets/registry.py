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
        "desc": "Node health, running VMs / LXC, CPU + memory.",
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

_catalog_cache: dict = {"mtime": None, "rows": []}


def _cached_catalog() -> list:
    """catalog.json's rows, re-read only when the file changes. It is ~47 KB and
    was parsed afresh on every /api/widget-catalog request."""
    try:
        mtime = os.path.getmtime(_CATALOG_JSON)
    except OSError:
        return []
    if _catalog_cache["mtime"] != mtime:
        try:
            with open(_CATALOG_JSON, encoding="utf-8") as fh:
                _catalog_cache["rows"] = (json.load(fh) or {}).get("widgets") or []
        except Exception:
            _catalog_cache["rows"] = []
        _catalog_cache["mtime"] = mtime
    return _catalog_cache["rows"]


def get(type_id: str) -> dict | None:
    return _BY_ID.get(type_id)


def field_names(type_id: str) -> list[str]:
    return [f["name"] for f in (_BY_ID.get(type_id) or {}).get("fields", [])]


def secret_fields(type_id: str) -> list[str]:
    return [f["name"] for f in (_BY_ID.get(type_id) or {}).get("fields", []) if f.get("secret")]


def full_catalog() -> list[dict]:
    """The whole browsable store: every Homepage widget from catalog.json, merged
    with our built ones. Built types carry their real field schema (installable);
    unbuilt types are listed for reference (built=False, no fetcher, can't install)."""
    allw = _cached_catalog()
    built = {t["id"]: t for t in CATALOG}

    def row(wid: str, w: dict) -> dict:
        """One store row. `w` is the catalog.json entry ({} for a built type the
        cache doesn't list); a built type overrides it with its real schema."""
        b = built.get(wid)
        r = {"id": wid, "label": w.get("title") or wid, "category": w.get("category") or "Other",
             "icon": wid, "built": False, "desc": w.get("note") or "",
             "shows": w.get("shows") or [], "doc": w.get("doc"), "config": w.get("config") or []}
        if b:
            r.pop("config")                              # unbuilt-only: raw config keys, no schema
            r.update(label=b["label"], category=b.get("category") or r["category"],
                     icon=b.get("icon") or wid, built=True, beta=bool(b.get("beta")),
                     desc=b.get("desc") or r["desc"], fields=[dict(f) for f in b["fields"]],
                     config_key=b.get("config_key"))
        return r

    out, seen = [], set()
    for w in allw:                                       # first entry wins on a duplicate id
        wid = w.get("id")
        if wid and wid not in seen:
            seen.add(wid)
            out.append(row(wid, w))
    out += [row(wid, {}) for wid in built if wid not in seen]   # built types the cache misses (e.g. seerr)
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
    assert fetch("nope", {}) == {}
    fc = full_catalog()                      # merged store: all cached + our built ones
    assert full_catalog() == fc, "cached catalog must not drift between calls"
    assert len(fc) >= len(CATALOG) and sum(1 for w in fc if w["built"]) == len(CATALOG)
    # the two store shapes: installable (field schema) vs reference-only (raw config keys)
    built_w = next(w for w in fc if w["built"])
    unbuilt = next(w for w in fc if not w["built"])
    assert set(built_w) == {"id", "label", "category", "icon", "built", "beta", "desc",
                            "shows", "doc", "fields", "config_key"}, sorted(built_w)
    assert set(unbuilt) == {"id", "label", "category", "icon", "built", "desc",
                            "shows", "doc", "config"}, sorted(unbuilt)
    assert all(w["category"] and w["label"] for w in fc)          # no blank rows in the store
    assert len({w["id"] for w in fc}) == len(fc), "duplicate id in the merged store"
    _json.dumps(fc)                          # JSON-safe (no fetch callables leaked)
    print(f"widgets/registry self-check ok ({len(CATALOG)} built / {len(fc)} in store)")
