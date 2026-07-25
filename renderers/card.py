"""The dashboard view-model — see CONTEXT.md § Card.

A `Card` is one node in the tree `renderers/html/index.html` draws. This module
OWNS the contract: the field set lives here once, instead of implicitly across a
JS reader and three independent writers (the two hardware scanners and
`renderers/network_cards.from_topology`).

Pure and dependency-free on purpose — the standalone scanners import only this.
`to_dict()` drops unset (`None`) fields, so output matches the hand-built dicts
it replaces (falsy-but-real values like `up=False`, `cap=0`, `fill=0.0` are kept).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Card:
    id: str
    label: str
    # ── core (every writer) ──
    parent: Optional[str] = None
    sub: Optional[str] = None
    cls: Optional[str] = None            # colour class: gen3/4/5 | mgmt/lan/srv/iot/…
    kind: Optional[str] = None           # cpu/hub/leaf (hardware) | firewall/wan/zone/host/root (net)
    meta: Optional[dict] = None          # key/value detail rows (hold-to-preview)
    # ── animation fields (hardware fabric only) ──
    grp: Optional[str] = None            # net/disk/mem/gpu/usb/display — drives live load
    cap: Optional[float] = None          # bandwidth/capacity for the readout + bar
    up: Optional[bool] = None            # port link state (LED)
    fill: Optional[float] = None         # disk used fraction
    link: Optional[str] = None           # link label (e.g. "x4 G4", "DDR5")
    iface: Optional[str] = None          # OS iface name, so telemetry maps bytes/sec here

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def project(nodes) -> list[dict]:
    """Foreign node dicts -> Cards, keeping only declared fields.

    The seam for untrusted input: `/api/ingest` takes cards from every remote agent
    and every SSH-scanned host, and the third writer (`make_linux_topo.py`) can't
    import this module, so its output is only a Card by convention. Projecting on
    arrival makes the contract hold for all three writers without any of them
    changing — the same move `_tele.ZERO` already makes for pushed telemetry.

    A node without `id` and `label` can't be drawn, so it is dropped.
    """
    out = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        known = {k: v for k, v in n.items() if k in Card.__dataclass_fields__ and v is not None}
        if not known.get("id") or not known.get("label"):
            continue
        out.append(known)
    return out


if __name__ == "__main__":  # ponytail: contract self-check
    assert Card(id="n0", label="cpu").to_dict() == {"id": "n0", "label": "cpu"}, "drops unset"
    c = Card(id="p1", label="eth0", parent="root", cls="gen4", grp="net",
             up=False, cap=0, fill=0.0).to_dict()
    assert c == {"id": "p1", "label": "eth0", "parent": "root", "cls": "gen4",
                 "grp": "net", "up": False, "cap": 0, "fill": 0.0}, c   # keeps falsy-but-real
    # project(): the untrusted-input seam
    p = project([{"id": "n0", "label": "cpu", "cap": 0, "up": False, "fill": 0.0,
                  "bogus": "x", "__proto__": "y"},
                 {"label": "no id"}, {"id": "no label"}, "not a dict", None])
    assert len(p) == 1, p
    assert p[0] == {"id": "n0", "label": "cpu", "cap": 0, "up": False, "fill": 0.0}, p
    assert project([]) == [] and project(None) == []
    # every field a scanner really emits survives the projection
    full = {"id": "p1", "label": "eth0", "parent": "root", "sub": "1GbE", "cls": "gen4",
            "kind": "leaf", "meta": {"a": 1}, "grp": "net", "cap": 1.0, "up": True,
            "fill": 0.5, "link": "x4 G4", "iface": "eth0"}
    assert project([full])[0] == full, project([full])
    print("card contract self-check ok")
