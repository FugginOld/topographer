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


def _fmt_uptime(secs) -> str | None:
    if not secs:
        return None
    secs = int(secs)
    d, h = secs // 86400, (secs % 86400) // 3600
    return f"{d}d {h}h" if d else f"{h}h"


def devices_to_items(devices: list[dict], ts: str) -> list[dict]:
    """Infra nodes (gateway/switch/AP), gateway detail + WAN node, uplink links."""
    items = []
    for d in devices:
        mac = d.get("mac")
        if not mac:
            continue
        node = {
            "kind": "node",
            "ip": d.get("lan_ip") or d.get("ip"),   # gateway .ip is the WAN/public IP
            "mac": mac,
            "name": d.get("name") or d.get("model") or mac,
            "nodekind": _DEV_KIND.get(d.get("type"), "switch"),
            "online": d.get("state", 1) == 1,
            "last_seen": ts,
        }
        up = d.get("uplink") or {}
        is_gw = d.get("type") in ("ugw", "udm", "uxg")
        if is_gw:
            # gateway: full identity + WAN uplink (up is the WAN interface dict)
            wan_ip = up.get("ip") or d.get("last_wan_ip")
            isp_gw = (up.get("gateways") or [None])[0]
            wan_link = " ".join(x for x in (up.get("media"),
                                "up" if up.get("up") else "down") if x) or None
            node["meta"] = {k: v for k, v in (
                ("model", d.get("model")),
                ("firmware", d.get("displayable_version") or d.get("version")),
                ("lan ip", d.get("lan_ip")),
                ("wan ip", wan_ip),
                ("uptime", _fmt_uptime(d.get("uptime"))),
                ("serial", d.get("serial")),
            ) if v}
            items.append(node)
            if wan_ip:                                  # WAN node + gateway->WAN link
                items.append({
                    "kind": "node", "ip": wan_ip, "name": "WAN / Internet",
                    "nodekind": "wan", "online": bool(up.get("up", True)), "last_seen": ts,
                    "meta": {k: v for k, v in (("public ip", wan_ip),
                             ("isp gateway", isp_gw), ("link", wan_link),
                             ("interface", up.get("name"))) if v},
                })
                items.append({"kind": "link", "src": norm_mac(mac), "dst": wan_ip,
                              "linkkind": "uplink", "port": up.get("name")})
            continue
        items.append(node)
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


def _subsys(health: list[dict], name: str) -> dict:
    return next((s for s in (health or []) if s.get("subsystem") == name), {})


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _gw_temp(dev: dict) -> float:
    """Gateway temperature from the device's temperatures[] list (prefer a CPU probe)."""
    temps = dev.get("temperatures") or []
    for t in temps:
        if t.get("type") == "cpu" or "cpu" in str(t.get("name", "")).lower():
            return _num(t.get("value"))
    return _num(temps[0].get("value")) if temps else 0.0


