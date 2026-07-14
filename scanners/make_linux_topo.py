#!/usr/bin/env python3
"""Linux counterpart of make_pc_topo.py — same fabric schema, different sources.

Reads the real hardware topology from sysfs / proc / lspci / lsblk (no Windows,
no PowerShell). The output is byte-for-byte the same shape the dashboard renders:

    {name, generated, nodes:[{id,label,sub,cls,parent,cap,kind,grp,up?,link?,fill?}]}

Sources (all readable without root except RAM detail):
    /proc/cpuinfo                     CPU model + cores            (root complex)
    /sys/bus/pci/devices/*            PCIe fabric + real parent tree
      current_link_width/_speed       negotiated  x{w} G{gen}  labels
    lspci -Dvmm                       device vendor/product names
    /sys/class/net/*                  NIC link state (up/down) + speed
    lsblk -b -J                       disks, transport, and fill (used/total)
    /sys/class/drm/card*-*/status     connected displays
    dmidecode -t memory               per-DIMM detail (needs root; falls back to
                                      /proc/meminfo total otherwise)

    python make_linux_topo.py [--out out/topo_pc.json] [--name "my server"]
    python make_linux_topo.py --selftest      # pure-logic checks, any OS

# ponytail: untested on real hardware from this Windows box — the sysfs/lspci
# interfaces are stable and well-documented, and --selftest covers the parsing.
# Run it on the target Linux server to validate against actual devices.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone

DDR = {"DDR5": "DDR5", "DDR4": "DDR4", "DDR3": "DDR3", "LPDDR5": "LPDDR5", "LPDDR4": "LPDDR4"}

PCI_CLASS = {"06": "bridge", "03": "display", "02": "network", "01": "storage",
             "0c": "serial bus", "04": "multimedia", "0d": "wireless", "08": "system"}


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def clean(name: str) -> str:
    name = re.sub(r"\s*\(rev [0-9a-fx]+\)\s*$", "", name)
    for junk in ("Corporation", " Inc.", " Co., Ltd.", "(R)", "(TM)", " Semiconductor"):
        name = name.replace(junk, "")
    return re.sub(r"\s+", " ", name).strip()


def gts_to_gen(speed: str) -> int | None:
    """'8.0 GT/s PCIe' -> 3 ; '16.0 GT/s' -> 4 ; '2.5 GT/s' -> 1."""
    m = re.search(r"([\d.]+)\s*GT/s", speed or "")
    if not m:
        return None
    return {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}.get(float(m.group(1)))


def pcie(bdf: str) -> tuple[str | None, str | None]:
    """(link label, cls) from sysfs negotiated width + speed."""
    w = read(f"/sys/bus/pci/devices/{bdf}/current_link_width")
    gen = gts_to_gen(read(f"/sys/bus/pci/devices/{bdf}/current_link_speed"))
    if not w or w == "0" or gen is None:
        return None, None
    cls = "gen5" if gen >= 5 else "gen4" if gen == 4 else "gen3"
    return f"x{w} G{gen}", cls


def pci_cat(class_hex: str) -> dict:
    """Map a PCI class code to {grp, cap, hub, struct}. class_hex like '0x030000'."""
    c = class_hex[2:] if class_hex.startswith("0x") else class_hex
    base, sub = c[:2], c[2:4]
    if base == "06":
        if sub == "00":
            return {"root": True}          # host bridge
        return {"hub": True}               # PCI/PCIe bridge, root port
    if base == "03":
        return {"grp": "gpu", "cap": 8}
    if base == "02":
        return {"grp": "net", "cap": (2.4 if sub == "80" else 1)}   # 0280 = wireless
    if base == "01":
        return {"struct": True}            # storage controller — the disk carries load
    if base == "0c" and sub == "03":
        return {"grp": "usb", "cap": 10}
    return {"struct": True}                 # audio, SMBus, ISA, etc.


def pci_meta(bdf: str, extra: dict, vendor: str | None = None) -> dict:
    """Real per-device identity from sysfs: vendor, device id, revision, class.
    Same shape as the Windows generator's meta — non-generic, straight from silicon."""
    m = {}
    ven = read(f"/sys/bus/pci/devices/{bdf}/vendor").lower().removeprefix("0x")
    dev = read(f"/sys/bus/pci/devices/{bdf}/device").lower().removeprefix("0x")
    rev = read(f"/sys/bus/pci/devices/{bdf}/revision").lower().removeprefix("0x")
    cls = read(f"/sys/bus/pci/devices/{bdf}/class").lower().removeprefix("0x")
    if vendor:
        m["vendor"] = vendor          # lspci-derived name (self-contained; no cross-scanner table)
    elif ven:
        m["vendor"] = "0x" + ven      # fallback: raw sysfs vendor id when lspci is absent
    if dev:
        m["device id"] = "0x" + dev
    if rev and rev not in ("00", ""):
        m["revision"] = "0x" + rev
    if cls[:2] in PCI_CLASS:
        m["class"] = PCI_CLASS[cls[:2]]
    m.update(extra)
    return m


