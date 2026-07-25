"""Widget config policy: what the browser may see, what may be stored, and what
the fetcher actually runs with.

One owner for the secret protocol, which used to be split across the server, the
registry and the store — so a path that forgot a step (snapshot restore skipped
the field whitelist) could echo an API key to the browser.

The protocol, in one place:
  - `public()`   — outbound: secret fields blank, `secret_set` says which are held.
  - `clean()`    — inbound: whitelist to the Type's declared fields; a blank secret
                   preserves the stored one, so the browser never echoes a key back.
  - `effective()`— what the fetcher sees: form values over an inherited config.yaml
                   block (Type.config_key), so blanks fall through to admin config.

`effective()` takes the inherited block as an argument rather than reading
config.yaml itself — the caller owns that file, this module owns the merge.
"""
from __future__ import annotations

from . import registry


def public(w: dict) -> dict:
    """A stored widget, safe to send to the browser."""
    secrets = set(registry.secret_fields(w.get("type", "")))
    cfg = w.get("config") or {}
    return {"id": w["id"], "type": w["type"], "position": w.get("position", 0),
            "interval": w.get("interval"),
            "config": {k: ("" if k in secrets else v) for k, v in cfg.items()},
            "secret_set": {k: bool(cfg.get(k)) for k in secrets}}


def clean(wtype: str, config: dict | None, prev: dict | None = None) -> dict:
    """Whitelist to the Type's known fields; a blank/absent secret keeps the prior
    stored value. An unknown Type declares no fields, so it cleans to {}."""
    secrets, prev = set(registry.secret_fields(wtype)), (prev or {})
    out: dict = {}
    for k in registry.field_names(wtype):
        v = (config or {}).get(k)
        if k in secrets and not v:
            if prev.get(k):
                out[k] = prev[k]
        elif v is not None:
            out[k] = v
    return out


def effective(w: dict, inherited: dict | None = None) -> dict:
    """Config the fetcher runs with: form values override the inherited block,
    blanks fall through to it."""
    form = {k: v for k, v in (w.get("config") or {}).items() if v not in (None, "")}
    return {**(inherited or {}), **form}


def inherits(wtype: str) -> str:
    """Which config.yaml block this Type inherits from, '' if none."""
    t = registry.get(wtype)
    return (t or {}).get("config_key") or ""


if __name__ == "__main__":   # ponytail: the secret protocol, offline
    stored = {"id": "w1", "type": "pihole", "config": {"url": "http://pi", "token": "SECRET"}}

    p = public(stored)
    assert p["config"]["token"] == "" and p["config"]["url"] == "http://pi", p
    assert p["secret_set"] == {"token": True}, p
    assert "SECRET" not in str(p), p                       # the whole point

    # blank secret preserves the stored one; a supplied one replaces it
    assert clean("pihole", {"url": "http://pi", "token": ""}, stored["config"])["token"] == "SECRET"
    assert clean("pihole", {"url": "http://pi", "token": "NEW"}, stored["config"])["token"] == "NEW"
    # unknown keys never survive, whatever the caller sends
    assert "evil" not in clean("pihole", {"url": "http://pi", "evil": "x"})
    # an unknown Type has no declared fields -> nothing is stored, nothing leaks
    assert clean("not-a-real-type", {"token": "hunter2", "url": "http://x"}) == {}
    assert public({"id": "w", "type": "not-a-real-type",
                   "config": {}})["secret_set"] == {}

    # inheritance: blanks fall through to the admin block, form values win
    base = {"url": "https://pve.lan", "token": "FROM-YAML", "verify_tls": False}
    assert effective({"config": {"url": "", "token": ""}}, base)["token"] == "FROM-YAML"
    assert effective({"config": {"url": "https://other"}}, base)["url"] == "https://other"
    assert effective({"config": {}}, None) == {}
    assert inherits("proxmox") == "proxmox" and inherits("pihole") == ""
    print("widgets/policy self-check ok")
