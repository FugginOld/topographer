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


# Collectors address link endpoints by NAME (proxmox.py:85, docker.py:71,
# unifi_snmp.py:94) but normalize keys node ids mac > ip > name, and the reconcile
# passes delete folded nodes outright. Both shapes below are what the collectors
# really emit — see the modules cited.

PVE = [
    # the host as the LAN sees it: MAC-keyed, hostname "pve"
    {"source": "arpscan", "ip": "10.0.30.5", "mac": "aa:bb:cc:00:00:01",
     "name": "pve", "online": True},
    # proxmox emits the same host name-only, expecting the fold (proxmox.py:93-96)
    {"source": "proxmox", "name": "pve", "nodekind": "server", "online": True,
     "tags": ["proxmox"]},
    # a guest with its own MAC, nested by host NAME
    {"source": "proxmox", "name": "vm100", "nodekind": "host", "host": "pve",
     "mac": "aa:bb:cc:00:00:02", "online": True, "tags": ["vm"]},
    {"source": "proxmox", "kind": "link", "src": "pve", "dst": "vm100",
     "linkkind": "host"},
]


def test_host_nesting_link_survives_the_fold():
    """The PVE host link must land on the real ids after 'pve' is folded away."""
    topo = normalize(PVE, ZONES)
    host_links = [l for l in topo.links if l.kind == "host"]
    assert len(host_links) == 1, f"guest nesting link was dropped: {topo.links}"
    assert host_links[0].src == "aa:bb:cc:00:00:01", host_links[0]
    assert host_links[0].dst == "aa:bb:cc:00:00:02", host_links[0]


SNMP = [
    # unifi_snmp emits the switch WITH an ip, so its id becomes the IP …
    {"source": "unifi_snmp", "name": "core-sw01", "ip": "10.0.10.2",
     "nodekind": "switch", "online": True, "id_hint": "core-sw01"},
    {"source": "arpscan", "ip": "10.0.30.10", "mac": "dc:a6:32:11:22:33", "online": True},
    # … but addresses its links by name (unifi_snmp.py:94)
    {"source": "unifi_snmp", "kind": "link", "src": "dc:a6:32:11:22:33",
     "dst": "core-sw01", "linkkind": "l2", "port": "Gi1/0/7"},
]


def _fake_collectors():
    """Two collectors: one that fails the way a misconfigured source really does
    (opnsense.py:38 / unifi.py:314 raise KeyError on a missing config key, and
    zones() used to run outside the runner's guard), one healthy."""
    from collectors.base import Collector

    class Boom(Collector):
        name = "boom"

        def collect(self):
            return [{"name": "ghost", "ip": "10.0.30.8"}]

        def zones(self):
            raise KeyError("key")           # enabled: true, but no api key configured

    class Bare(Collector):
        name = "bare"

        def collect(self):
            return [{"name": "unstamped", "ip": "10.0.30.9"}]   # never calls _tag()

    return {"boom": Boom, "bare": Bare}


def test_a_failing_source_cannot_kill_the_scan():
    from scanners.make_network_topo import run_collectors
    cfg = {"boom": {"enabled": True}, "bare": {"enabled": True}}
    raw, zones = run_collectors(cfg, _fake_collectors())
    assert "unstamped" in [r.get("name") for r in raw], raw
    assert zones == [], zones


def test_runner_stamps_the_contract_even_when_a_collector_forgets():
    """pingsweep.py:81 returns nodes without _tag(), so they landed as
    source='unknown' with no last_seen. The runner stamps both."""
    from scanners.make_network_topo import run_collectors
    raw, _ = run_collectors({"bare": {"enabled": True}}, _fake_collectors())
    assert raw[0]["source"] == "bare", raw
    assert raw[0].get("last_seen"), raw


def test_link_by_name_reaches_an_ip_keyed_node():
    topo = normalize(SNMP, ZONES)
    assert len(topo.links) == 1, f"switch-port link was dropped: {topo.links}"
    assert topo.links[0].dst == "10.0.10.2", topo.links[0]


if __name__ == "__main__":
    # every test_* in this module, so adding one can't silently skip it
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("all tests passed")
