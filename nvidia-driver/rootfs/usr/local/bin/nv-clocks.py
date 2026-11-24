#!/usr/bin/env python3
"""Clock offsets through NVML (no X server):  nv-clocks.py <core_offset_mhz> <mem_offset_mhz>

Probes which offsets the card accepts (allowed range per P-state), applies the requested ones
(0 = leave as is) and reads them back. Prints one JSON object.
Why: on 2026-11-24 a fault came on a 113.5 W spike at the 115 W cap while the boost clock climbed
1442 -> 1885 MHz in under a second. A negative offset lowers the top clock and its voltage, so
the transients get smaller; idle stays at P8. Offsets last until the driver is unloaded.
"""
import ctypes
import json
import sys

GRAPHICS, MEM = 0, 2
NAMES = {GRAPHICS: "core", MEM: "mem"}
PSTATES = (0, 2)   # GeForce runs CUDA in P2


class ClockOffset(ctypes.Structure):   # nvmlClockOffset_v1_t (R555+)
    _fields_ = [("version", ctypes.c_uint), ("type", ctypes.c_int), ("pstate", ctypes.c_int),
                ("clockOffsetMHz", ctypes.c_int), ("minClockOffsetMHz", ctypes.c_int),
                ("maxClockOffsetMHz", ctypes.c_int)]


VERSION = ctypes.sizeof(ClockOffset) | (1 << 24)


def main():
    want = {GRAPHICS: int(sys.argv[1]), MEM: int(sys.argv[2])}
    nv = ctypes.CDLL("libnvidia-ml.so.1")
    out = {"requested": {"core": want[GRAPHICS], "mem": want[MEM]}}
    if nv.nvmlInit_v2() != 0:
        print(json.dumps({"error": "nvmlInit failed"}))
        return
    h = ctypes.c_void_p()
    nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h))
    nv.nvmlErrorString.restype = ctypes.c_void_p
    err = lambda rc: ctypes.string_at(nv.nvmlErrorString(rc)).decode() if rc else "ok"

    def get(kind, ps):
        o = ClockOffset(version=VERSION, type=kind, pstate=ps)
        rc = nv.nvmlDeviceGetClockOffsets(h, ctypes.byref(o)) if hasattr(nv, "nvmlDeviceGetClockOffsets") else -1
        return rc, o

    probe = {}
    for kind in (GRAPHICS, MEM):
        for ps in PSTATES:
            rc, o = get(kind, ps)
            probe[f"{NAMES[kind]}_P{ps}"] = ({"offset": o.clockOffsetMHz, "min": o.minClockOffsetMHz,
                                              "max": o.maxClockOffsetMHz} if rc == 0 else err(rc) if rc > 0 else "no API")
    # Pre-R555 API, for comparison.
    for kind, fn in ((GRAPHICS, "Gpc"), (MEM, "Mem")):
        lo, hi, cur = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if not hasattr(nv, f"nvmlDeviceGet{fn}ClkVfOffset"):
            probe[f"{NAMES[kind]}_vf"] = "no API"
            continue
        rc1 = getattr(nv, f"nvmlDeviceGet{fn}ClkMinMaxVfOffset")(h, ctypes.byref(lo), ctypes.byref(hi))
        rc2 = getattr(nv, f"nvmlDeviceGet{fn}ClkVfOffset")(h, ctypes.byref(cur))
        probe[f"{NAMES[kind]}_vf"] = {"offset": cur.value if rc2 == 0 else err(rc2),
                                      "range": [lo.value, hi.value] if rc1 == 0 else err(rc1)}
    out["probe"] = probe

    applied = {}
    for kind in (GRAPHICS, MEM):
        mhz = want[kind]
        if mhz == 0:
            continue
        res = {}
        for ps in PSTATES:
            rc, o = get(kind, ps)
            if rc != 0:
                continue
            o.clockOffsetMHz = mhz
            rc = nv.nvmlDeviceSetClockOffsets(h, ctypes.byref(o))
            res[f"P{ps}"] = err(rc)
        if not any(v == "ok" for v in res.values()) and hasattr(nv, "nvmlDeviceSetGpcClkVfOffset"):
            fn = "nvmlDeviceSetGpcClkVfOffset" if kind == GRAPHICS else "nvmlDeviceSetMemClkVfOffset"
            res["vf"] = err(getattr(nv, fn)(h, ctypes.c_int(mhz)))
        applied[NAMES[kind]] = res
    out["applied"] = applied
    if applied:
        out["after"] = {f"{NAMES[k]}_P{ps}": get(k, ps)[1].clockOffsetMHz for k in (GRAPHICS, MEM) for ps in PSTATES
                        if get(k, ps)[0] == 0}
    print(json.dumps(out))


main()
