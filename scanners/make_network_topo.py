#!/usr/bin/env python3
"""Topology generator entrypoint.

    python make_network_topo.py --config config.yaml

Pipeline: run enabled collectors (read-only) -> dump raw -> normalize into a
canonical Topology -> enrich (vendor/kind/aging) -> topo.json for the dashboard.
Every stage degrades gracefully; a down source yields a partial map, not a crash.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from core.normalize import normalize
from core.enrich import enrich
from core.schema import now_iso

# collector registry: name -> (module path, class)
from collectors.arpscan import ArpScanCollector
from collectors.pingsweep import PingSweepCollector
from collectors.opnsense import OPNsenseCollector
from collectors.proxmox import ProxmoxCollector
from collectors.unifi import UnifiCollector
from collectors.unifi_snmp import UnifiSnmpCollector
from collectors.tailscale import TailscaleCollector
from collectors.docker import DockerCollector
from collectors.dns import DnsCollector

COLLECTORS = {
    "opnsense": OPNsenseCollector,     # zones + names first
    "unifi": UnifiCollector,           # UniFi gateway: VLANs + client/uplink map
    "arpscan": ArpScanCollector,
    "pingsweep": PingSweepCollector,   # no-deps discovery (Windows-friendly)
    "unifi_snmp": UnifiSnmpCollector,
    "proxmox": ProxmoxCollector,       # PVE API: VMs + LXC the network can't see
    "tailscale": TailscaleCollector,
    "docker": DockerCollector,
    "dns": DnsCollector,
}

log = logging.getLogger("make_network_topology")


def load_config(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML required: pip install -r requirements.txt")
    if not os.path.exists(path):
        sys.exit(f"config not found: {path} (copy config.example.yaml)")
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def run_collectors(cfg: dict, collectors: dict, outdir: str | None = None):
    """Run every enabled collector -> (raw items, raw zones).

    Where the Collector contract is actually enforced, rather than described:
      - a source that fails contributes nothing and never stops the scan — and
        that covers zones() too, which used to run outside the guard, so one
        misconfigured collector (a missing api key) killed the whole run;
      - source and last_seen are stamped here, so a collector that forgets
        _tag() can't land nodes as source="unknown" with no age.
    """
    raw_items: list[dict] = []
    zones_raw: list[dict] = []
    ts = now_iso()
    for name, cls in collectors.items():
        try:
            c = cls(cfg.get(name, {}))
            if not c.enabled:
                log.info("skip %s (disabled)", name)
                continue
        except Exception as e:
            log.error("%s could not start: %s", name, e)
            continue
        log.info("collect %s", name)
        try:
            items = c.collect() or []
        except Exception as e:  # last-resort guard
            log.error("%s crashed: %s", name, e)
            items = []
        for it in items:
            if isinstance(it, dict):
                it.setdefault("source", name)
                if it.get("kind") != "link":
                    it.setdefault("last_seen", ts)
        if outdir:
            c.dump_raw(items, outdir)
        raw_items.extend(items)
        try:
            zones_raw.extend(c.zones() or [])
        except Exception as e:      # a source can define zones badly without killing the scan
            log.error("%s zones failed: %s", name, e)
    return raw_items, zones_raw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    os.makedirs(args.outdir, exist_ok=True)

    raw_items, zones_raw = run_collectors(cfg, COLLECTORS, args.outdir)

    # zones defined in config override/augment discovered ones
    for z in cfg.get("static_zones", []):
        zones_raw.append(z)

    topo = normalize(raw_items, _dedupe_zones(zones_raw))
    topo = enrich(topo, cfg.get("offline_after_minutes", 30))

    json_path = os.path.join(args.outdir, "topo.json")
    topo.dump(json_path)
    log.info("wrote %s (%d nodes, %d links, %d zones)",
             json_path, len(topo.nodes), len(topo.links), len(topo.zones))
    print(f"\n  topo.json ready -> serve it:  python renderers/html/topo_server.py")


def _dedupe_zones(zones: list[dict]) -> list[dict]:
    seen, out = set(), []
    for z in zones:
        vid = z.get("vid")
        if vid in seen:
            continue
        seen.add(vid)
        out.append(z)
    return out


if __name__ == "__main__":
    main()
