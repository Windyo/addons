#!/usr/bin/env python3
"""ACPI events around the GPU, timestamped in UTC, through kprobes in a private tracefs mount.
   nv-acpi-watch.py <tracefs> <summary> [events]     watch until killed
   nv-acpi-watch.py selftest                          parse canned trace lines (CI)
Probes (each optional: one that can't attach is reported in the summary and skipped):
  gpu_notify  nv_acpi_notify_event(handle, event): every ACPI notification to the GPU.
              0xD1-0xD5 = the firmware's "power level" orders (D1 full .. D5 lowest), 0xC0 = GPS
              status change.
  dsm         nv_acpi_dsm_method(nv, guid, rev, nvpcf, subfunction, ...): every _DSM call the driver
              makes (GPS = power steering, JT, NBCI, MXM), with the subfunction.
  notify      acpi_ev_queue_notify_request(node, value): every ACPI notification in the system
              (AC adapter, battery, thermal zones, ...) with the node's 4-letter name.
On this laptop the DSDT turns EC queries _QD1-_QD5 into GPU D-levels (D5 whenever the EC flag PTHF
is set); what makes the EC raise them is EC firmware, not visible in the tables. Hypothesis to
test: a D-level change at load onset coincides with the GPU faults (fault-facts §8.6).
Events print as JSON lines (at most 30 a minute; the counts keep everything) and are appended
fsynced to <events>, like nv-pcie.py's watcher."""
import json
import os
import re
import sys
import time

GROUP = "nvwatch"
PROBES = {
    "gpu_notify": "nv_acpi_notify_event event=$arg2:x32",
    "dsm": "nv_acpi_dsm_method guid=+0($arg2):x32 sub=$arg5:x32",
    # struct acpi_namespace_node: object pointer (8), descriptor_type, type, flags (u16), then the
    # 4-character name at offset 12.
    "notify": "acpi_ev_queue_notify_request name=+12($arg1):x32 value=$arg2:x32",
}
# First 32 bits of each _DSM GUID the driver uses (open-gpu-kernel-modules acpidsmguids.h).
DSM = {0xA3132D01: "GPS", 0xD4A50B75: "NBCI", 0xCBECA351: "JT", 0x4004A400: "MXM",
       0xA486D8F8: "NVOP", 0x9D95A0A0: "NVHG", 0x998669A6: "NBSI", 0x81C6147D: "PCFG",
       0xE5C937D0: "PEX"}
GPU_EVENTS = {0xC0: "GPS status change", 0xD1: "power level D1 (full)", 0xD2: "power level D2",
              0xD3: "power level D3", 0xD4: "power level D4", 0xD5: "power level D5 (lowest)",
              0x80: "status change", 0x81: "information change", 0x02: "device wake"}
LINE = re.compile(r"\s(\d+\.\d+): (\w+): \([^)]*\)\s*(.*)$")


def now_iso(t=None):
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"


def acpi_name(v):
    b = v.to_bytes(4, "little")
    return b.decode() if all(0x30 <= c <= 0x39 or 0x41 <= c <= 0x5A or c == 0x5F for c in b) else hex(v)


def decode(line):
    """One trace_pipe line -> (monotonic seconds, event dict), or None."""
    m = LINE.search(line)
    if not m:
        return None
    ts, probe, rest = float(m[1]), m[2], m[3]
    a = {k: int(v, 16) for k, v in re.findall(r"(\w+)=(0x[0-9a-fA-F]+)", rest)}
    if probe == "gpu_notify" and "event" in a:
        return ts, {"probe": probe, "event": hex(a["event"]),
                    "meaning": GPU_EVENTS.get(a["event"], "unknown")}
    if probe == "dsm" and "guid" in a:
        return ts, {"probe": probe, "dsm": DSM.get(a["guid"], hex(a["guid"])),
                    "sub": hex(a.get("sub", 0))}
    if probe == "notify" and "value" in a:
        return ts, {"probe": probe, "node": acpi_name(a.get("name", 0)), "value": hex(a["value"])}
    return None