def health_summary(health: list[dict], devices: list[dict], clients: list[dict]) -> dict:
    """Compact live gateway summary for the dashboard panel: WAN, throughput,
    clients, gateway + infra device health. Pure — unit-tested with sample data;
    the collector feeds it the three live API lists (stat/health|device|sta).

    Gateway CPU/mem/uptime/firmware live in the wan subsystem's gw_* fields (not
    on stat/device), and the gateway device is matched by gw_mac since a UCG's
    .type isn't the classic ugw/udm/uxg."""
    wan, www, lan = _subsys(health, "wan"), _subsys(health, "www"), _subsys(health, "lan")
    devices, clients = devices or [], clients or []
    gw_mac = wan.get("gw_mac")
    gwdev = (next((d for d in devices if d.get("mac") == gw_mac), {}) if gw_mac
             else next((d for d in devices if d.get("type") in ("ugw", "udm", "uxg")), {}))
    gss = wan.get("gw_system-stats") or gwdev.get("system-stats") or {}
    status = wan.get("status") or www.get("status")
    wired = sum(1 for c in clients if c.get("is_wired"))
    uw = (wan.get("uptime_stats") or {}).get("WAN") or {}
    netcounts: dict = {}
    for c in clients:
        nm = c.get("network") or c.get("last_connection_network_name")
        if nm:
            netcounts[nm] = netcounts.get(nm, 0) + 1
    return {
        "gateway": {
            "name": wan.get("gw_name") or gwdev.get("name") or gwdev.get("model"),
            "model": gwdev.get("model"),
            "firmware": wan.get("gw_version") or gwdev.get("displayable_version") or gwdev.get("version"),
            "uptime": _fmt_uptime(gss.get("uptime") or gwdev.get("uptime")),
            "cpu": _num(gss.get("cpu")),
            "mem": _num(gss.get("mem")),
            "temp": _gw_temp(gwdev),
        },
        "wan": {
            "up": status == "ok",
            "status": status,
            "ip": wan.get("wan_ip") or www.get("wan_ip"),
            "isp": wan.get("isp_name") or wan.get("gw_name"),
            "latency": round(_num(www.get("latency") or wan.get("latency"))),
            "down_mbps": round(_num(www.get("xput_down")), 1),   # last speedtest (0 = idle)
            "up_mbps": round(_num(www.get("xput_up")), 1),
            "availability": round(_num(uw.get("availability")), 1),
            "asn": wan.get("asn"),
            "isp_org": wan.get("isp_organization"),
            "speedtest_status": www.get("speedtest_status"),
            "ping": round(_num(www.get("speedtest_ping"))),        # last speedtest ping (ms)
            "lastrun": _num(www.get("speedtest_lastrun")),         # unix ts; grows on a new run
        },
        "throughput": {
            "rx_bps": _num(wan.get("rx_bytes-r")),   # live download rate (WAN in)
            "tx_bps": _num(wan.get("tx_bytes-r")),   # live upload rate   (WAN out)
        },
        "clients": {
            "total": len(clients) or int(_num(wan.get("num_sta")) or _num(lan.get("num_user"))),
            "wired": wired,
            "wireless": len(clients) - wired,
            "guest": sum(1 for c in clients if c.get("is_guest")) or int(_num(lan.get("num_guest"))),
            "iot": int(_num(lan.get("num_iot"))),
        },
        "networks": sorted(({"name": k, "clients": v} for k, v in netcounts.items()),
                           key=lambda x: -x["clients"]),
        "devices": [   # infra beyond the gateway (switches/APs); the gateway has its own tile
            {
                "name": d.get("name") or d.get("model") or d.get("mac"),
                "kind": _DEV_KIND.get(d.get("type"), "device"),
                "up": d.get("state", 1) == 1,
                "clients": d.get("num_sta"),
                "uptime": _fmt_uptime(d.get("uptime")),
            }
            for d in devices if d.get("mac") and d.get("mac") != gw_mac
        ],
    }


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

    def _post(self, path: str, body: dict) -> dict:
        op = self._build_opener()
        if op is None:
            return {}
        url = self.cfg["url"].rstrip("/")
        site = self.cfg.get("site", "default")
        req = urllib.request.Request(f"{url}/proxy/network/api/s/{site}/{path}",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        if self._api_key():
            req.add_header("X-API-KEY", self._api_key())
        try:
            with op.open(req, timeout=20) as r:
                return json.load(r)
        except (urllib.error.URLError, ValueError) as e:
            log.warning("unifi POST %s failed: %s", path, e)
            return {}

    def run_speedtest(self) -> dict:
        """Kick off a WAN speedtest; results land in the next stat/health (~30s)."""
        return self._post("cmd/devmgr", {"cmd": "speedtest"})

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

    def dashboard(self) -> dict:
        """Live gateway summary for the dashboard panel. Uses the same authed
        _get as collect(); empty API lists just yield an empty summary."""
        return health_summary(self._get("stat/health"),
                              self._get("stat/device"), self._get("stat/sta"))


def _probe() -> None:
    """Dump the fields each UniFi endpoint returns for THIS controller — a dev aid
    for mapping API fields to the dashboard. Run: python -m collectors.unifi --probe
    from the repo root (reads the unifi block in config.yaml)."""
    import os
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "config.yaml")
    if not os.path.exists(path):
        print(f"no config.yaml at {path} — run this on the dashboard server where it lives")
        return
    cfg = (yaml.safe_load(open(path, encoding="utf-8")) or {}).get("unifi") or {}
    if not cfg.get("enabled"):
        print("unifi is not enabled in config.yaml (set unifi.enabled: true)")
        return
    c = UnifiCollector(cfg)
    for path in ("stat/health", "stat/sysinfo", "stat/device", "stat/sta",
                 "rest/networkconf", "rest/wlanconf"):
        data = c._get(path) or []
        keys = sorted({k for o in data if isinstance(o, dict) for k in o})
        print(f"\n### {path}  ({len(data)} objects)")
        print(", ".join(keys) or "(no data / endpoint unavailable)")
    print("\n### stat/health FULL (redact public IPs / MACs before sharing) ###")
    print(json.dumps(c._get("stat/health"), indent=2)[:4000])


