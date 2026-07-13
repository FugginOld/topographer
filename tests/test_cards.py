"""Dashboard Card contract + the network→cards adapter (see CONTEXT.md § Card)."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from renderers.card import Card
from renderers.network_cards import from_topology


def test_card_contract():
    assert Card(id="n0", label="cpu").to_dict() == {"id": "n0", "label": "cpu"}   # drops unset
    # falsy-but-real values survive (port down, empty hub, empty disk)
    c = Card(id="p", label="eth0", up=False, cap=0, fill=0.0, sub="").to_dict()
    assert c == {"id": "p", "label": "eth0", "up": False, "cap": 0, "fill": 0.0, "sub": ""}, c


TOPO = {
    "zones": [
        {"vid": 1, "name": "Home", "subnet": "192.168.1.1/24", "cls": "lan"},
        {"vid": 2, "name": "Servers", "subnet": "192.168.2.1/24", "cls": "srv"},
    ],
    "nodes": [
        {"id": "wan", "ip": "47.1.2.3", "name": "WAN", "kind": "wan", "meta": {"public ip": "47.1.2.3"}},
        {"id": "aa:bb", "ip": "192.168.1.1", "name": "pve-gw", "kind": "firewall", "mac": "aa:bb"},
        {"id": "52:54", "name": "debian-vm", "kind": "host", "host": "pve-gw", "online": True},
        {"id": "h1", "ip": "192.168.2.9", "name": "nas", "kind": "server", "online": True},
    ],
}


def test_spine_and_placement():
    by = {c["id"]: c for c in from_topology(TOPO)}
    assert by["aa:bb"]["parent"] == "wan"                       # gateway hangs off WAN
    assert by["vlan1"]["parent"] == "aa:bb" == by["vlan2"]["parent"]  # VLANs off the gateway
    assert by["h1"]["parent"] == "vlan2"                        # placed by subnet


def test_host_nesting():
    by = {c["id"]: c for c in from_topology(TOPO)}
    assert by["52:54"]["parent"] == "aa:bb"                     # guest nested under host by name
    assert "_host" not in by["52:54"]                          # temp key cleaned up


def test_gatewayless_fallback():
    cards = from_topology({"zones": [], "nodes": [{"id": "x", "ip": "10.0.0.5", "name": "box"}]})
    root = next(c for c in cards if c["label"] == "NETWORK")
    assert root["kind"] == "root" and "parent" not in root
    assert next(c for c in cards if c["id"] == "x")["parent"] == "net"


if __name__ == "__main__":
    test_card_contract(); test_spine_and_placement(); test_host_nesting(); test_gatewayless_fallback()
    print("all card tests passed")