def count_key(ev):
    if ev["probe"] == "gpu_notify":
        return f"gpu_notify.{ev['event']}"
    if ev["probe"] == "dsm":
        return f"dsm.{ev['dsm']}.{ev['sub']}"
    return f"notify.{ev['node']}.{ev['value']}"


def write(path, text, mode="w"):
    """mode "a" appends without a seek: Python's append mode seeks to the end at open, which tracefs
    command files (kprobe_events) refuse with EINVAL. "w" truncates, as `echo >` does."""
    if mode == "a":
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def cleanup(t, inst):
    """Remove what an earlier run of this add-on left (the definitions outlive the container)."""
    if os.path.isdir(inst):
        try:
            write(f"{inst}/events/enable", "0")
        except OSError:
            pass
        try:
            os.rmdir(inst)
        except OSError:
            pass
    try:
        defs = open(f"{t}/kprobe_events").read().splitlines()
    except OSError:
        defs = []
    for d in defs:
        if d.startswith(f"p:{GROUP}/"):
            try:
                write(f"{t}/kprobe_events", f"-:{d.split()[0][2:]}\n", "a")
            except OSError:
                pass


def watch(t, summary_path, events_path=None):
    inst = f"{t}/instances/{GROUP}"
    cleanup(t, inst)
    s = {"since": now_iso(), "probes": {}, "events": 0, "counts": {}, "last_event": None}
    for name, spec in PROBES.items():
        try:
            write(f"{t}/kprobe_events", f"p:{GROUP}/{name} {spec}\n", "a")
            s["probes"][name] = "defined"
        except OSError as e:
            s["probes"][name] = f"failed: {e.strerror}"
    os.mkdir(inst)
    write(f"{inst}/buffer_size_kb", "256")
    write(f"{inst}/trace_clock", "mono")   # CLOCK_MONOTONIC: converted to UTC per event below
    for name, state in s["probes"].items():
        if state == "defined":
            try:
                write(f"{inst}/events/{GROUP}/{name}/enable", "1")
                s["probes"][name] = "ok"
            except OSError as e:
                s["probes"][name] = f"failed to enable: {e.strerror}"

    def save():
        s["updated"] = now_iso()
        write(summary_path + ".tmp", json.dumps(s))
        os.replace(summary_path + ".tmp", summary_path)

    save()
    print(json.dumps({"t": now_iso(), "probes": s["probes"]}), flush=True)
    if "ok" not in s["probes"].values():
        return 1
    printed, minute = 0, 0
    with open(f"{inst}/trace_pipe") as pipe:
        for line in pipe:
            d = decode(line)
            if d is None:
                continue
            ts, ev = d
            ev = {"t": now_iso(time.time() - time.monotonic() + ts), **ev}
            k = count_key(ev)
            s["counts"][k] = s["counts"].get(k, 0) + 1
            s["events"] += 1
            s["last_event"] = ev
            m = int(time.monotonic() // 60)
            if m != minute:
                minute, printed = m, 0
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
            save()
    return 0


def selftest():
    lines = [
        "     kworker/0:2-123     [000] d..1.  1234.500000: gpu_notify: (nv_acpi_notify_event+0x0/0x30 [nvidia]) event=0xd3",
        "     nvidia-smi-456     [001] ....1  1234.600000: dsm: (nv_acpi_dsm_method+0x0/0x2a0 [nvidia]) guid=0xa3132d01 sub=0x20",
        "     kworker/0:1-12      [000] d..1.  1234.700000: notify: (acpi_ev_queue_notify_request+0x0/0x150) name=0x31504441 value=0x80",
        "# tracer: nop",
    ]
    out = [decode(x) for x in lines]
    assert out[0][1] == {"probe": "gpu_notify", "event": "0xd3", "meaning": "power level D3"}, out[0]
    assert out[1][1] == {"probe": "dsm", "dsm": "GPS", "sub": "0x20"}, out[1]
    assert out[2][1] == {"probe": "notify", "node": "ADP1", "value": "0x80"}, out[2]
    assert out[3] is None
    assert count_key(out[1][1]) == "dsm.GPS.0x20"
    print("selftest ok")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "selftest":
        sys.exit(selftest())
    sys.exit(watch(a[0], a[1], a[2] if len(a) > 2 else None))