def pci_parent(bdf: str) -> str | None:
    """Upstream PCI device BDF from the sysfs device path, or None if at the root."""
    real = os.path.realpath(f"/sys/bus/pci/devices/{bdf}")
    chain = [p for p in real.split("/") if re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:", p)]
    if len(chain) >= 2 and chain[-1] == bdf:
        return chain[-2]
    return None


def lspci_info() -> tuple[dict, dict]:
    """(labels, vendors) per BDF from lspci -Dvmm (machine-readable, no root).
    The vendor name comes straight from lspci — no cross-scanner vendor table."""
    out, cur, names, vendors = run(["lspci", "-Dvmm"]), {}, {}, {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            slot = cur.get("Slot")
            if slot:
                label = (cur.get("Vendor", "") + " " + cur.get("Device", "")).strip()
                names[slot] = clean(label) or slot
                if cur.get("Vendor"):
                    vendors[slot] = clean(cur["Vendor"])
            cur = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            cur[k.strip()] = v.strip()
    return names, vendors


def cpu_info() -> tuple[str, int]:
    model, cores = "CPU", 0
    ids = set()
    for line in read("/proc/cpuinfo").splitlines():
        if line.startswith("model name") and model == "CPU":
            model = clean(line.split(":", 1)[1])
        if line.startswith("core id"):
            ids.add(line.split(":", 1)[1].strip())
    return model, (len(ids) or os.cpu_count() or 0)


def net_links() -> dict:
    """{bdf: {'up':bool,'speed':Mbps,'iface':name}} for physical NICs."""
    out = {}
    for iface in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
        base = f"/sys/class/net/{iface}"
        real = os.path.realpath(base + "/device")
        m = re.search(r"([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])", real)
        if not m:
            continue
        up = read(base + "/carrier") == "1" or read(base + "/operstate") == "up"
        spd = read(base + "/speed")
        out[m.group(1)] = {"up": up, "speed": int(spd) if spd.lstrip("-").isdigit() else 0, "iface": iface}
    return out


def disks() -> list[dict]:
    """Physical disks with transport, size, fill fraction, and controller BDF."""
    try:
        data = json.loads(run(["lsblk", "-b", "-J", "-o",
                               "NAME,TYPE,SIZE,MODEL,TRAN,FSSIZE,FSAVAIL"]) or "{}")
    except json.JSONDecodeError:
        return []
    out = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        tot = used = 0
        for part in [d] + d.get("children", []):
            fs, av = part.get("fssize"), part.get("fsavail")
            if fs:
                tot += int(fs)
                used += int(fs) - int(av or 0)
        real = os.path.realpath(f"/sys/block/{d['name']}/device")
        m = re.search(r"([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])", real)
        out.append({
            "name": d.get("model") or d["name"],
            "tran": (d.get("tran") or "disk").upper(),
            "gb": round(int(d.get("size", 0)) / 1e9),
            "fill": round(used / tot, 4) if tot else None,
            "used_gb": round(used / 1e9), "tot_gb": round(tot / 1e9),
            "ctrl_bdf": m.group(1) if m else None,
        })
    return out


def dimms() -> list[dict]:
    """Per-DIMM detail from dmidecode (root), else a single /proc/meminfo total."""
    out = run(["dmidecode", "-t", "memory"])
    rows, cur = [], {}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Memory Device"):
            if cur.get("Size") and "No Module" not in cur.get("Size", ""):
                rows.append(cur)
            cur = {}
        elif ":" in s:
            k, v = s.split(":", 1)
            cur[k.strip()] = v.strip()
    if cur.get("Size") and "No Module" not in cur.get("Size", ""):
        rows.append(cur)
    if rows:
        dimms = []
        for r in rows:
            typ = DDR.get(r.get("Type", ""), r.get("Type", "RAM"))
            spd = re.sub(r"\s*MT/s|\s*MHz", "", r.get("Configured Memory Speed", r.get("Speed", "")))
            dimms.append({"size": r.get("Size", ""), "type": typ,
                          "speed": spd.strip(), "locator": r.get("Locator", "DIMM")})
        return dimms
    # fallback: total only
    for line in read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal"):
            gb = round(int(line.split()[1]) / 1048576)
            return [{"size": f"{gb} GB", "type": "RAM", "speed": "", "locator": "system"}]
    return []


def displays() -> list[str]:
    out = []
    for st in glob.glob("/sys/class/drm/card*-*/status"):
        if read(st) == "connected":
            out.append(os.path.basename(os.path.dirname(st)).split("-", 1)[-1])   # e.g. HDMI-A-1
    return out


# ── universal sources (work on any Linux — the payoff is on boards with no PCIe
#    or DMI, e.g. a Raspberry Pi, where the fabric/dmidecode paths find nothing) ──

def _parse_board(cpuinfo: str, dt_model: str, arch: str) -> dict:
    """Board/SoC identity from /proc/cpuinfo + device-tree. x86 fills 'model';
    ARM boards (Pi) fill 'board' (device-tree) + 'soc'/'revision'/'serial'."""
    info: dict = {}
    for line in cpuinfo.splitlines():
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        kl = k.lower()
        if kl == "model name" and "model" not in info:
            info["model"] = clean(v)
        elif kl == "hardware":
            info["soc"] = v
        elif kl == "revision" and v:
            info["revision"] = v
        elif kl == "serial" and v.strip("0 "):
            info["serial"] = v
    if dt_model:
        info["board"] = dt_model
    if arch:
        info["arch"] = arch
    return info


def board_info() -> dict:
    dt = read("/proc/device-tree/model").replace("\x00", "").strip()
    return _parse_board(read("/proc/cpuinfo"), dt, platform.machine())


def _pick_temp(zones: list[tuple[str, str]]) -> float | None:
    """First CPU/SoC thermal zone (°C), else the first readable zone."""
    best = None
    for typ, milli in zones:
        if not milli.lstrip("-").isdigit():
            continue
        c = int(milli) / 1000.0
        if any(k in typ.lower() for k in ("cpu", "soc", "x86_pkg")):
            return c
        if best is None:
            best = c
    return best


def cpu_temp() -> float | None:
    zones = [(read(z + "/type"), read(z + "/temp"))
             for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*"))]
    return _pick_temp(zones)


def net_ifaces() -> list[dict]:
    """Every physical network interface (not just PCI-backed ones). This is what
    surfaces a Pi's USB ethernet + SDIO wifi, which the PCI path never sees."""
    base = "/sys/class/net"
    out = []
    for iface in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        p = f"{base}/{iface}"
        if iface == "lo" or read(p + "/type") == "772":        # skip loopback
            continue
        has_dev = os.path.exists(p + "/device")
        real = os.path.realpath(p + "/device") if has_dev else ""
        bdf = re.search(r"([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])", real)
        spd = read(p + "/speed")
        drv = os.path.realpath(p + "/device/driver") if has_dev else ""
        out.append({
            "iface": iface,
            "mac": read(p + "/address"),
            "up": read(p + "/carrier") == "1" or read(p + "/operstate") == "up",
            "speed": int(spd) if spd.lstrip("-").isdigit() else 0,
            "mtu": read(p + "/mtu"),
            "driver": os.path.basename(drv) if drv else "",
            "wireless": os.path.exists(p + "/wireless") or os.path.exists(p + "/phy80211"),
            "bdf": bdf.group(1) if bdf else None,
            "virtual": not has_dev,        # docker0/veth/bridges have no device link
        })
    return out


def usb_devices() -> list[dict]:
    """Named USB devices (skips hubs + interface nodes) from sysfs."""
    base = "/sys/bus/usb/devices"
    out = []
    for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        p = f"{base}/{d}"
        idv, idp = read(p + "/idVendor"), read(p + "/idProduct")
        if not idv or read(p + "/bDeviceClass") == "09":       # no vid, or a hub
            continue
        product, manuf = read(p + "/product"), read(p + "/manufacturer")
        if not (product or manuf):                             # unnamed root hubs
            continue
        out.append({"product": product or f"{idv}:{idp}", "manuf": manuf,
                    "vid_pid": f"{idv}:{idp}", "speed": read(p + "/speed")})
    return out


def build() -> list[dict]:
    names, pci_vendors = lspci_info()
    nets = net_links()
    devs = sorted(glob.glob("/sys/bus/pci/devices/*"))
    bdfs = [os.path.basename(d) for d in devs]
    kept = set(bdfs)

    idmap, n = {}, 0

    def nid(key):
        nonlocal n
        if key not in idmap:
            idmap[key] = f"n{n}"; n += 1
        return idmap[key]

    cpu_name, cores = cpu_info()
    binfo = board_info()
    # x86 has a cpuinfo "model name"; an ARM board (Pi) gets its device-tree name
    label = cpu_name if cpu_name != "CPU" else (binfo.get("board") or binfo.get("model") or "CPU")
    temp = cpu_temp()
    root_meta = {k: v for k, v in binfo.items()}
    root_meta["cores"] = str(cores)
    if temp is not None:
        root_meta["temp"] = f"{temp:.0f}°C"
    sub = f"{cores} cores" + (f" · {binfo['arch']}" if binfo.get("arch") else "") \
        + (f" · {temp:.0f}°C" if temp is not None else "")
    root_bdf = next((b for b in bdfs if b.endswith(":00:00.0")), None)
    root_key = root_bdf or "__root__"
    nodes = {root_key: {"id": nid(root_key), "label": label, "kind": "cpu",
                        "cls": "gen5", "parent": None, "sub": sub, "meta": root_meta}}

    for bdf in bdfs:
        if bdf == root_bdf:
            continue
        cls_hex = read(f"/sys/bus/pci/devices/{bdf}/class")
        cat = pci_cat(cls_hex)
        label = names.get(bdf, bdf)
        blabel, bcls = pcie(bdf)
        node = {"id": nid(bdf), "label": label, "sub": bdf, "parent": None}
        if cat.get("hub"):
            node.update(kind="hub", cls=(bcls or "gen3"), cap=0)
        else:
            node.update(kind="leaf", cls=(bcls or "gen3"),
                        cap=(0 if cat.get("struct") else cat.get("cap", 0)))
            if not cat.get("struct") and cat.get("grp"):
                node["grp"] = cat["grp"]
            if blabel:
                node["link"] = blabel
            if bdf in nets:
                node["up"] = nets[bdf]["up"]
                node["iface"] = nets[bdf]["iface"]     # lets telemetry map bytes/sec to this card
                spd = nets[bdf]["speed"]
                node["sub"] += " · " + (f"{spd/1000:g}GbE" if spd >= 1000 else f"{spd}Mb" if spd > 0 else "")
                node["sub"] += (" · link up" if nets[bdf]["up"] else " · no link")
                if spd > 0:
                    node["cap"] = spd / 1000
        node["meta"] = pci_meta(bdf, {}, pci_vendors.get(bdf))
        p = pci_parent(bdf)
        node["_p"] = p if p in kept else root_key
        nodes[bdf] = node

    for key, node in nodes.items():
        if key != root_key:
            node["parent"] = nodes[node.pop("_p")]["id"]

    out = list(nodes.values())

    # prune empty bridges (root ports with nothing downstream)
    changed = True
    while changed:
        parents = {x["parent"] for x in out}
        before = len(out)
        out = [x for x in out if not (x["kind"] == "hub" and x["id"] not in parents)]
        changed = len(out) < before

    root_id = nodes[root_key]["id"]

    def add(node):
        nonlocal n
        node["id"] = f"n{n}"; n += 1
        out.append(node)
        return node["id"]

    # disks under their controller node (via BDF), else root
    ctrl_by_bdf = {b: nodes[b]["id"] for b in kept if b in nodes}
    for d in disks():
        parent = ctrl_by_bdf.get(d["ctrl_bdf"], root_id)
        node = {"label": clean(d["name"]), "sub": f"{d['tran']} · {d['gb']}GB",
                "cls": "gen4", "parent": parent, "kind": "leaf", "cap": 7, "grp": "disk",
                "meta": {"bus type": d["tran"], "capacity": f"{d['gb']} GB"}}
        if d["fill"] is not None:
            node["fill"] = d["fill"]
            node["sub"] = f"{d['used_gb']}/{d['tot_gb']}GB used"
            node["meta"]["used"] = f"{d['used_gb']}/{d['tot_gb']} GB ({round(d['fill']*100)}%)"
        add(node)

    # RAM under a synthesized IMC
    dm = dimms()
    if dm:
        imc = add({"label": "Integrated Memory Controller", "sub": "IMC",
                   "cls": "gen5", "parent": root_id, "kind": "hub", "cap": 0})
        for d in dm:
            lbl = f"{d['size']} {d['type']}" + (f"-{d['speed']}" if d["speed"] else "")
            rammeta = {"type": d["type"], "size": d["size"], "slot": d["locator"]}
            if d["speed"]:
                rammeta["speed"] = d["speed"] + " MT/s"
            add({"label": lbl.strip(), "sub": d["locator"], "cls": "gen5",
                 "parent": imc, "kind": "leaf", "cap": 38, "grp": "mem", "link": d["type"],
                 "meta": rammeta})

    # displays under the GPU node
    gpu_id = next((x["id"] for x in out if x.get("grp") == "gpu"), root_id)
    for conn in displays():
        add({"label": conn, "sub": "display", "cls": "gen4", "parent": gpu_id,
             "kind": "leaf", "cap": 6, "grp": "display", "link": "video"})

    # network interfaces the PCI path missed (Pi USB-ethernet + SDIO wifi, etc.)
    for nif in net_ifaces():
        if nif["virtual"] or (nif["bdf"] and nif["bdf"] in kept):
            continue
        spd = nif["speed"]
        parts = []
        if spd > 0:
            parts.append(f"{spd / 1000:g}GbE" if spd >= 1000 else f"{spd}Mb")
        parts.append("link up" if nif["up"] else "no link")
        meta = {"interface": nif["iface"]}
        for k in ("mac", "driver", "mtu"):
            if nif[k]:
                meta[k] = nif[k]
        add({"label": nif["iface"] + (" · wifi" if nif["wireless"] else ""),
             "sub": " · ".join(parts), "cls": "gen4", "parent": root_id, "kind": "leaf",
             "grp": "net", "cap": (spd / 1000 if spd > 0 else 1), "up": nif["up"],
             "iface": nif["iface"], "link": ("wifi" if nif["wireless"] else "eth"), "meta": meta})

    # USB devices under a USB hub node
    usbs = usb_devices()
    if usbs:
        hub = add({"label": "USB", "sub": f"{len(usbs)} device{'s' if len(usbs) != 1 else ''}",
                   "cls": "gen3", "parent": root_id, "kind": "hub", "cap": 0})
        for u in usbs:
            spd = u["speed"]
            umeta = {"vid:pid": u["vid_pid"]}
            if u["manuf"]:
                umeta["vendor"] = u["manuf"]
            if spd:
                umeta["speed"] = f"{spd} Mb/s"
            add({"label": clean(u["product"]), "parent": hub, "kind": "leaf", "grp": "usb",
                 "cls": "gen3", "sub": (u["manuf"] or "USB") + (f" · {spd}Mb" if spd else ""),
                 "meta": umeta})

    # ponytail: emits the Card contract (renderers/card.py) inline, NOT via import.
    # topology_server.scan_host pipes THIS FILE ALONE over SSH to hosts without the
    # repo, so it must stay single-file self-contained. Keep it in sync with Card.
    return out


def _selftest() -> None:
    assert gts_to_gen("8.0 GT/s PCIe") == 3
    assert gts_to_gen("16.0 GT/s") == 4
    assert gts_to_gen("2.5 GT/s PCIe") == 1
    assert gts_to_gen("bogus") is None
    assert pci_cat("0x060000") == {"root": True}
    assert pci_cat("0x060400") == {"hub": True}
    assert pci_cat("0x030000")["grp"] == "gpu"
    assert pci_cat("0x028000")["cap"] == 2.4          # wireless
    assert pci_cat("0x020000")["grp"] == "net"
    assert pci_cat("0x010802").get("struct") is True   # nvme controller
    assert pci_cat("0x0c0330")["grp"] == "usb"
    assert clean("Intel Corporation Ethernet Controller I226-V (rev 04)") == "Intel Ethernet Controller I226-V"
    # lspci block parse
    import io
    sample = "Slot:\t0000:00:1f.6\nClass:\tEthernet controller\nVendor:\tIntel Corporation\nDevice:\tI219-V\n\n"
    g = globals(); g["run"] = lambda c: sample
    labels, vendors = lspci_info()
    assert labels["0000:00:1f.6"] == "Intel I219-V"
    assert vendors["0000:00:1f.6"] == "Intel"          # vendor comes straight from lspci
    # pci_meta parses sysfs vendor/device/revision/class files
    sysfs = {"vendor": "0x8086", "device": "0x9a09", "revision": "0x02", "class": "0x030000"}
    g["read"] = lambda p: sysfs.get(p.rsplit("/", 1)[-1], "")
    mm = pci_meta("0000:00:02.0", {"bus type": "NVMe"})
    assert mm["device id"] == "0x9a09" and mm["class"] == "display" and mm["bus type"] == "NVMe"
    # board identity: a Raspberry Pi cpuinfo has no "model name" — device-tree names it
    pi_cpuinfo = ("processor\t: 0\nHardware\t: BCM2835\n"
                  "Revision\t: a02082\nSerial\t\t: 00000000abcd1234\n")
    b = _parse_board(pi_cpuinfo, "Raspberry Pi 3 Model B Rev 1.2", "aarch64")
    assert b["board"] == "Raspberry Pi 3 Model B Rev 1.2" and b["soc"] == "BCM2835", b
    assert b["serial"] == "00000000abcd1234" and b["arch"] == "aarch64" and "model" not in b, b
    x86 = _parse_board("model name\t: Intel Core i7\n", "", "x86_64")
    assert x86["model"] == "Intel Core i7", x86
    # thermal picks a cpu/soc zone over a generic one
    assert _pick_temp([("acpitz", "40000"), ("x86_pkg_temp", "55000")]) == 55.0
    assert _pick_temp([("iwlwifi", "48000")]) == 48.0
    assert _pick_temp([("t", "bogus")]) is None
    print("selftest OK")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("out", "topo_pc.json"))
    ap.add_argument("--name", default="linux server")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--stdout", action="store_true",
                    help="write only JSON to stdout (for piping over SSH)")
    args = ap.parse_args()
    if args.selftest:
        _selftest(); return
    if not sys.platform.startswith("linux"):
        sys.exit("make_linux_topo.py reads Linux sysfs/proc — run it on the Linux host "
                 "(use make_pc_topo.py on Windows).")

    nodes = build()
    topo = {"name": args.name,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "nodes": nodes}
    if args.stdout:                       # clean JSON only — nothing else on stdout
        json.dump(topo, sys.stdout)
        return
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(topo, fh, indent=2)
    print(f"wrote {args.out}  ({len(nodes)} modules)")


if __name__ == "__main__":
    main()
