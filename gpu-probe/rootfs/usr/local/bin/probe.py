#!/usr/bin/env python3
"""GPU probe: can an add-on with no kernel rights use the NVIDIA GPU that another add-on loaded?

Waits for the driver and its /dev/nvidia* nodes, checks the driver version, opens each node, then
runs nvidia-smi and a small CUDA job (cuda_check.py, in a subprocess so a crash or a hang can't
take the probe down). It records when the nodes appeared: a run that starts before the driver
loads shows whether new nodes reach a container that is already running.

Results: sensor.gpu_probe_status and /data/runs.json (last 10 runs).
`probe.py --facts` prints only the container facts (used by CI, which has no GPU).
"""
import errno
import json
import os
import stat
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ENTITY = "sensor.gpu_probe_status"
REQUIRED = ("/dev/nvidiactl", "/dev/nvidia0", "/dev/nvidia-uvm")
NODES = REQUIRED + ("/dev/nvidia-uvm-tools",)
USERSPACE = "/opt/nvidia/VERSION"
RUNS = "/data/runs.json"
POLL_S = 2  # node watch while waiting
SETTLE_S = 10  # after the nodes appear, let the add-on that loaded the driver finish its setup
ATTEMPTS = 3  # a failure right after the driver loads can be a race: retry before deciding
RETRY_S = 30
REPUBLISH_S = 60  # identical state: restores the sensor after a Core restart, adds no history

# Linux capability bits 0-40, for CapEff.
CAPS = (
    "chown", "dac_override", "dac_read_search", "fowner", "fsetid", "kill", "setgid", "setuid",
    "setpcap", "linux_immutable", "net_bind_service", "net_broadcast", "net_admin", "net_raw",
    "ipc_lock", "ipc_owner", "sys_module", "sys_rawio", "sys_chroot", "sys_ptrace", "sys_pacct",
    "sys_admin", "sys_boot", "sys_nice", "sys_resource", "sys_time", "sys_tty_config", "mknod",
    "lease", "audit_write", "audit_control", "setfcap", "mac_override", "mac_admin", "syslog",
    "wake_alarm", "block_suspend", "audit_read", "perfmon", "bpf", "checkpoint_restore",
)
KERNEL_RIGHTS = ("sys_module", "sys_admin", "sys_rawio")

HINTS = {
    "EPERM": "device cgroup refused the open: full_access is ignored while Protection mode is on",
    "EACCES": "refused by AppArmor or by the node's file mode",
    "ENXIO": "the driver has no device behind this node",
    "ENODEV": "the driver has no device behind this node",
}

_published = None  # last publish result, to log only the changes


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip().strip("\0").strip() or None
    except OSError:
        return None


def status_field(name):
    for line in (read("/proc/self/status") or "").splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return value.strip()
    return None


def apparmor_label():
    for path in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        label = read(path)
        if label:
            return label
    return "unavailable"


def dev_mount():
    """The top /dev mount as `fstype root options` (the Supervisor binds the host's /dev read-only)."""
    found = None
    for line in (read("/proc/self/mountinfo") or "").splitlines():
        left, _, right = line.partition(" - ")
        f = left.split()
        if len(f) >= 6 and f[4] == "/dev" and right:
            found = f"{right.split()[0]} {f[3]} {f[5]}"
    return found


def facts():
    capeff = int(status_field("CapEff") or "0", 16)
    caps = [name for bit, name in enumerate(CAPS) if capeff >> bit & 1]
    return {
        "boot_id": read("/proc/sys/kernel/random/boot_id"),
        "kernel": os.uname().release,
        "kernel_rights": [c for c in caps if c in KERNEL_RIGHTS],
        "capabilities": caps,
        "apparmor": apparmor_label(),
        "seccomp": {"0": "off", "1": "strict", "2": "filter"}.get(status_field("Seccomp"), "unknown"),
        "dev_mount": dev_mount(),
        "userspace": read(USERSPACE),
    }


def watch():
    """Everything the probe waits for: node -> 'char major:minor' or None, module -> version/bool."""
    out = {}
    for path in NODES:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            out[path] = None
            continue
        except OSError as e:
            out[path] = errno.errorcode.get(e.errno, str(e.errno))
            continue
        out[path] = (f"char {os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
                     if stat.S_ISCHR(st.st_mode) else "not a char device")
    out["module nvidia"] = read("/sys/module/nvidia/version")
    out["module nvidia_uvm"] = os.path.isdir("/sys/module/nvidia_uvm")
    return out


def is_ready(w):
    return bool(w["module nvidia"] and w["module nvidia_uvm"]) and all(
        (w[p] or "").startswith("char") for p in REQUIRED)


def open_nodes():
    out = {}
    for path in REQUIRED:
        try:
            os.close(os.open(path, os.O_RDWR))
            out[path] = "ok"
        except OSError as e:
            out[path] = errno.errorcode.get(e.errno, str(e.errno))
    return out


