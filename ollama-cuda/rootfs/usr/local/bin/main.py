#!/usr/bin/env python3
"""Ollama CUDA add-on: a local LLM on the GPU the NVIDIA Driver add-on loaded.

1. Waits for a usable GPU (/share/nvidia/state.json, no timeout) and checks the driver version.
2. Serves `ollama serve` on 127.0.0.1:11435 behind a thermal gate on :11434: while the driver
   add-on reports the GPU hot, /api/chat and /api/generate get 503 (HA then reports the agent
   unavailable) instead of heating the card further. Everything else passes through.
3. Pulls the model if missing, warms it up with keep_alive -1, checks it sits fully in VRAM.
4. Publishes sensor.ollama_cuda_status every minute.
Option `benchmark`: runs the phase-3 matrix first (bench.py) and publishes sensor.ollama_cuda_benchmark.
"""
import http.client
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

STATE_FILE = "/share/nvidia/state.json"
NV_VERSION = open("/opt/nvidia/VERSION").read().strip()
UPSTREAM = ("127.0.0.1", 11435)
LISTEN = ("0.0.0.0", 11434)
GATED = ("/api/chat", "/api/generate")
# Local thermal fallback while state.json is stale (driver add-on stopped): same thresholds.
HOT_C, COOL_C = 85, 78
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authenticate", "proxy-authorization"}

OPTS = json.load(open("/data/options.json"))
BOOT = open("/proc/sys/kernel/random/boot_id").read().strip()
# A GPU fault in this boot (CUDA error in the runner, or a failed test generation). After one, the
# card is not trusted again until a host reboot: on 2026-10-25 and 2026-11-22 the job after a first
# fault hung the GPU, and in October that ended with the whole host down.
FAULT_FILE = "/data/gpu_fault.json"

status = {"state": "starting", "thermal": "ok", "thermal_source": None, "refused_hot": 0,
          "last_chat_ms": None, "last_chat_at": None, "model": OPTS["model"]}
