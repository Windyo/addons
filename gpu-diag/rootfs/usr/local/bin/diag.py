#!/usr/bin/env python3
"""GPU diagnostic add-on: why does llama.cpp fault this card (Xid 13, then Xid 79)?

Runs once per start, stopping at the first failure, in order of risk:
1. vram  - pattern test over free VRAM (memory-bound, low SM load); `vram_passes` sets how long
           it streams (each pass writes and reads every tested byte).
2. ops   - llama.cpp test-backend-ops (b11845, built for sm_61 only by us) on CUDA0, compared with
           the CPU, CUDA_LAUNCH_BLOCKING=1 so a fault names its test. Small tensors, short kernels.
3. model - llama-bench (same build) on the real Qwen3-4B Q4_K_M: prompt processing at 32/256/512
           tokens (`model_tests: pp_tg`), then 64 generated tokens; `model_env` adds variables such
           as GGML_CUDA_DISABLE_FUSION=1.
4. burn  - gpu-burn (cuBLAS SGEMM at full load) for `burn_seconds`, with a temperature cap.
Reading it: vram fails -> VRAM; ops fault -> the kernels themselves (our build too), not Ollama's
packaging; model faults -> the real workload (shapes, load pattern); burn faults -> power/hardware
under sustained full load; all pass -> Ollama's build.
Every stage needs the driver add-on `ready` before it starts; any fault ends the run (the card is
not touched again until a power cycle).
Results: sensor.gpu_diag_status and /data/results.json.
"""
import ctypes
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone

STATE_FILE = "/share/nvidia/state.json"
NV_VERSION = open("/opt/nvidia/VERSION").read().strip()
BIN = "/opt/diag/bin"
OPTS = json.load(open("/data/options.json"))
BOOT = open("/proc/sys/kernel/random/boot_id").read().strip()
TAIL = 40
SAMPLE_HZ = 4
MAX_SAMPLES = SAMPLE_HZ * 900
# unsloth's GGUF of the model Ollama runs (same architecture and quantisation).
MODEL_URL = ("https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/"
             "Qwen3-4B-Instruct-2507-Q4_K_M.gguf")
MODEL_SHA256 = "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597"
MODEL_FILE = "/data/qwen3-4b-instruct-2507-q4_k_m.gguf"