def run(cmd, timeout):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout} s"
    except OSError as e:
        return None, "", str(e)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def nvidia_smi():
    rc, out, err = run(["nvidia-smi", "--format=csv,noheader", "--query-gpu=name,driver_version,pstate,"
                        "temperature.gpu,memory.used,memory.total,compute_mode,persistence_mode"], 30)
    res = {"rc": rc, "gpu": out or err[-500:]}
    if rc == 0:
        # Processes in other containers show up here too: the GPU is shared, not handed over.
        _, apps, _ = run(["nvidia-smi", "--format=csv,noheader",
                          "--query-compute-apps=pid,process_name,used_memory"], 30)
        res["compute_apps"] = apps.splitlines()
    return res


def cuda():
    rc, out, err = run([sys.executable, "/usr/local/bin/cuda_check.py"], 120)
    try:
        res = json.loads(out.splitlines()[-1])
    except (IndexError, ValueError):
        res = {"ok": False, "error": f"no result (exit {rc})"}
    if err:
        res["stderr"] = err[-500:]
    return res


def attempt():
    res = {"at": now(), "open": open_nodes(), "nvidia_smi": nvidia_smi(), "cuda": cuda()}
    res["ok"] = (all(v == "ok" for v in res["open"].values()) and res["nvidia_smi"]["rc"] == 0
                 and res["cuda"].get("ok") is True)
    hints = sorted({HINTS[v] for v in res["open"].values() if v in HINTS})
    if hints:
        res["hints"] = hints
    return res


def publish(state, attrs):
    global _published
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    req = urllib.request.Request(
        f"http://supervisor/core/api/states/{ENTITY}", method="POST",
        data=json.dumps({"state": state, "attributes": attrs}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            ok = True
    except OSError as e:  # Core not up yet during boot, or restarting
        ok = False
        if _published is not False:
            log(f"could not publish {ENTITY} ({e}); retrying")
    if ok and not _published:
        log(f"published {ENTITY}: {state}")
    _published = ok


def load_runs():
    try:
        with open(RUNS, encoding="utf-8") as f:
            runs = json.load(f)
        return runs if isinstance(runs, list) else []
    except (OSError, ValueError):
        return []


def save_runs(runs):
    try:
        with open(RUNS + ".tmp", "w", encoding="utf-8") as f:
            json.dump(runs[-10:], f, indent=1)
        os.replace(RUNS + ".tmp", RUNS)
    except OSError as e:
        log(f"WARNING: could not save {RUNS}: {e}")


def summary(r):
    env = r.get("env", {})
    return {"started": r.get("started"), "boot": (env.get("boot_id") or "")[:8], "mode": r.get("mode"),
            "apparmor": env.get("apparmor"), "verdict": r.get("verdict")}


def main():
    if "--facts" in sys.argv:
        print(json.dumps(facts()))
        return

    previous = load_runs()
    start = watch()
    record = {"started": now(), "probe_version": os.environ.get("PROBE_VERSION", "dev"), "env": facts(),
            "at_start": start, "mode": "after" if is_ready(start) else "before", "appeared": {}}

    def attrs():
        return {"friendly_name": "GPU probe status", "icon": "mdi:test-tube", **record,
                "history": [summary(r) for r in previous[-9:]]}

    env = record["env"]
    log(f"GPU probe {record['probe_version']}: apparmor={env['apparmor']} seccomp={env['seccomp']} "
        f"kernel_rights={env['kernel_rights'] or 'none'} /dev={env['dev_mount']}")
    log(f"mode {record['mode']}: at start {start}")

    if record["mode"] == "before":
        publish("waiting_gpu", attrs())
        last = time.monotonic()
        while True:
            time.sleep(POLL_S)
            w = watch()
            changed = False
            for key, value in w.items():
                if value and not start[key] and key not in record["appeared"]:
                    record["appeared"][key] = now()
                    log(f"appeared: {key} = {value}")
                    changed = True
            if is_ready(w):
                break
            if changed or time.monotonic() - last >= 30:
                publish("waiting_gpu", attrs())
                last = time.monotonic()
        record["live_appearance"] = all(p in record["appeared"] for p in REQUIRED)
        log(f"driver and nodes ready; waiting {SETTLE_S} s before testing")
        time.sleep(SETTLE_S)

    record["driver"] = read("/sys/module/nvidia/version")
    if record["driver"] != env["userspace"]:
        record["verdict"] = "driver_mismatch"
        log(f"driver {record['driver']} != userspace {env['userspace']}: not opening the GPU")
    else:
        publish("testing", attrs())
        record["attempts"] = []
        for i in range(ATTEMPTS):
            res = attempt()
            record["attempts"].append(res)
            cu = res["cuda"]
            log(f"attempt {i + 1}: {'PASS' if res['ok'] else 'FAIL'} open={res['open']} "
                f"nvidia-smi rc={res['nvidia_smi']['rc']} cuda ok={cu.get('ok')} "
                f"{cu.get('failed_step', '')} {cu.get('error', '')}".rstrip())
            for hint in res.get("hints", []):
                log(f"hint: {hint}")
            if res["ok"]:
                break
            if i + 1 < ATTEMPTS:
                time.sleep(RETRY_S)
        record["verdict"] = "pass" if record["attempts"][-1]["ok"] else "fail"

    record["finished"] = now()
    save_runs(previous + [record])
    log(f"verdict: {record['verdict']}")
    final = attrs()
    while True:
        publish(record["verdict"], final)
        time.sleep(REPUBLISH_S)


if __name__ == "__main__":
    main()
