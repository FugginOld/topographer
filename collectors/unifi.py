"""UniFi Network controller API — VLAN zones + clients + device uplinks.

Talks to the UniFi Network app running *on* a UniFi OS gateway (UCG, UDM, UXG).
This is the source that knows your real VLAN layout (subnets, purpose) and which
switch/AP port every client sits behind — far richer than SNMP or a ping sweep.

Auth (pick one in config.yaml):
  - api_key:  Settings -> Control Plane -> Integrations -> Create API Key
              (recommended: least privilege, no admin password stored)
  - username + password:  a *local* UniFi admin account (not UI/SSO)

Config:
  unifi:
    enabled: true
    url: https://192.168.1.1        # the gateway; UniFi OS proxies /proxy/network
    site: default
    verify_tls: false               # gateways ship a self-signed cert
    api_key: "CHANGEME"             # OR the username/password pair below
    # username: topology
    # password: "CHANGEME"
    zone_map:                       # optional: force a color band per network name
      IoT: {cls: iot, policy: "no lateral"}

Emits VLAN zones + a node per client and per infra device (gateway/switch/AP),
plus uplink/l2 links (client -> its switch/AP -> gateway). Degrades to [] on any
failure — a down controller yields no map, never a crash.
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

from .base import Collector
from core.schema import norm_mac, now_iso

log = logging.getLogger("collector.unifi")


def _ssl_ctx(verify: bool):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx

# UniFi device .type -> our node kind
_DEV_KIND = {"ugw": "firewall", "udm": "firewall", "uxg": "firewall",
             "usw": "switch", "uap": "ap"}


def _cls_for(name: str, purpose: str, zone_map: dict) -> str:
    """Color band for a VLAN, from an explicit override or a name heuristic."""
    if name in zone_map:
        return zone_map[name].get("cls", "unknown")
    h = (name or "").lower()
    if purpose == "guest" or "guest" in h:
        return "guest"
    if any(k in h for k in ("iot", "cam", "camera", "sensor")):
        return "iot"
    if any(k in h for k in ("mgmt", "manage", "admin")):
        return "mgmt"
    if any(k in h for k in ("srv", "server", "lab", "dmz", "nas")):
        return "srv"
    return "lan"


def networks_to_zones(nets: list[dict], zone_map: dict) -> list[dict]:
    zones = []
    for n in nets:
        if n.get("purpose") in ("wan", "wan2") or not n.get("enabled", True):
            continue
        subnet = n.get("ip_subnet") or n.get("ipv4_subnet") or ""
        if not subnet:
            continue                              # no L3 = nothing to place hosts in
        name = n.get("name") or f"VLAN{n.get('vlan') or ''}"
        zones.append({
            "vid": int(n.get("vlan") or 1),       # native/untagged LAN -> 1
            "name": name,
            "subnet": subnet,
            "policy": zone_map.get(name, {}).get("policy", ""),
            "cls": _cls_for(name, n.get("purpose", ""), zone_map),
        })
    return zones


def clients_to_nodes(clients: list[dict], ts: str) -> list[dict]:
    nodes = []
    for c in clients:
        mac, ip = c.get("mac"), c.get("ip")
        if not (mac or ip):
            continue
        nodes.append({
            "kind": "node",
            "ip": ip,
            "mac": mac,
            "name": c.get("hostname") or c.get("name") or ip,
            "nodekind": "host",
            "vendor": c.get("oui"),
            "online": True,
            "last_seen": ts,
        })
    return nodes


def devices_to_items(devices: list[dict], ts: str) -> list[dict]:
    """Infra nodes (gateway/switch/AP) + their uplink links."""
    items = []
    for d in devices:
        mac = d.get("mac")
        if not mac:
            continue
        items.append({
            "kind": "node",
            "ip": d.get("lan_ip") or d.get("ip"),   # gateway .ip is the WAN/public IP
            "mac": mac,
            "name": d.get("name") or d.get("model") or mac,
            "nodekind": _DEV_KIND.get(d.get("type"), "switch"),
            "online": d.get("state", 1) == 1,
            "last_seen": ts,
        })
        up = d.get("uplink") or {}
        up_mac = up.get("uplink_mac") or up.get("uplink_device_mac")
        if up_mac:
            items.append({"kind": "link", "src": norm_mac(mac), "dst": norm_mac(up_mac),
                          "linkkind": "uplink", "port": up.get("uplink_remote_port")})
    return items


def client_links(clients: list[dict]) -> list[dict]:
    """Each client -> the switch port or AP it is attached to."""
    links = []
    for c in clients:
        cid = norm_mac(c.get("mac"))
        if not cid:
            continue
        if c.get("is_wired") and c.get("sw_mac"):
            links.append({"kind": "link", "src": cid, "dst": norm_mac(c["sw_mac"]),
                          "linkkind": "l2", "port": str(c.get("sw_port") or "")})
        elif c.get("ap_mac"):
            links.append({"kind": "link", "src": cid, "dst": norm_mac(c["ap_mac"]),
                          "linkkind": "l2"})
    return links


class UnifiCollector(Collector):
    name = "unifi"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._opener = None
        self._nets = self._clients = self._devices = None

    # ---- HTTP (stdlib only, no requests dependency) --------------------------
    def _api_key(self):
        key = self.cfg.get("api_key")
        return key if key and key != "CHANGEME" else None

    def _build_opener(self):
        """Cookie-aware opener; logs in with username/password if no API key."""
        if self._opener is not None:
            return self._opener
        ctx = _ssl_ctx(self.cfg.get("verify_tls", False))
        op = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(CookieJar()),
        )
        if not self._api_key():                       # password auth -> get a cookie
            url = self.cfg["url"].rstrip("/")
            body = json.dumps({"username": self.cfg.get("username"),
                               "password": self.cfg.get("password")}).encode()
            req = urllib.request.Request(f"{url}/api/auth/login", data=body,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            try:
                op.open(req, timeout=15)
            except urllib.error.URLError as e:
                log.warning("unifi login failed — check url/credentials: %s", e)
                return None
        self._opener = op
        return op

    def _get(self, path: str):
        op = self._build_opener()
        if op is None:
            return []
        url = self.cfg["url"].rstrip("/")
        site = self.cfg.get("site", "default")
        req = urllib.request.Request(f"{url}/proxy/network/api/s/{site}/{path}")
        if self._api_key():
            req.add_header("X-API-KEY", self._api_key())
        try:
            with op.open(req, timeout=20) as r:
                return json.load(r).get("data", [])
        except (urllib.error.URLError, ValueError) as e:
            log.warning("unifi GET %s failed: %s", path, e)
            return []

    def _fetch(self):
        """Pull all three lists once; reused by zones() and collect()."""
        if self._nets is None:
            self._nets = self._get("rest/networkconf")
            self._clients = self._get("stat/sta")
            self._devices = self._get("stat/device")

    # ---- Collector API -------------------------------------------------------
    def zones(self) -> list[dict]:
        self._fetch()
        return networks_to_zones(self._nets, self.cfg.get("zone_map", {}))

    def collect(self) -> list[dict]:
        self._fetch()
        ts = now_iso()
        items = clients_to_nodes(self._clients, ts)
        items += devices_to_items(self._devices, ts)
        items += client_links(self._clients)
        return self._tag(items)


if __name__ == "__main__":  # ponytail: transform self-check, no live controller
    nets = [{"name": "IoT", "vlan": 50, "ip_subnet": "10.0.50.1/24", "purpose": "corporate", "enabled": True},
            {"name": "WAN", "purpose": "wan", "enabled": True}]
    z = networks_to_zones(nets, {})
    assert z == [{"vid": 50, "name": "IoT", "subnet": "10.0.50.1/24",
                  "policy": "", "cls": "iot"}], z
    devs = [{"mac": "aa:bb:cc:00:00:00", "name": "gw", "type": "udm",
             "ip": "47.1.2.3", "lan_ip": "10.0.10.1"},          # gateway: ip=WAN, use lan_ip
            {"mac": "aa:bb:cc:00:00:01", "name": "core-sw", "type": "usw", "ip": "10.0.10.2",
             "uplink": {"uplink_mac": "aa:bb:cc:00:00:00", "uplink_remote_port": 1}}]
    di = devices_to_items(devs, "t")
    assert di[0]["ip"] == "10.0.10.1", di[0]           # not the public WAN ip
    assert di[1]["nodekind"] == "switch" and di[2]["kind"] == "link", di
    assert di[2]["dst"] == "aa:bb:cc:00:00:00" and di[2]["port"] == 1, di
    cl = [{"mac": "de:ad:be:ef:00:01", "ip": "10.0.50.9", "hostname": "cam1",
           "is_wired": True, "sw_mac": "aa:bb:cc:00:00:01", "sw_port": 7}]
    n = clients_to_nodes(cl, "t")
    assert n[0]["name"] == "cam1" and n[0]["nodekind"] == "host", n
    lk = client_links(cl)
    assert lk[0]["dst"] == "aa:bb:cc:00:00:01" and lk[0]["port"] == "7", lk
    print("unifi transform self-check ok")
