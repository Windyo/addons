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
    # The nodes exist host-wide, but this container may not be allowed to open them: the Supervisor
    # grants full_access only while Protection mode is off, when the container is created.
    try:
        os.close(os.open("/dev/nvidiactl", os.O_RDWR))
    except OSError as e:
        return False, f"cannot open /dev/nvidiactl ({e.strerror}; Protection mode on?)", None
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
        if gated and status["thermal"] == "hot":
            with lock:
                status["refused_hot"] += 1
            return self._reply(503, {"error": "GPU too hot, try again in a minute"})
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP | {"host"}}
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
            while chunk := up.read1(65536):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        finally:
            conn.close()
        if gated and up.status == 200:
            with lock:
                status["last_chat_ms"] = round((time.monotonic() - t0) * 1000)
                status["last_chat_at"] = now()

    do_GET = do_POST = do_DELETE = do_HEAD = do_PUT = _proxy


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


def warm_up(model):
    t0 = time.monotonic()
    api("POST", "/api/generate", {"model": model, "keep_alive": -1}, timeout=600)
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
        f"KV {OPTS['kv_cache_type']}, reserve {OPTS['whisper_reserve_mib']} MiB")

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

    if OPTS.get("benchmark"):
        set_state("benchmark")
        subprocess.run([sys.executable, "/usr/local/bin/bench.py"], check=False)

    # 2-3. Serve, pull, warm up, verify full offload.
    set_state("loading")
    start_ollama()
    ensure_model(OPTS["model"])
    full = warm_up(OPTS["model"])
    set_state("ready" if full else "partial_offload")
    if not full:
        log("WARNING: the model is not fully in VRAM; answers will be slow")

    # 4. Status every minute; restart ollama serve if it dies.
    while True:
        for _ in range(12):
            time.sleep(5)
            if ollama.poll() is not None:
                log(f"ollama serve exited ({ollama.returncode}), restarting")
                set_state("loading")
                start_ollama()
                set_state("ready" if warm_up(OPTS["model"]) else "partial_offload")
        publish_status()


if __name__ == "__main__":
    main()
