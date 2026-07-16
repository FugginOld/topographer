"""Widget store — per-host persistence of configured Widget instances.

Deep and narrow, modelled on `store.py`: bytes in/out through one path-injection
barrier, no presentation and no HTTP. A host owns a list of Widgets
(`{id, type, config, position}`); config may hold service secrets, so this file
is the ONLY place they live — server-side, gitignored (`out/`). Masking for the
wire happens in the server layer, not here.

    out/widgets/<host>.json  ->  {"widgets": [ {id, type, config, position}, ... ]}
"""
from __future__ import annotations

import json
import os
import re

from _guard import guarded_path

_HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.abspath(os.path.join(_HERE, "..", "..", "out", "widgets"))


def _path(host: str) -> str:
    """Resolve <host>.json inside STORE — the shared path-injection barrier."""
    return guarded_path(STORE, host, "host id")


def _read(host: str) -> list[dict]:
    try:
        with open(_path(host), encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("widgets") or []
    except (OSError, ValueError):
        return []


def _write(host: str, widgets: list[dict]) -> None:
    os.makedirs(STORE, exist_ok=True)
    with open(_path(host), "w", encoding="utf-8") as fh:
        json.dump({"widgets": widgets}, fh, indent=2)


def list_for(host: str) -> list[dict]:
    """This host's widgets, ordered by position (raw config — secrets included)."""
    return sorted(_read(host), key=lambda w: w.get("position", 0))


def _set_interval(w: dict, interval) -> None:
    """A positive int sets a per-widget refresh; 0 clears it (back to the global
    default); None leaves it unchanged."""
    if interval is None:
        return
    try:
        iv = int(interval)
    except (TypeError, ValueError):
        return
    if iv > 0:
        w["interval"] = iv
    else:
        w.pop("interval", None)


def add(host: str, wtype: str, config: dict, interval=None) -> dict:
    ws = _read(host)
    n = max((int(re.sub(r"\D", "", w["id"]) or 0) for w in ws), default=0) + 1
    w = {"id": f"w-{n}", "type": wtype, "config": config or {}, "position": len(ws)}
    _set_interval(w, interval)
    ws.append(w)
    _write(host, ws)
    return w


def update(host: str, wid: str, config: dict, interval=None) -> dict | None:
    """Replace a widget's config wholesale. The caller (server) has already
    resolved masked secrets against the stored config, so this is authoritative."""
    ws = _read(host)
    for w in ws:
        if w["id"] == wid:
            w["config"] = config or {}
            _set_interval(w, interval)
            _write(host, ws)
            return w
    return None


def delete(host: str, wid: str) -> None:
    ws = [w for w in _read(host) if w["id"] != wid]
    _write(host, ws)


def reorder(host: str, order: list[str]) -> None:
    """Apply a new id order; ids not in `order` keep trailing positions."""
    pos = {wid: i for i, wid in enumerate(order)}
    ws = _read(host)
    for w in ws:
        w["position"] = pos.get(w["id"], len(order) + w.get("position", 0))
    _write(host, ws)


if __name__ == "__main__":   # ponytail: CRUD round-trip; the path barrier is tested in _guard.py
    # CRUD against a scratch host, then clean up its file
    h = "__selftest_host__"
    try:
        a = add(h, "pihole", {"url": "http://x", "token": "s"})
        b = add(h, "unifi", {"url": "https://y"})
        assert [w["id"] for w in list_for(h)] == [a["id"], b["id"]]
        reorder(h, [b["id"], a["id"]])
        assert [w["id"] for w in list_for(h)] == [b["id"], a["id"]]
        update(h, a["id"], {"url": "http://x", "token": "s2"})    # replace, not merge
        assert next(w for w in list_for(h) if w["id"] == a["id"])["config"]["token"] == "s2"
        delete(h, a["id"])
        assert [w["id"] for w in list_for(h)] == [b["id"]]
    finally:
        try:
            os.remove(_path(h))
        except OSError:
            pass
    print("widget_store self-check ok")
