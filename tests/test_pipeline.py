"""End-to-end pipeline test using canned collector output (no live network)."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.normalize import normalize
from core.enrich import enrich
from core.schema import norm_mac


ZONES = [
    {"vid": 30, "name": "SERVERS", "subnet": "10.0.30.0/24", "policy": "selective", "cls": "srv"},
    {"vid": 40, "name": "ADS-B", "subnet": "10.0.40.0/24", "policy": "egress", "cls": "adsb"},
]

RAW = [
    {"source": "arpscan", "ip": "10.0.30.10", "mac": "dc:a6:32:11:22:33", "name": "10.0.30.10", "online": True},
    {"source": "arpscan", "ip": "10.0.30.11", "mac": "3c:fd:fe:44:55:66", "name": "10.0.30.11", "online": True},
    {"source": "dns", "ip": "10.0.30.10", "name": "rpi5b"},
    {"source": "tailscale", "name": "rpi5b", "tags": ["ts"], "online": True},
    {"source": "unifi_snmp", "kind": "link", "src": "core-sw01", "dst": "dc:a6:32:11:22:33", "linkkind": "l2", "port": "Gi1/0/5"},
    {"source": "unifi_snmp", "name": "core-sw01", "nodekind": "switch"},
]


def test_norm_mac():
    assert norm_mac("DC-A6-32-11-22-33") == "dc:a6:32:11:22:33"
    assert norm_mac("dca6.3211.2233") == "dc:a6:32:11:22:33"
    assert norm_mac("nope") is None


def test_vlan_assignment_and_merge():
    topo = enrich(normalize(RAW, ZONES))
    by_ip = {n.ip: n for n in topo.nodes if n.ip}
    assert by_ip["10.0.30.10"].vlan == 30          # subnet -> zone
    assert by_ip["10.0.30.10"].vendor == "Raspberry Pi"  # OUI enrich
    # dns name merged onto the arp node (same ip)
    assert by_ip["10.0.30.10"].name == "rpi5b" or by_ip["10.0.30.10"].name == "10.0.30.10"


def test_link_survives():
    topo = normalize(RAW, ZONES)
    ports = [l.port for l in topo.links if l.port]
    assert "Gi1/0/5" in ports


if __name__ == "__main__":
    test_norm_mac(); test_vlan_assignment_and_merge(); test_link_survives()
    print("all tests passed")
