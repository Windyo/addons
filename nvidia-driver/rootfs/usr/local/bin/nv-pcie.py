#!/usr/bin/env python3
"""PCIe link state of the GPU and of the port above it, from sysfs and config space.
   nv-pcie.py <gpu>                                   read-only snapshot: one JSON object per device
   nv-pcie.py watch <gpu> <sysfs> <interval> <summary> [events]  timestamp every link error (see watch())
   nv-pcie.py cap <gpu> <sysfs> <gen> <state>          cap the link speed at the root port (see cap())
<sysfs> is a sysfs mount the add-on can write to (the container's own /sys is read-only).
The faults end with Xid 79 (GPU fell off the bus) and the HAOS kernel has no AER, so link errors are
never logged: the config-space status bits are the only record."""
import json
import os
import sys
import time

ASPM = {0: "off", 1: "L0s", 2: "L1", 3: "L0s+L1"}
DEVSTA = {0: "correctable", 1: "nonfatal", 2: "fatal", 3: "unsupported_req"}
AER_CORR = {0: "receiver_error", 6: "bad_tlp", 7: "bad_dllp", 8: "replay_num_rollover",
            12: "replay_timer_timeout", 13: "advisory_nonfatal", 14: "corrected_internal",
            15: "header_log_overflow"}
AER_UNCORR = {4: "data_link_protocol", 5: "surprise_down", 12: "poisoned_tlp",
              13: "flow_control_protocol", 14: "completion_timeout", 15: "completer_abort",
              16: "unexpected_completion", 17: "receiver_overflow", 18: "malformed_tlp",
              19: "ecrc", 20: "unsupported_request", 21: "acs_violation", 22: "uncorrectable_internal"}


def sysfs(dev, name):
    try:
        return open(f"/sys/bus/pci/devices/{dev}/{name}").read().strip()
    except OSError:
        return None


def find_caps(cfg):
    """-> (offset of the PCI Express capability, offset of AER, list of extended capability ids);
    an offset is None when the capability is absent."""
    d = lambda o: int.from_bytes(cfg[o:o + 4], "little")
    pcie = aer = None
    caps = []
    ptr, seen = cfg[0x34] if len(cfg) > 0x34 else 0, set()
    while ptr and ptr not in seen and ptr + 0x14 <= len(cfg):
        seen.add(ptr)
        if cfg[ptr] == 0x10:
            pcie = ptr
            break
        ptr = cfg[ptr + 1]
    off, n = 0x100, 0
    while pcie and off and off + 4 <= len(cfg) and n < 32:
        h = d(off)
        if h in (0, 0xffffffff):
            break
        caps.append(hex(h & 0xffff))
        if h & 0xffff == 1:
            aer = off
        off, n = (h >> 20) & 0xffc, n + 1
    return pcie, aer, caps


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
    ptr, aer, caps = find_caps(cfg)
    if ptr is not None:
        devctl, devsta = w(ptr + 0x08), w(ptr + 0x0a)
        lnkcap, lnkctl, lnksta = d(ptr + 0x0c), w(ptr + 0x10), w(ptr + 0x12)
        out.update({
            "devctl_relaxed_ordering": bool(devctl & 0x10), "devctl_no_snoop": bool(devctl & 0x800),
            "devctl_max_payload": 128 << ((devctl >> 5) & 7), "devctl_max_read": 128 << ((devctl >> 12) & 7),
            # Write-1-to-clear error bits: set means an error happened since boot (or since the
            # watcher last cleared them).
            "devsta_correctable_err": bool(devsta & 1), "devsta_nonfatal_err": bool(devsta & 2),
            "devsta_fatal_err": bool(devsta & 4), "devsta_unsupported_req": bool(devsta & 8),
            "lnkcap_aspm": ASPM[(lnkcap >> 10) & 3], "lnkcap_speed_gen": lnkcap & 0xf,
            "lnkctl_aspm": ASPM[lnkctl & 3], "lnkctl_clock_pm": bool(lnkctl & 0x100),
            "lnksta_speed_gen": lnksta & 0xf, "lnksta_width": (lnksta >> 4) & 0x3f,
        })
        if ptr + 0x32 <= len(cfg):
            out["lnkctl2_target_gen"] = w(ptr + 0x30) & 0xf
        if aer is not None:
            out["aer_uncorrectable_status"] = hex(d(aer + 0x04))
            out["aer_correctable_status"] = hex(d(aer + 0x10))
        if caps:
            out["ext_caps"] = caps
    return out


