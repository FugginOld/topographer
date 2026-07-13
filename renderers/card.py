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


if __name__ == "__main__":  # ponytail: contract self-check
    assert Card(id="n0", label="cpu").to_dict() == {"id": "n0", "label": "cpu"}, "drops unset"
    c = Card(id="p1", label="eth0", parent="root", cls="gen4", grp="net",
             up=False, cap=0, fill=0.0).to_dict()
    assert c == {"id": "p1", "label": "eth0", "parent": "root", "cls": "gen4",
                 "grp": "net", "up": False, "cap": 0, "fill": 0.0}, c   # keeps falsy-but-real
    print("card contract self-check ok")