results = {"boot_id": BOOT, "started": None, "stages": {}}
SAMPLER = None


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publish(state):
    token = os.environ.get("SUPERVISOR_TOKEN")
    with open("/data/results.json", "w") as f:
        json.dump(results, f, indent=1)
    if not token:
        return
    attrs = {"friendly_name": "GPU diagnostic", "icon": "mdi:stethoscope", "updated": now(), **results}
    req = urllib.request.Request(
        "http://supervisor/core/api/states/sensor.gpu_diag_status", method="POST",
        data=json.dumps({"state": state, "attributes": attrs}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as e:
        log(f"WARNING: could not publish: {e}")


def driver_state():
    """-> (ready: bool, reason). The consumer contract: ready, this boot, < 60 s old."""
    try:
        loaded = open("/sys/module/nvidia/version").read().strip()
    except OSError:
        return False, "driver not loaded"
    if loaded != NV_VERSION:
        return False, f"driver_mismatch: kernel {loaded}, userspace {NV_VERSION}"
    try:
        st = json.load(open(STATE_FILE))
        age = time.time() - datetime.strptime(st["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (OSError, ValueError, KeyError):
        return False, f"no readable {STATE_FILE}"
    if st.get("boot_id") != BOOT or age >= 60:
        return False, "state.json stale or from another boot"
    if st.get("state") != "ready":
        return False, f"driver add-on: {st.get('state')}"
    if st.get("thermal") != "ok":
        return False, "driver add-on: GPU hot"
    return True, "ready"


class Sampler:
    """In-process NVML sampling at SAMPLE_HZ for the whole run: P-state, clocks, PCIe link, power,
    temperature, clock event reasons. One NVML client for the run, instead of an nvidia-smi process
    (a full NVML init) every 2 s. Every fault so far came from idle P8 within seconds of a job
    starting; the samples show what the clocks were doing at that moment."""
    FIELDS = ("t", "pstate", "gr_mhz", "mem_mhz", "pcie_gen", "pcie_width", "power_w", "temp_c",
              "util_pct", "reasons")

    def __init__(self):
        self.samples, self.t0, self.lock = [], time.monotonic(), threading.Lock()
        self.error = None
        try:
            nv = ctypes.CDLL("libnvidia-ml.so.1")
            if nv.nvmlInit_v2() != 0:
                raise OSError("nvmlInit_v2 failed")
            self.h = ctypes.c_void_p()
            if nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(self.h)) != 0:
                raise OSError("no NVML device 0")
            self.nv = nv
        except OSError as e:
            self.nv, self.error = None, str(e)
            log(f"WARNING: NVML sampler off ({e}); temperature from nvidia-smi")
            return
        threading.Thread(target=self.loop, daemon=True).start()

    def _u(self, fn, *args, kind=ctypes.c_uint):
        v = kind()
        return v.value if getattr(self.nv, fn)(self.h, *args, ctypes.byref(v)) == 0 else None

    def read(self):
        nv = self.nv
        util = (ctypes.c_uint * 2)()
        u = util[0] if nv.nvmlDeviceGetUtilizationRates(self.h, ctypes.byref(util)) == 0 else None
        reasons_fn = ("nvmlDeviceGetCurrentClocksEventReasons" if hasattr(nv, "nvmlDeviceGetCurrentClocksEventReasons")
                      else "nvmlDeviceGetCurrentClocksThrottleReasons")
        p, w, r = (self._u("nvmlDeviceGetPerformanceState", kind=ctypes.c_int),
                   self._u("nvmlDeviceGetPowerUsage"),
                   self._u(reasons_fn, kind=ctypes.c_ulonglong))
        return (round(time.monotonic() - self.t0, 2), p,
                self._u("nvmlDeviceGetClockInfo", 0), self._u("nvmlDeviceGetClockInfo", 2),
                self._u("nvmlDeviceGetCurrPcieLinkGeneration"), self._u("nvmlDeviceGetCurrPcieLinkWidth"),
                None if w is None else round(w / 1000, 1), self._u("nvmlDeviceGetTemperature", 0),
                u, None if r is None else hex(r))

    def loop(self):
        while True:
            s = self.read()
            with self.lock:
                self.samples.append(s)
                del self.samples[:-MAX_SAMPLES]
            time.sleep(1 / SAMPLE_HZ)

    def now(self):
        return round(time.monotonic() - self.t0, 2)

    def since(self, t):
        with self.lock:
            return [s for s in self.samples if s[0] >= t]

    def temp(self):
        with self.lock:
            fresh = self.samples[-1] if self.samples and self.now() - self.samples[-1][0] < 2 else None
        if fresh and fresh[7] is not None:
            return fresh[7]
        try:
            out = subprocess.run(["nvidia-smi", "--format=csv,noheader,nounits", "--query-gpu=temperature.gpu"],
                                 capture_output=True, text=True, timeout=10)
            return int(out.stdout.strip()) if out.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


def changes(samples):
    """Samples where the P-state, a clock or the PCIe link changed (plus the first and last)."""
    out, prev = [], None
    for s in samples:
        key = (s[1], s[2], s[3], s[4], s[5])
        if key != prev:
            out.append(s)
            prev = key
    if samples and (not out or out[-1] is not samples[-1]):
        out.append(samples[-1])
    return out


def gpu_temp():
    return SAMPLER.temp()


def run_stage(name, cmd, timeout, env=None, cwd=None):
    """Streams the command's output to the log. -> dict with rc, tail, max_temp_c, aborted."""
    res = {"cmd": " ".join(cmd), "started": now(), "max_temp_c": None, "aborted": None}
    results["stages"][name] = res
    publish(f"running {name}")
    log(f"== {name}: {res['cmd']}")
    t_start, t_err = SAMPLER.now(), None
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                         cwd=cwd, env={**os.environ, **(env or {})})
    tail, flagged = [], []
    done = threading.Event()

    def watch():   # temperature cap and wall-clock timeout
        t0 = time.monotonic()
        while not done.wait(2):
            t = gpu_temp()
            if t is not None:
                res["max_temp_c"] = max(res["max_temp_c"] or 0, t)
            ok, reason = driver_state()
            if not ok and "gpu_error" in reason:
                res["aborted"] = reason
            elif t is not None and t >= OPTS["max_temp_c"]:
                res["aborted"] = f"GPU {t} C >= {OPTS['max_temp_c']} C"
            elif time.monotonic() - t0 > timeout:
                res["aborted"] = f"timeout after {timeout} s"
            if res["aborted"]:
                log(f"{name}: stopping, {res['aborted']}")
                p.kill()
                # A process stuck in a hung GPU may never exit: report now, not after it does.
                results["stopped"] = f"{name}: {res['aborted']}"
                publish(f"fault in {name}" if "gpu_error" in res["aborted"] else f"failed {name}")
                return

    threading.Thread(target=watch, daemon=True).start()
    for line in p.stdout:
        line = line.rstrip()
        # gpu-burn redraws its progress line with \r: keep only the last frame
        line = line.rsplit("\r", 1)[-1]
        if line:
            print(f"  {line}", flush=True)
            tail = (tail + [line])[-TAIL:]
            if "FAIL" in line or "error" in line.lower() or "illegal" in line.lower():
                flagged = (flagged + [line])[:30]
                if t_err is None:
                    t_err = SAMPLER.now()
    p.wait()
    done.set()
    res.update({"rc": p.returncode, "tail": tail, "flagged": flagged, "finished": now()})
    if SAMPLER.nv:
        # Times are seconds since the add-on started; the stage started at t_start. A CUDA error is
        # reported when the host next synchronises, so the fault itself is at or before t_err.
        window = SAMPLER.since(t_start - 3)
        res["clocks"] = {"fields": Sampler.FIELDS, "stage_start_t": t_start, "first_error_t": t_err,
                         "changes": changes(window),
                         "around_error": [s for s in window if t_err is not None and t_err - 4 <= s[0] <= t_err + 1]}
        for s in res["clocks"]["changes"][:60]:
            log(f"{name} clocks: " + " ".join(f"{k}={v}" for k, v in zip(Sampler.FIELDS, s)))
        if t_err is not None:
            log(f"{name}: first error line at t={t_err} (stage start t={t_start})")
    return res


def after_stage(name):
    """The card must still answer and the driver add-on must still say ready."""
    time.sleep(20)   # the driver add-on polls every 15 s
    ok, reason = driver_state()
    if not ok:
        results["stages"][name]["gpu_after"] = reason
        return False
    return True


def stage_vram():
    r = run_stage("vram", [f"{BIN}/vram_test", str(OPTS["reserve_mib"]), str(OPTS["vram_passes"])], 3600)
    try:
        last = r["tail"][-1]   # a CUDA error's JSON can follow a half-printed result line
        r["result"] = json.loads(last[last.rfind('{"'):])
    except (ValueError, IndexError):
        r["result"] = None
    return r["rc"] == 0 and not r["aborted"] and (r["result"] or {}).get("ok") is True


def stage_ops():
    cmd = [f"{BIN}/test-backend-ops", "test", "-b", "CUDA0"]
    if OPTS["ops"] == "matmul":
        cmd += ["-o", "MUL_MAT"]
    r = run_stage("ops", cmd, 3600, env={"CUDA_LAUNCH_BLOCKING": "1"})
    r["summary"] = [l for l in r["tail"] if "tests passed" in l or "Backend CUDA0" in l]
    return r["rc"] == 0 and not r["aborted"]


def fetch_model():
    """Downloads the GGUF once, checked against its sha256. -> error text or None."""
    if os.path.exists(MODEL_FILE):
        return None
    log(f"downloading {MODEL_URL} (2.5 GB, once)")
    publish("downloading model")
    h, part = hashlib.sha256(), MODEL_FILE + ".part"
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
    except OSError as e:
        return f"download failed: {e}"
    if h.hexdigest() != MODEL_SHA256:
        os.remove(part)
        return f"model sha256 mismatch: {h.hexdigest()}"
    os.rename(part, MODEL_FILE)
    return None


def stage_model():
    err = fetch_model()
    if err:
        results["stages"]["model"] = {"error": err}
        log(err)
        return False
    # Generation (one token at a time) faulted on 2026-11-23 after pp 32/256/512 passed.
    pp = "32,256,512" if OPTS["model_tests"] == "pp_tg" else "0"
    env = dict(kv.split("=", 1) for kv in OPTS["model_env"].split())
    if OPTS["prewarm_s"]:
        # A/B for "the fault comes with the ramp out of idle P8": raise the clocks with gpu-burn
        # (never faulted), then start llama-bench at once, before the card drops back to P8.
        w = run_stage("prewarm", [f"{BIN}/gpu_burn", "-m", "512", str(OPTS["prewarm_s"])],
                      OPTS["prewarm_s"] + 60, cwd=BIN)
        if w["rc"] != 0 or w["aborted"]:
            results["stages"]["model"] = {"error": f"prewarm failed: rc {w['rc']}, {w['aborted']}"}
            return False
    r = run_stage("model", [f"{BIN}/llama-bench", "-m", MODEL_FILE, "-ngl", "99", "-fa", "0",
                            "-p", pp, "-n", "64", "-r", "1", "-o", "md"], 900, env=env)
    r["env"] = env
    r["prewarm_s"] = OPTS["prewarm_s"]
    r["table"] = [l for l in r["tail"] if l.startswith("|")]
    return r["rc"] == 0 and not r["aborted"]


def stage_burn():
    r = run_stage("burn", [f"{BIN}/gpu_burn", "-m", str(OPTS["burn_mib"]), str(OPTS["burn_seconds"])],
                  OPTS["burn_seconds"] + 120, cwd=BIN)
    r["verdict"] = next((l.split(":", 1)[1].strip() for l in r["tail"] if l.strip().startswith("GPU 0:")), None)
    return r["rc"] == 0 and not r["aborted"] and r["verdict"] == "OK"


def main():
    global SAMPLER
    results["started"] = now()
    results["options"] = OPTS
    stages = [("vram", OPTS["vram"], stage_vram), ("ops", OPTS["ops"] != "skip", stage_ops),
              ("model", OPTS["model"], stage_model), ("burn", OPTS["burn_seconds"] > 0, stage_burn)]
    for name, enabled, fn in stages:
        if not enabled:
            continue
        ok, reason = driver_state()
        if not ok:
            results["stopped"] = f"before {name}: {reason}"
            log(f"not starting {name}: {reason}")
            publish("not ready")
            return
        if SAMPLER is None:   # only once the driver says the GPU answers: NVML can block on a lost GPU
            SAMPLER = Sampler()
            results["sampler"] = {"hz": SAMPLE_HZ, "error": SAMPLER.error}
        t = gpu_temp()
        if t is None or t > OPTS["start_temp_c"]:
            results["stopped"] = f"before {name}: GPU temperature {t} C (start at <= {OPTS['start_temp_c']} C)"
            log(results["stopped"])
            publish("not ready")
            return
        passed = fn()
        alive = after_stage(name)
        results["stages"][name]["passed"] = passed
        log(f"== {name}: {'PASS' if passed else 'FAIL'}; GPU after: {'ok' if alive else 'NOT OK'}")
        if not alive:
            results["stopped"] = f"GPU fault during {name}: power the host off fully before any GPU use"
            publish(f"fault in {name}")
            return
        if not passed:
            results["stopped"] = f"{name} failed"
            publish(f"failed {name}")
            return
    results["finished"] = now()
    publish("all passed")
    log("all stages passed")


if __name__ == "__main__":
    main()
