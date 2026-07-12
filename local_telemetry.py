#!/usr/bin/env python3
"""Live hardware metrics for the local machine — shared by topology_server.py (local HUD)
and topology_agent.py (per-host push). One call = one point-in-time sample.

    sample() -> {cpu, cpu_temp, net_gbps, disk_mbps, nics:{name: gbps}}

Windows reads Get-Counter / CIM (no CPU temp without admin). Linux reads /proc
+ /sys/class/hwmon and gets real CPU temperature.
"""
from __future__ import annotations

import glob
import re
import subprocess
import sys
import time

ZERO = {"cpu": 0, "cpu_temp": 0, "net_gbps": 0, "disk_mbps": 0, "nics": {}}

_TELE_PS = r"""
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
Write-Output ('CPU|' + [math]::Round($cpu,0))
$s = (Get-Counter -Counter '\Network Interface(*)\Bytes Total/sec','\PhysicalDisk(_Total)\Disk Bytes/sec' -ErrorAction SilentlyContinue).CounterSamples
foreach ($x in $s) {
  if ($x.Path -like '*physicaldisk*') { Write-Output ('DISK|' + [math]::Round($x.CookedValue,0)) }
  else { Write-Output ('NIC|' + $x.InstanceName + '|' + [math]::Round($x.CookedValue,0)) }
}
"""


def _win() -> dict:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", _TELE_PS],
                       capture_output=True, text=True)
    cpu, disk_bytes, nics = 0, 0, {}
    for ln in r.stdout.splitlines():
        p = ln.strip().split("|")
        if p[0] == "CPU":
            cpu = int(float(p[1] or 0))
        elif p[0] == "DISK":
            disk_bytes = float(p[1] or 0)
        elif p[0] == "NIC" and len(p) >= 3:
            nics[p[1]] = float(p[2] or 0) * 8 / 1e9   # Gb/s per adapter
    return {"cpu": cpu, "cpu_temp": 0,   # Windows won't give CPU temp without admin/driver
            "net_gbps": round(sum(nics.values()), 2),
            "disk_mbps": round(disk_bytes / 1048576, 1),
            "nics": {k: round(v, 3) for k, v in nics.items()}}


def _rd(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


_DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|mmcblk\d+)$")


def _lin_snap():
    """(cpu_total, cpu_idle, net_bytes, disk_bytes, {iface: bytes}) from /proc."""
    stat = _rd("/proc/stat").splitlines()
    v = [int(x) for x in stat[0].split()[1:]] if stat else [0, 0, 0, 0, 0]
    total, idle = sum(v), v[3] + (v[4] if len(v) > 4 else 0)
    net, ifaces = 0, {}
    for ln in _rd("/proc/net/dev").splitlines():
        if ":" not in ln:
            continue
        name, rest = ln.split(":", 1)
        name, f = name.strip(), rest.split()
        if name != "lo" and len(f) >= 9:
            b = int(f[0]) + int(f[8]); net += b; ifaces[name] = b
    disk = 0
    for ln in _rd("/proc/diskstats").splitlines():
        f = ln.split()
        if len(f) >= 10 and _DISK_RE.match(f[2]):
            disk += (int(f[5]) + int(f[9])) * 512   # (read+write) sectors * 512
    return total, idle, net, disk, ifaces


def _cpu_temp() -> int:
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if _rd(hw + "/name").strip() in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            best = 0
            for inp in sorted(glob.glob(hw + "/temp*_input")):
                lbl = _rd(inp.replace("_input", "_label"))
                val = int(_rd(inp) or 0) / 1000
                if any(t in lbl for t in ("Package", "Tctl", "Tdie")):
                    return round(val)
                best = best or val
            if best:
                return round(best)
    for tz in glob.glob("/sys/class/thermal/thermal_zone*"):
        if _rd(tz + "/type").strip() in ("x86_pkg_temp", "cpu-thermal", "soc_thermal"):
            return round(int(_rd(tz + "/temp") or 0) / 1000)
    return 0


def _linux() -> dict:
    a = _lin_snap(); time.sleep(0.5); b = _lin_snap(); dt = 0.5
    dtot, didle = b[0] - a[0], b[1] - a[1]
    cpu = round((1 - didle / dtot) * 100) if dtot > 0 else 0
    nics = {k: round(max(0, b[4].get(k, 0) - a[4].get(k, 0)) * 8 / 1e9 / dt, 3) for k in b[4]}
    return {"cpu": max(0, min(100, cpu)), "cpu_temp": _cpu_temp(),
            "net_gbps": round(max(0, (b[2] - a[2]) * 8 / 1e9 / dt), 2),
            "disk_mbps": round(max(0, (b[3] - a[3]) / 1048576 / dt), 1),
            "nics": nics}


def sample() -> dict:
    """One live metrics reading for this machine's OS."""
    return _win() if sys.platform.startswith("win") else _linux()


if __name__ == "__main__":
    import json
    print(json.dumps(sample(), indent=2))