def describe(dev):
    r = {"device": dev}
    for f in ("current_link_speed", "current_link_width", "max_link_speed", "max_link_width",
              "power_state", "d3cold_allowed", "power/control", "power/runtime_status"):
        r[f] = sysfs(dev, f)
    r.update(pcie_regs(dev))
    return r


def port_of(gpu, root="/sys"):
    return os.path.basename(os.path.dirname(os.path.realpath(f"{root}/bus/pci/devices/{gpu}")))


class Cfg:
    """Config space of one device through a writable sysfs mount."""

    def __init__(self, root, dev):
        self.dev = dev
        self.fd = os.open(f"{root}/bus/pci/devices/{dev}/config", os.O_RDWR)
        self.pcie, self.aer, _ = find_caps(os.pread(self.fd, 4096, 0))
        if self.pcie is None:
            raise SystemExit(f"{dev}: no PCI Express capability")

    def r(self, off, size):
        return int.from_bytes(os.pread(self.fd, size, off), "little")

    def w(self, off, size, val):
        os.pwrite(self.fd, val.to_bytes(size, "little"), off)


def names(val, table):
    return [table.get(b, f"bit{b}") for b in range(32) if val >> b & 1]


def now_iso():
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"


def watch(gpu, root, interval, summary_path, events_path=None):
    """Poll the latched error bits of the GPU and its root port. Each poll that finds any set prints one
    JSON line (UTC time, link speed, seconds since the last speed change, which errors) and clears
    them (write-1-to-clear), so the next error gets its own timestamp. Counts and the highest link
    speed seen go to <summary> for the status sensor. Read-and-clear is what the kernel's AER
    driver would do; HAOS doesn't build it. Events are also appended to <events> with an fsync: the
    host journal did not survive the hard power-off of 2026-11-24."""
    port = port_of(gpu, root)
    g, p = Cfg(root, gpu), Cfg(root, port)
    s = {"since": now_iso(), "interval_s": interval, "polls": 0, "events": 0, "gen_now": None,
         "gen_max": 0, "gen_changes": 0, "counts": {}, "last_event": None, "gpu_unreadable": 0,
         "port_target_gen": p.r(p.pcie + 0x30, 2) & 0xf if p.pcie else None}
    last_gen, last_change, printed, minute, last_write = None, time.monotonic(), 0, 0, 0.0

    def count(where, items):
        for n in items:
            k = f"{where}.{n}"
            s["counts"][k] = s["counts"].get(k, 0) + 1

    while True:
        t = time.monotonic()
        s["polls"] += 1
        ev = {}
        if g.r(0, 2) == 0xffff:   # GPU not answering config reads (fallen off the bus)
            s["gpu_unreadable"] += 1
            gen = None
        else:
            gen = g.r(g.pcie + 0x12, 2) & 0xf
            gsta = g.r(g.pcie + 0x0a, 2) & 0xf
            if gsta:
                ev["gpu_devsta"] = names(gsta, DEVSTA)
                g.w(g.pcie + 0x0a, 2, gsta)
            if g.aer is not None:
                corr, unc = g.r(g.aer + 0x10, 4), g.r(g.aer + 0x04, 4)
                if corr:
                    ev["gpu_corr"] = names(corr, AER_CORR)
                    g.w(g.aer + 0x10, 4, corr)
                if unc:
                    ev["gpu_uncorr"] = names(unc, AER_UNCORR)
                    g.w(g.aer + 0x04, 4, unc)
        psta = p.r(p.pcie + 0x0a, 2) & 0xf
        if psta:
            ev["port_devsta"] = names(psta, DEVSTA)
            p.w(p.pcie + 0x0a, 2, psta)
        if p.aer is not None:
            corr, unc = p.r(p.aer + 0x10, 4), p.r(p.aer + 0x04, 4)
            if corr:
                ev["port_corr"] = names(corr, AER_CORR)
                p.w(p.aer + 0x10, 4, corr)
            if unc:
                ev["port_uncorr"] = names(unc, AER_UNCORR)
                p.w(p.aer + 0x04, 4, unc)
        if gen is not None and gen != last_gen:
            if last_gen is not None:
                s["gen_changes"] += 1
            last_gen, last_change = gen, t
            s["gen_max"] = max(s["gen_max"], gen)
        s["gen_now"] = gen
        if ev:
            s["events"] += 1
            for k, v in ev.items():
                count(k, v)
            ev = {"t": now_iso(), "gen": gen, "since_speed_change_s": round(t - last_change, 1), **ev}
            s["last_event"] = ev
            # At most 30 lines a minute in the log; the counts keep everything.
            if int(t // 60) != minute:
                minute, printed = int(t // 60), 0
            if printed < 30:
                print(json.dumps(ev), flush=True)
                printed += 1
            if events_path:
                if os.path.exists(events_path) and os.path.getsize(events_path) > 1 << 20:
                    os.replace(events_path, events_path + ".1")
                with open(events_path, "a") as f:
                    f.write(json.dumps(ev) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        if ev or t - last_write >= 15:
            s["updated"] = now_iso()
            with open(summary_path + ".tmp", "w") as f:
                json.dump(s, f)
            os.replace(summary_path + ".tmp", summary_path)
            last_write = t
        time.sleep(max(0.0, interval - (time.monotonic() - t)))


def cap(gpu, root, gen, state_path):
    """Cap the link speed with the root port's Target Link Speed (Link Control 2), which limits the
    rates the port offers in training: what a BIOS "PCIe Gen2" setting does. The driver changes the
    speed itself (gen 1 at idle, gen 3 under load), so the cap holds from the next change on. gen 0
    restores the value this boot started with. Prints one JSON line."""
    port = port_of(gpu, root)
    p = Cfg(root, port)
    boot = open("/proc/sys/kernel/random/boot_id").read().strip()
    lnkcap = p.r(p.pcie + 0x0c, 4) & 0xf
    ctl2 = p.r(p.pcie + 0x30, 2)
    before = ctl2 & 0xf
    try:
        st = json.load(open(state_path))
        if st.get("boot_id") != boot:
            st = None
    except (OSError, ValueError):
        st = None
    if gen > 0:
        if st is None:
            st = {"boot_id": boot, "original_target_gen": before}
            json.dump(st, open(state_path, "w"))
        target = min(gen, lnkcap)
    else:
        target = st["original_target_gen"] if st else before
    if target != before:
        p.w(p.pcie + 0x30, 2, (ctl2 & ~0xf) | target)
    after = p.r(p.pcie + 0x30, 2) & 0xf
    if gen == 0 and st and after == st["original_target_gen"]:
        os.remove(state_path)
    print(json.dumps({"port": port, "requested_gen": gen, "port_max_gen": lnkcap,
                      "target_gen_before": before, "target_gen_after": after,
                      "link_gen_now": p.r(p.pcie + 0x12, 2) & 0xf}))
    return 0 if after == target else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "watch":
        watch(a[1], a[2], float(a[3]), a[4], a[5] if len(a) > 5 else None)
    elif a and a[0] == "cap":
        sys.exit(cap(a[1], a[2], int(a[3]), a[4]))
    else:
        gpu = a[0]
        for dev in (port_of(gpu), gpu):
            print(json.dumps(describe(dev)))
