#!/usr/bin/env python3
"""Read-only: PCIe link state of the GPU and of the port above it, from sysfs and config space.
   nv-pcie.py <pci-address>   -> one JSON object per device on stdout.
The faults end with Xid 79 (GPU fell off the bus) and the HAOS kernel has no AER, so link errors are
never logged: the config-space status bits are the only record."""
import json
import os
import sys

ASPM = {0: "off", 1: "L0s", 2: "L1", 3: "L0s+L1"}


def sysfs(dev, name):
    try:
        return open(f"/sys/bus/pci/devices/{dev}/{name}").read().strip()
    except OSError:
        return None


def pcie_regs(dev):
    try:
        cfg = open(f"/sys/bus/pci/devices/{dev}/config", "rb").read()
    except OSError as e:
        return {"error": str(e)}
    if len(cfg) < 64:
        return {"error": f"config space {len(cfg)} bytes"}
    w = lambda o: int.from_bytes(cfg[o:o + 2], "little")
    d = lambda o: int.from_bytes(cfg[o:o + 4], "little")
    out = {}
    ptr, seen = cfg[0x34], set()
    while ptr and ptr not in seen and ptr + 0x14 <= len(cfg):
        seen.add(ptr)
        if cfg[ptr] == 0x10:   # PCI Express capability
            devctl, devsta = w(ptr + 0x08), w(ptr + 0x0a)
            lnkcap, lnkctl, lnksta = d(ptr + 0x0c), w(ptr + 0x10), w(ptr + 0x12)
            out.update({
                "devctl_relaxed_ordering": bool(devctl & 0x10), "devctl_no_snoop": bool(devctl & 0x800),
                "devctl_max_payload": 128 << ((devctl >> 5) & 7), "devctl_max_read": 128 << ((devctl >> 12) & 7),
                # Write-1-to-clear error bits: set means an error happened since boot.
                "devsta_correctable_err": bool(devsta & 1), "devsta_nonfatal_err": bool(devsta & 2),
                "devsta_fatal_err": bool(devsta & 4), "devsta_unsupported_req": bool(devsta & 8),
                "lnkcap_aspm": ASPM[(lnkcap >> 10) & 3], "lnkcap_speed_gen": lnkcap & 0xf,
                "lnkctl_aspm": ASPM[lnkctl & 3], "lnkctl_clock_pm": bool(lnkctl & 0x100),
                "lnksta_speed_gen": lnksta & 0xf, "lnksta_width": (lnksta >> 4) & 0x3f,
            })
            if len(cfg) > 0x100:   # extended capabilities (root only): is AER there at all
                off, caps = 0x100, []
                while off and off + 4 <= len(cfg) and len(caps) < 32:
                    h = d(off)
                    if h in (0, 0xffffffff):
                        break
                    caps.append(hex(h & 0xffff))
                    if h & 0xffff == 1:   # AER: uncorrectable / correctable status registers
                        out["aer_uncorrectable_status"] = hex(d(off + 0x04))
                        out["aer_correctable_status"] = hex(d(off + 0x10))
                    off = (h >> 20) & 0xffc
                out["ext_caps"] = caps
            break
        ptr = cfg[ptr + 1]
    return out


def describe(dev):
    r = {"device": dev}
    for f in ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width",
              "power_state", "d3cold_allowed", "power/control", "power/runtime_status"):
        r[f] = sysfs(dev, f)
    r.update(pcie_regs(dev))
    return r


gpu = sys.argv[1]
port = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/bus/pci/devices/{gpu}")))
for dev in (port, gpu):
    print(json.dumps(describe(dev)))