if __name__ == "__main__":  # ponytail: transform self-check, no live controller
    import sys
    if "--probe" in sys.argv:          # live field dump for THIS controller (needs config.yaml)
        _probe()
        sys.exit()
    nets = [{"name": "IoT", "vlan": 50, "ip_subnet": "10.0.50.1/24", "purpose": "corporate", "enabled": True},
            {"name": "WAN", "purpose": "wan", "enabled": True}]
    z = networks_to_zones(nets, {})
    assert z == [{"vid": 50, "name": "IoT", "subnet": "10.0.50.1/24",
                  "policy": "", "cls": "iot"}], z
    devs = [{"mac": "aa:bb:cc:00:00:00", "name": "gw", "type": "udm",
             "ip": "47.1.2.3", "lan_ip": "10.0.10.1", "model": "UDMPRO",
             "displayable_version": "4.0.6", "uptime": 90000,
             "uplink": {"ip": "47.1.2.3", "gateways": ["47.1.2.1"],
                        "media": "10GE", "up": True, "name": "eth4"}},
            {"mac": "aa:bb:cc:00:00:01", "name": "core-sw", "type": "usw", "ip": "10.0.10.2",
             "uplink": {"uplink_mac": "aa:bb:cc:00:00:00", "uplink_remote_port": 1}}]
    di = devices_to_items(devs, "t")
    gw = di[0]
    assert gw["ip"] == "10.0.10.1", gw                 # LAN ip, not the public WAN ip
    assert gw["meta"]["model"] == "UDMPRO" and gw["meta"]["wan ip"] == "47.1.2.3", gw
    wan = di[1]
    assert wan["nodekind"] == "wan" and wan["ip"] == "47.1.2.3", wan
    assert wan["meta"]["isp gateway"] == "47.1.2.1", wan
    assert di[2]["kind"] == "link" and di[2]["dst"] == "47.1.2.3", di[2]
    assert di[3]["nodekind"] == "switch", di[3]
    assert di[4]["dst"] == "aa:bb:cc:00:00:00" and di[4]["port"] == 1, di[4]
    cl = [{"mac": "de:ad:be:ef:00:01", "ip": "10.0.50.9", "hostname": "cam1", "network": "Default",
           "is_wired": True, "sw_mac": "aa:bb:cc:00:00:01", "sw_port": 7}]
    n = clients_to_nodes(cl, "t")
    assert n[0]["name"] == "cam1" and n[0]["nodekind"] == "host", n
    lk = client_links(cl)
    assert lk[0]["dst"] == "aa:bb:cc:00:00:01" and lk[0]["port"] == "7", lk
    hs = health_summary(
        [{"subsystem": "wan", "status": "ok", "wan_ip": "47.1.2.3", "gw_mac": "aa:bb:cc:00:00:00",
          "gw_name": "UCG", "gw_version": "5.1.19", "isp_name": "Acme ISP", "asn": 5650,
          "isp_organization": "Acme Inc", "uptime_stats": {"WAN": {"availability": 99.9}},
          "gw_system-stats": {"cpu": "22.2", "mem": "72.2", "uptime": "90000"},
          "rx_bytes-r": 1250000, "tx_bytes-r": 250000},
         {"subsystem": "www", "status": "ok", "latency": 8, "xput_down": 930.5, "xput_up": 42.1},
         {"subsystem": "lan", "num_user": 1, "num_guest": 0, "num_iot": 3}],
        devs, cl)
    assert hs["wan"]["availability"] == 99.9 and hs["wan"]["asn"] == 5650, hs
    assert hs["networks"] == [{"name": "Default", "clients": 1}], hs   # cl's one client's network
    assert hs["wan"]["up"] and hs["wan"]["ip"] == "47.1.2.3" and hs["wan"]["isp"] == "Acme ISP", hs
    assert hs["wan"]["down_mbps"] == 930.5 and hs["wan"]["latency"] == 8, hs
    assert hs["throughput"]["rx_bps"] == 1250000, hs
    assert hs["clients"] == {"total": 1, "wired": 1, "wireless": 0, "guest": 0, "iot": 3}, hs
    # gateway CPU/firmware come from the wan subsystem's gw_* fields; model from the matched device
    assert hs["gateway"]["name"] == "UCG" and hs["gateway"]["cpu"] == 22.2, hs
    assert hs["gateway"]["firmware"] == "5.1.19" and hs["gateway"]["model"] == "UDMPRO", hs
    assert hs["gateway"]["uptime"] == "1d 1h", hs
    # gateway (gw_mac) is excluded from the devices list; the switch remains
    assert all(dv["name"] != "gw" for dv in hs["devices"]) and any(dv["kind"] == "switch" for dv in hs["devices"]), hs
    print("unifi transform self-check ok")