lock = threading.Lock()
ollama = None
stopping = False


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publish(entity, state, attrs):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    req = urllib.request.Request(
        f"http://supervisor/core/api/states/{entity}", method="POST",
        data=json.dumps({"state": state, "attributes": attrs}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError as e:
        log(f"WARNING: could not publish {entity}: {e}")


def publish_status():
    with lock:
        s = dict(status)
    s.update({"friendly_name": "Ollama CUDA status", "icon": "mdi:robot-outline", "updated": now()})
    publish("sensor.ollama_cuda_status", s.pop("state"), s)


def contract():
    """-> (usable: bool, reason, state dict or None). Same rules as every GPU consumer."""
    try:
        loaded = open("/sys/module/nvidia/version").read().strip()
    except OSError:
        return False, "driver not loaded", None
    if loaded != NV_VERSION:
        return False, f"driver_mismatch: kernel {loaded}, userspace {NV_VERSION}", None
    try:
        st = json.load(open(STATE_FILE))
        age = time.time() - datetime.strptime(st["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (OSError, ValueError, KeyError):
        return False, f"no readable {STATE_FILE}", None
    if st.get("boot_id") != BOOT or age >= 60:
        return False, "state.json stale or from another boot", None
    if st.get("state") != "ready" or not os.path.exists("/dev/nvidia-uvm"):
        return False, f"driver add-on: {st.get('state')}", st
    # Only when the driver says ready (opening a hung GPU can block): the nodes exist host-wide,
    # but this container may not be allowed to open them. The Supervisor grants full_access only
    # while Protection mode is off, when the container is created.
    try:
        subprocess.run(["python3", "-c", "import os; os.close(os.open('/dev/nvidiactl', os.O_RDWR))"],
                       check=True, timeout=5, capture_output=True)
    except (subprocess.SubprocessError, OSError):
        return False, "cannot open /dev/nvidiactl (Protection mode on?)", st
    return True, "ready", st


def thermal_watch():
    """Keeps status['thermal'] current: from the driver add-on, or nvidia-smi if that's stale."""
    while not stopping:
        usable, _, st = contract()
        if st is not None and usable:
            t, src = st.get("thermal", "ok"), "driver"
        else:
            t, src = status["thermal"], "local"
            try:
                out = subprocess.run(["nvidia-smi", "--format=csv,noheader,nounits",
                                      "--query-gpu=temperature.gpu,clocks_event_reasons.active"],
                                     capture_output=True, text=True, timeout=10).stdout
                temp, reasons = [x.strip() for x in out.split(",")]
                hw = int(reasons, 16) & 0x40
                if t == "ok" and (int(temp) >= HOT_C or hw):
                    t = "hot"
                elif t == "hot" and int(temp) <= COOL_C and not hw:
                    t = "ok"
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        with lock:
            if t != status["thermal"]:
                log(f"thermal: {status['thermal']} -> {t} ({src})")
            status["thermal"], status["thermal_source"] = t, src
        time.sleep(5)


def cap_answer(body):
    """Gives a chat a maximum answer length unless the client set one. A small model can loop:
    on 2026-11-23 one benchmark answer was still generating after 5 minutes, at full GPU load."""
    try:
        req = json.loads(body)
    except ValueError:
        return body
    if not isinstance(req, dict):
        return body
    opts = req.setdefault("options", {})
    if isinstance(opts, dict) and "num_predict" not in opts:
        opts["num_predict"] = OPTS["max_answer_tokens"]
    return json.dumps(req).encode()


class Gate(http.server.BaseHTTPRequestHandler):
    """Streaming reverse proxy to Ollama, refusing chats while the GPU is hot."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy(self):
        path = self.path.split("?")[0]
        gated = self.command == "POST" and path in GATED
        # Read the body first, even for a refusal: leftovers would corrupt the next request on a
        # kept-alive connection.
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        if gated and status["state"] not in ("ready", "partial_offload"):
            with lock:
                status["refused_unavailable"] = status.get("refused_unavailable", 0) + 1
            return self._reply(503, {"error": f"local LLM unavailable ({status['state']})"})
        if gated and status["thermal"] == "hot":
            with lock:
                status["refused_hot"] += 1
            return self._reply(503, {"error": "GPU too hot, try again in a minute"})
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"host"}}
        if gated and body:
            body = cap_answer(body)
            headers = {k: v for k, v in headers.items() if k.lower() != "content-length"}
            headers["Content-Length"] = str(len(body))
        t0 = time.monotonic()
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=600)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            up = conn.getresponse()
        except OSError:
            conn.close()
            return self._reply(502, {"error": "ollama is not up yet"})
        try:
            self.send_response(up.status)
            for k, v in up.getheaders():
                if k.lower() not in HOP and k.lower() != "content-length":
                    self.send_header(k, v)
            if self.command == "HEAD" or up.status in (204, 304):
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Re-chunk as it arrives: chat answers stream token by token.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while chunk := up.read1(65536):
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
            except (http.client.HTTPException, OSError) as e:
                # Ollama dropped the stream mid-answer: its runner died. End this reply (no clean
                # terminator, so the client sees a failure) and let the fault check decide.
                self.close_connection = True
                if gated:
                    threading.Thread(target=check_runner, args=(f"stream cut: {type(e).__name__}",),
                                     daemon=True).start()
                return
            self.wfile.write(b"0\r\n\r\n")
        finally:
            conn.close()
        if gated and up.status == 200:
            with lock:
                status["last_chat_ms"] = round((time.monotonic() - t0) * 1000)
                status["last_chat_at"] = now()
        elif gated and up.status >= 500:
            # Ollama answers 500 when the runner died (e.g. "CUDA error: an illegal instruction").
            threading.Thread(target=check_runner, args=(f"chat returned HTTP {up.status}",), daemon=True).start()

    do_GET = do_POST = do_DELETE = do_HEAD = do_PUT = _proxy


def select_library():
    """Points the cuda_v12 runner at the chosen ggml CUDA library (option cuda_library).
    release: the one in Ollama's release (kernels for 13 GPU generations). On the GTX 1070 it hit
    Xid 13 "Illegal Instruction Encoding" and dropped off the bus on the first prompt batch
    (2026-11-22/23). sm61: the same source and build preset, compiled for sm_61 only, in CI."""
    choice = OPTS["cuda_library"]
    target = "/usr/lib/ollama/cuda_v12/libggml-cuda.so"
    tmp = target + ".new"
    # A copy, not a symlink: the library finds its cuBLAS next to it through $ORIGIN.
    shutil.copyfile(f"/opt/ggml/{choice}/libggml-cuda.so", tmp)
    os.replace(tmp, target)
    log(f"ggml CUDA library: {choice} ({open(f'/opt/ggml/{choice}/VERSION').read().strip()})")
    with lock:
        status["cuda_library"] = choice


def ollama_env():
    env = dict(os.environ)
    env.update({
        "OLLAMA_HOST": f"{UPSTREAM[0]}:{UPSTREAM[1]}",
        "OLLAMA_MODELS": "/data/models",
        "OLLAMA_LLM_LIBRARY": "cuda_v12",
        "OLLAMA_VULKAN": "0",
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_KEEP_ALIVE": "-1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_CONTEXT_LENGTH": str(OPTS["context_length"]),
        # Room left for Whisper (it may load after the LLM, e.g. when it returns from the CPU).
        "OLLAMA_GPU_OVERHEAD": str(OPTS["whisper_reserve_mib"] * 1024 * 1024),
        "OLLAMA_KV_CACHE_TYPE": OPTS["kv_cache_type"],
    })
    if not OPTS["cuda_fusion"]:
        # llama.cpp's fused CUDA kernels (several ops in one launch, used when generating one token
        # at a time). On the GTX 1070 the first generated token faulted (2026-11-23: Xid 13 + Xid 31
        # MMU read fault) while prompt processing passed.
        env["GGML_CUDA_DISABLE_FUSION"] = "1"
    if OPTS.get("cuda_launch_blocking"):
        # Diagnostic: each CUDA call waits for its kernel, so a fault names the kernel that caused it.
        env["CUDA_LAUNCH_BLOCKING"] = "1"
    fa = OPTS["flash_attention"]
    if fa != "auto":
        env["OLLAMA_FLASH_ATTENTION"] = "1" if fa == "on" else "0"
    return env


def start_ollama(env=None):
    global ollama
    ollama = subprocess.Popen(["ollama", "serve"], env=env or ollama_env())
    for _ in range(120):
        try:
            api("GET", "/api/version")
            return
        except OSError:
            time.sleep(1)
    raise RuntimeError("ollama serve did not answer in 120 s")


def stop_ollama():
    if ollama and ollama.poll() is None:
        ollama.terminate()
        try:
            ollama.wait(30)
        except subprocess.TimeoutExpired:
            ollama.kill()


def api(method, path, body=None, timeout=30):
    conn = http.client.HTTPConnection(*UPSTREAM, timeout=timeout)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        if r.status >= 400:
            raise OSError(f"{method} {path}: HTTP {r.status} {data[:300]!r}")
        return json.loads(data.splitlines()[-1]) if data else {}
    finally:
        conn.close()


def ensure_model(model):
    names = [m["name"] for m in api("GET", "/api/tags").get("models", [])]
    if model not in names:
        set_state("pulling")
        log(f"pulling {model}")
        res = api("POST", "/api/pull", {"model": model, "stream": False}, timeout=3600)
        log(f"pull: {res.get('status')}")


def load_fault():
    try:
        f = json.load(open(FAULT_FILE))
        return f if f.get("boot_id") == BOOT else None
    except (OSError, ValueError):
        return None


def gpu_fault(reason):
    """Stop using the GPU for the rest of this boot."""
    if load_fault():
        return
    f = {"boot_id": BOOT, "at": now(), "reason": reason[:500]}
    with open(FAULT_FILE, "w") as fh:
        json.dump(f, fh)
    log(f"GPU FAULT: {reason}. Not using the GPU again until the host reboots.")
    with lock:
        status["fault"] = f
    set_state("gpu_fault")
    stop_ollama()


def check_runner(why):
    """After a failed chat: a test generation decides between a one-off error and a GPU fault."""
    try:
        test_generation(OPTS["model"])
    except Exception as e:
        gpu_fault(f"{why}; test generation failed: {e}")


def test_generation(model):
    """One real token: proves the runner can compute, not just load.
    The first token after a cold load took over 60 s with cuda_launch_blocking (2026-11-23)."""
    t0 = time.monotonic()
    r = api("POST", "/api/generate", {"model": model, "prompt": "Say OK.", "stream": False,
                                      "keep_alive": -1, "options": {"num_predict": 1}}, timeout=300)
    if not r.get("done"):
        raise OSError(f"no complete answer: {str(r)[:200]}")
    log(f"test generation: {time.monotonic() - t0:.1f} s")


def warm_up(model):
    t0 = time.monotonic()
    api("POST", "/api/generate", {"model": model, "keep_alive": -1}, timeout=600)
    test_generation(model)
    ps = api("GET", "/api/ps").get("models", [])
    m = next((x for x in ps if x.get("name") == model), {})
    size, vram = m.get("size", 0), m.get("size_vram", 0)
    with lock:
        status.update({"load_s": round(time.monotonic() - t0, 1), "model_size_mib": size >> 20,
                       "model_vram_mib": vram >> 20, "context_length": m.get("context_length")})
    return size > 0 and size == vram


def set_state(s):
    with lock:
        changed = status["state"] != s
        status["state"] = s
    if changed:
        log(f"status: {s}")
        publish_status()


def shutdown(*_):
    global stopping
    stopping = True
    stop_ollama()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log(f"Ollama CUDA: model {OPTS['model']}, ctx {OPTS['context_length']}, FA {OPTS['flash_attention']}, "
        f"KV {OPTS['kv_cache_type']}, reserve {OPTS['whisper_reserve_mib']} MiB, "
        f"CUDA library {OPTS['cuda_library']}, fusion {'on' if OPTS['cuda_fusion'] else 'off'}")

    # The gate listens from the start (502 until Ollama is up), so the watchdog sees a live port.
    server = http.server.ThreadingHTTPServer(LISTEN, Gate)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 1. Wait for the GPU. No CPU serving: 4 slow cores would make every answer take 10+ s.
    last = None
    while True:
        usable, reason, _ = contract()
        if usable:
            break
        if reason != last:
            log(f"waiting for the GPU: {reason}")
            with lock:
                status["reason"] = reason
            last = reason
        set_state("waiting_gpu")
        time.sleep(15)
    with lock:
        status.pop("reason", None)

    threading.Thread(target=thermal_watch, daemon=True).start()
    select_library()

    if OPTS.get("benchmark"):
        set_state("benchmark")
        subprocess.run([sys.executable, "/usr/local/bin/bench.py"], check=False)

    fault = load_fault()
    if fault:
        log(f"GPU fault earlier in this boot ({fault['reason']}); not starting Ollama until a reboot")
        with lock:
            status["fault"] = fault
        set_state("gpu_fault")
        while True:
            time.sleep(60)
            publish_status()

    # 2-3. Serve, pull, warm up (with one real token), verify full offload.
    set_state("loading")
    start_ollama()
    ensure_model(OPTS["model"])
    try:
        full = warm_up(OPTS["model"])
    except Exception as e:
        gpu_fault(f"warm-up failed: {e}")
        while True:
            time.sleep(60)
            publish_status()
    set_state("ready" if full else "partial_offload")
    if not full:
        log("WARNING: the model is not fully in VRAM; answers will be slow")

    # 4. Status every minute; restart ollama serve if it dies.
    while True:
        for _ in range(12):
            time.sleep(5)
            if status["state"] == "gpu_fault":
                continue
            usable, reason, st = contract()
            if st is not None and st.get("state") == "gpu_error":
                gpu_fault("driver add-on reports gpu_error")
                continue
            if ollama.poll() is not None:
                log(f"ollama serve exited ({ollama.returncode}), restarting")
                set_state("loading")
                start_ollama()
                try:
                    set_state("ready" if warm_up(OPTS["model"]) else "partial_offload")
                except Exception as e:
                    gpu_fault(f"warm-up after restart failed: {e}")
        publish_status()


if __name__ == "__main__":
    main()
