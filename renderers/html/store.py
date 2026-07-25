"""Topology store — persistence over a guarded directory (`out/topologies/*.json`).

Deep and narrow: bytes in/out, plus the single path-injection barrier every
user-supplied id passes through. No presentation (the sidebar row shaping lives
with the Handler), so this imports nothing server-specific and is testable with
no HTTP — see the __main__ self-check.
"""
from __future__ import annotations

import json
import os
import re
import time

from _guard import guarded_path

_HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.abspath(os.path.join(_HERE, "..", "..", "out", "topologies"))


def path(tid: str) -> str:
    """Resolve <tid>.json inside STORE — the shared path-injection barrier."""
    return guarded_path(STORE, tid, "topology id")


def slug(name: str) -> str:
    """Timestamped id — keeps generated topologies unique."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "topology"
    return f"{s}-{int(time.time())}"


def stable_slug(name: str) -> str:
    """No timestamp — one stable entry per host, so re-pushes overwrite."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "host"


def save(topo: dict, source: str | None = None) -> dict:
    """Persist a topology keyed by its (slugged) name. `source` records the push
    origin for reported hosts. Returns {id, name}."""
    name = (topo.get("name") or "host").strip()
    tid = stable_slug(name)
    topo["name"] = name
    if source is not None:
        topo["source"] = source
    os.makedirs(STORE, exist_ok=True)
    with open(path(tid), "w", encoding="utf-8") as fh:
        json.dump(topo, fh, indent=2)
    return {"id": tid, "name": name}


def save_as(tid: str, topo: dict) -> dict:
    """Persist under an explicit id — snapshot restore, which must reproduce the
    ids it captured (a generated id is timestamped, not slug(name), so re-deriving
    it would fork a second copy of the same host). The id still goes through the
    same guard as every other path, so a hostile one raises instead of escaping."""
    # guarded_path() basename-reduces, so "../../x" would land inside the store as
    # "x" rather than raise. Contained, but a real snapshot never holds an id like
    # that — refuse it instead of quietly inventing an entry.
    if tid != os.path.basename(tid):
        raise ValueError("topology id must be a plain id, not a path")
    p = path(tid)                                  # charset allowlist + containment
    os.makedirs(STORE, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(topo, fh, indent=2)
    return {"id": tid, "name": topo.get("name") or tid}


def load(tid: str) -> dict:
    with open(path(tid), encoding="utf-8") as fh:
        return json.load(fh)


def ids() -> list[str]:
    """Every stored topology id (no order guarantee)."""
    if not os.path.isdir(STORE):
        return []
    return [f[:-5] for f in os.listdir(STORE) if f.endswith(".json")]


def delete(tid: str) -> None:
    """Remove a topology; a bad/absent id is a no-op."""
    try:
        fp = path(tid)
    except ValueError:
        return
    if os.path.isfile(fp):
        os.remove(fp)


if __name__ == "__main__":  # ponytail: slug policy; the path barrier is tested in _guard.py
    assert os.path.dirname(path("network")) == os.path.realpath(STORE)   # barrier wired
    assert stable_slug("PVE Host!") == "pve-host"
    assert slug("x").startswith("x-") and slug("x")[2:].isdigit()
    print("store self-check ok")
