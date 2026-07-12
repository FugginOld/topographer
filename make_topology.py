#!/usr/bin/env python3
"""Topology generator entrypoint.

    python make_topology.py --config config.yaml

Pipeline: run enabled collectors (read-only) -> dump raw -> normalize into a
canonical Topology -> enrich (vendor/kind/aging) -> render (json/mermaid/svg).
Every stage degrades gracefully; a down source yields a partial map, not a crash.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from core.normalize import normalize
from core.enrich import enrich
from renderers import mermaid, static_svg

# collector registry: name -> (module path, class)
from collectors.arpscan import ArpScanCollector
from collectors.pingsweep import PingSweepCollector
from collectors.opnsense import OPNsenseCollector
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
    "tailscale": TailscaleCollector,
    "docker": DockerCollector,
    "dns": DnsCollector,
}

log = logging.getLogger("make_topology")


def load_config(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML required: pip install -r requirements.txt")
    if not os.path.exists(path):
        sys.exit(f"config not found: {path} (copy config.example.yaml)")
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


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

    raw_items: list[dict] = []
    zones_raw: list[dict] = []

    for name, cls in COLLECTORS.items():
        c = cls(cfg.get(name, {}))
        if not c.enabled:
            log.info("skip %s (disabled)", name)
            continue
        log.info("collect %s", name)
        try:
            items = c.collect()
        except Exception as e:  # last-resort guard
            log.error("%s crashed: %s", name, e)
            items = []
        c.dump_raw(items, args.outdir)
        raw_items.extend(items)
        zones_raw.extend(c.zones())

    # zones defined in config override/augment discovered ones
    for z in cfg.get("static_zones", []):
        zones_raw.append(z)

    topo = normalize(raw_items, _dedupe_zones(zones_raw))
    topo = enrich(topo, cfg.get("offline_after_minutes", 30))

    # renders
    json_path = os.path.join(args.outdir, "topology.json")
    topo.dump(json_path)
    mermaid.write(topo, os.path.join(args.outdir, "topology.mmd"))
    static_svg.write(
        topo,
        os.path.join(args.outdir, "topology.dot"),
        os.path.join(args.outdir, "topology.svg"),
    )
    log.info("wrote %s (%d nodes, %d links, %d zones)",
             json_path, len(topo.nodes), len(topo.links), len(topo.zones))
    print(f"\n  topology.json ready -> serve it:  python renderers/html/serve.py")


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
