"""Proxmox VE guests as dashboard tiles.

The shaping is pure: a /cluster/resources payload in, tiles out. Per-guest detail
needs extra API calls, so the caller injects a `detail` callable — which is what
makes the whole thing testable against a canned payload instead of a live cluster.

The caller (topo_server) keeps what it already owns: reading the proxmox block from
config.yaml, loading the stored topology to learn the host's name and reporting IP,
and constructing the collector.
"""
from __future__ import annotations


def up_status(secs) -> str:
    """PVE uptime seconds -> docker-style 'Up N unit' so the tile's uptime parser reads it."""
    secs = int(secs or 0)
    for n, word in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if secs >= n:
            v = secs // n
            return f"Up {v} {word}{'s' if v != 1 else ''}"
    return "Up"


def gib(b) -> str:
    """Bytes -> '301.24 MiB' / '16.01 GiB', matching the PVE summary panel."""
    b = float(b or 0)
    for unit, div in (("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if b >= div:
            return f"{b / div:.2f} {unit}"
    return f"{int(b)} B"


def guest_ips(col, node: str, kind: str, vmid) -> list[str]:
    """All non-loopback IPv4s of a running guest (lxc /interfaces, qemu agent). []
    when the container's off or the VM has no guest agent."""
    ips = []
    try:
        if kind == "lxc":
            for i in col._get(f"/nodes/{node}/lxc/{vmid}/interfaces") or []:
                ip = (i.get("inet") or "").split("/")[0]
                if ip and not ip.startswith("127."):
                    ips.append(ip)
        else:
            data = col._get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces") or {}
            for iface in data.get("result", []):
                for a in iface.get("ip-addresses", []):
                    ip = a.get("ip-address", "")
                    if a.get("ip-address-type") == "ipv4" and ip and not ip.startswith("127."):
                        ips.append(ip)
    except Exception:
        return []
    seen = set()
    return [x for x in ips if not (x in seen or seen.add(x))]   # dedupe, keep order


def guest_detail(col, g: dict) -> list[list]:
    """PVE summary rows for a guest tile, ordered like the PVE panel. Cheap fields
    come from /cluster/resources; SWAP / Unprivileged / IPs need a per-guest call
    (running guests only), each best-effort."""
    kind, node, vmid = g["type"], g.get("node"), g.get("vmid")
    running = g.get("status") == "running"
    cpu, maxcpu = (g.get("cpu") or 0) * 100, int(g.get("maxcpu") or 0)
    mem, maxmem = g.get("mem") or 0, g.get("maxmem") or 0
    disk, maxdisk = g.get("disk") or 0, g.get("maxdisk") or 0
    unpriv, swap_row, ips = None, None, []
    if running:
        if kind == "lxc":
            cur = col._get(f"/nodes/{node}/lxc/{vmid}/status/current") or {}
            mxs = cur.get("maxswap") or 0
            if mxs:
                swap_row = ["SWAP usage", f"{(cur.get('swap') or 0) / mxs * 100:.2f}% "
                                          f"({gib(cur.get('swap'))} of {gib(mxs)})"]
            cf = col._get(f"/nodes/{node}/lxc/{vmid}/config") or {}
            if "unprivileged" in cf:
                unpriv = "Yes" if cf.get("unprivileged") else "No"
        ips = guest_ips(col, node, kind, vmid)
    rows = [["Status", g.get("status") or "unknown"],
            ["HA State", g.get("hastate") or "none"],
            ["Node", node or "—"]]
    if unpriv is not None:
        rows.append(["Unprivileged", unpriv])
    if maxcpu:
        rows.append(["CPU usage", f"{cpu:.2f}% of {maxcpu} CPU{'s' if maxcpu != 1 else ''}"])
    if maxmem:
        rows.append(["Memory usage", f"{mem / maxmem * 100:.2f}% ({gib(mem)} of {gib(maxmem)})"])
    if swap_row:
        rows.append(swap_row)
    if disk and maxdisk:                                    # qemu often reports disk=0 (can't see inside) -> skip
        rows.append(["Bootdisk size", f"{disk / maxdisk * 100:.2f}% ({gib(disk)} of {gib(maxdisk)})"])
    if ips:
        rows.append(["IPs", ", ".join(ips[:6])])
    return rows


def tiles(resources: list, name_key: str, is_endpoint: bool, detail=None) -> list[dict]:
    """Guest tiles for one host. A host claims guests when a cluster node matches
    its hostname; the machine that IS the PVE API endpoint gets the whole cluster.
    Neither -> [] (this host simply isn't a PVE box)."""
    matched = next((r.get("node") for r in resources
                    if r.get("type") == "node" and (r.get("node") or "").lower() == name_key), None)
    if matched:
        want = lambda g: g.get("node") == matched
    elif is_endpoint:
        want = lambda g: True
    else:
        return []
    out = []
    for g in resources:
        if g.get("type") not in ("qemu", "lxc") or not want(g):
            continue
        running, vmid, maxmem = g.get("status") == "running", g.get("vmid"), g.get("maxmem") or 0
        out.append({
            "name": g.get("name") or f"{g['type']}{vmid}",
            "image": ("VM" if g["type"] == "qemu" else "LXC") + (f" · {vmid}" if vmid else ""),
            "state": "running" if running else "stopped",
            "status": up_status(g.get("uptime")) if running else (g.get("status") or "stopped"),
            "project": g.get("node"),                                  # groups under the node
            "cpu": f"{(g.get('cpu') or 0) * 100:.1f}%" if running else "",
            "memp": f"{(g.get('mem') or 0) / maxmem * 100:.1f}%" if running and maxmem else "",
            "detail": detail(g) if detail else [],
            "engine": "proxmox",
        })
    out.sort(key=lambda c: (c["state"] != "running", c["project"] or "", c["name"].lower()))
    return out


if __name__ == "__main__":   # ponytail: tile shaping against a canned payload, offline
    assert up_status(0) == "Up" and up_status(59) == "Up 59 seconds"
    assert up_status(90000) == "Up 1 day" and up_status(7200) == "Up 2 hours"
    assert gib(0) == "0 B" and gib(2**30 * 16 + 10**7) == "16.01 GiB"
    assert gib(315621376) == "301.00 MiB" or gib(315621376).endswith("MiB")

    RES = [
        {"type": "node", "node": "pve1"},
        {"type": "node", "node": "pve2"},
        {"type": "qemu", "node": "pve1", "vmid": 100, "name": "win11", "status": "running",
         "cpu": 0.25, "mem": 2 * 2**30, "maxmem": 8 * 2**30, "uptime": 3600},
        {"type": "lxc", "node": "pve1", "vmid": 201, "name": "adguard", "status": "stopped"},
        {"type": "lxc", "node": "pve2", "vmid": 202, "name": "elsewhere", "status": "running"},
        {"type": "storage", "node": "pve1"},                       # never a tile
    ]
    t = tiles(RES, "pve1", False)
    assert [x["name"] for x in t] == ["win11", "adguard"], t        # only pve1, running first
    assert t[0]["image"] == "VM · 100" and t[0]["cpu"] == "25.0%" and t[0]["memp"] == "25.0%"
    assert t[0]["status"] == "Up 1 hour" and t[1]["status"] == "stopped"
    assert t[1]["cpu"] == "" and t[1]["detail"] == []               # stopped: no live figures

    whole = tiles(RES, "", True)                                    # the endpoint sees the cluster
    assert sorted(x["name"] for x in whole) == ["adguard", "elsewhere", "win11"], whole
    assert tiles(RES, "not-a-node", False) == []                    # not a PVE box
    assert tiles([], "pve1", True) == []

    seen = []
    tiles(RES, "pve1", False, detail=lambda g: seen.append(g["vmid"]) or [["Status", "x"]])
    assert seen == [100, 201], seen                                 # detail called per tile
    print("renderers/html/pve_tiles self-check ok")
