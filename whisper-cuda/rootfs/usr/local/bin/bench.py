#!/opt/venv/bin/python3
"""Benchmark Whisper configurations on this machine's GPU (and CPU for reference).

Test speech is synthesised with Piper: Home Assistant's own voices (fr_FR-siwis-medium,
en_US-libritts-high) plus a second voice per language, each phrase clean and with noise
at 15 dB SNR, plus noise-only clips to catch hallucinations. The phrases are real commands
for this house (its areas, French aliases, media and list intents).

Writes /data/bench/results.json and prints a summary plus every wrong transcript.
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
import unicodedata
import urllib.request
import wave
from pathlib import Path

import numpy as np

BENCH = Path("/data/bench")
VOICES = BENCH / "voices"
CLIPS = BENCH / "clips"
MODELS = Path("/data/models")

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE_PATHS = {
    "fr_FR-siwis-medium": "fr/fr_FR/siwis/medium",
    "fr_FR-tom-medium": "fr/fr_FR/tom/medium",
    "en_US-libritts-high": "en/en_US/libritts/high",
    "en_GB-alan-medium": "en/en_GB/alan/medium",
}
LANG_VOICES = {"fr": ["fr_FR-siwis-medium", "fr_FR-tom-medium"],
               "en": ["en_US-libritts-high", "en_GB-alan-medium"]}

PHRASES = {
    "fr": [
        "Allume la lumière de la cuisine.",
        "Éteins toutes les lumières du salon.",
        "Mets le bureau à vingt et un degrés.",
        "Quelle est la température dans la salle de piano ?",
        "Arrête la musique.",
        "Règle le volume du salon télé à trente pour cent.",
        "Allume le ventilateur de la chambre.",
        "Ferme les stores du séjour.",
        "Mets Daft Punk sur la chambre.",
        "Est-ce que la porte d'entrée est ouverte ?",
        "Allume la lumière de la buanderie.",
        "Éteins la lumière du cellier.",
        "Quelle heure est-il ?",
        "Bonne nuit.",
        "Morceau suivant.",
        "Ajoute du lait à la liste de courses.",
    ],
    "en": [
        "Turn on the kitchen lights.",
        "Turn off all the lights in the living room.",
        "Set the office to twenty one degrees.",
        "What's the temperature in the piano room?",
        "Stop the music.",
        "Set the TV volume to thirty percent.",
        "Turn on the bedroom fan.",
        "Is the front door open?",
        "Turn off the laundry room light.",
        "What time is it?",
        "Good night.",
        "Add milk to the shopping list.",
    ],
}

NUMBERS = {
    "fr": [("vingt et un", "21"), ("vingt-et-un", "21"), ("trente", "30"), ("vingt", "20"),
           ("pour cent", "pourcent"), ("%", " pourcent")],
    "en": [("twenty one", "21"), ("twenty-one", "21"), ("thirty", "30"), ("twenty", "20"),
           ("per cent", "percent"), ("%", " percent")],
}


def log(msg: str) -> None:
    print(f"[bench {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize(text: str, lang: str) -> list[str]:
    t = unicodedata.normalize("NFC", text).lower().replace("’", "'")
    for a, b in NUMBERS[lang]:
        t = t.replace(a, b)
    t = re.sub(r"[^\w'%\s-]", " ", t).replace("-", " ")
    t = re.sub(r"(\d)\s+%", r"\1%", t)
    return t.split()


def wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
            prev, d[j] = d[j], cur
    return d[len(hyp)] / max(1, len(ref))


# ---------------------------------------------------------------------------------------------
def ensure_voices() -> None:
    VOICES.mkdir(parents=True, exist_ok=True)
    for name, path in VOICE_PATHS.items():
        for ext in (".onnx", ".onnx.json"):
            dst = VOICES / f"{name}{ext}"
            if not dst.exists():
                log(f"downloading voice {name}{ext}")
                urllib.request.urlretrieve(f"{HF}/{path}/{name}{ext}", dst)


def make_clips() -> tuple[list[dict], dict]:
    """Synthesise every phrase per voice (clean + noisy) and a few noise-only clips."""
    from piper import PiperVoice

    CLIPS.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1234)
    clips, tts_times = [], {}
    for lang, voices in LANG_VOICES.items():
        for vname in voices:
            voice = PiperVoice.load(str(VOICES / f"{vname}.onnx"))
            times = []
            for i, text in enumerate(PHRASES[lang]):
                clean = CLIPS / f"{vname}-{i:02d}-clean.wav"
                t0 = time.perf_counter()
                with wave.open(str(clean), "wb") as w:
                    voice.synthesize_wav(text, w)
                times.append(time.perf_counter() - t0)
                with wave.open(str(clean), "rb") as r:
                    rate, pcm = r.getframerate(), np.frombuffer(r.readframes(r.getnframes()), np.int16)
                # 300 ms of silence around the speech, like a satellite's capture.
                pad = np.zeros(int(rate * 0.3), np.int16)
                pcm = np.concatenate([pad, pcm, pad]).astype(np.float32)
                write_wav(clean, rate, pcm)
                p_sig = np.mean(pcm ** 2)
                noise = rng.normal(0, np.sqrt(p_sig / 10 ** (15 / 10)), pcm.shape)
                noisy = CLIPS / f"{vname}-{i:02d}-noisy.wav"
                write_wav(noisy, rate, pcm + noise)
                for kind, path in (("clean", clean), ("noisy", noisy)):
                    clips.append({"lang": lang, "voice": vname, "kind": kind, "text": text,
                                  "path": str(path), "seconds": len(pcm) / rate})
            tts_times[vname] = {"mean_s": round(statistics.mean(times), 3),
                                "max_s": round(max(times), 3)}
            del voice
    for i in range(3):
        path = CLIPS / f"silence-{i}.wav"
        write_wav(path, 16000, rng.normal(0, 300, 16000 * 3))
        clips.append({"lang": "fr" if i % 2 == 0 else "en", "voice": "-", "kind": "silence",
                      "text": "", "path": str(path), "seconds": 3.0})
    return clips, tts_times


def write_wav(path: Path, rate: int, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def vram_used_mib() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


class FallbackCounter(logging.Handler):
    """Counts faster-whisper's temperature fallbacks (each one is a full re-decode)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "threshold is not met" in record.getMessage():
            self.count += 1


FALLBACKS = FallbackCounter()
_fw_log = logging.getLogger("faster_whisper")
_fw_log.setLevel(logging.DEBUG)
_fw_log.propagate = False
_fw_log.addHandler(FALLBACKS)


def gpu_telemetry() -> str | None:
    """SM clock vs max, temperature, power, P-state and active throttle reasons."""
    q = "clocks.sm,clocks.max.sm,temperature.gpu,power.draw,pstate,clocks_event_reasons.active"
    try:
        r = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return repr(exc)


def gpu_temp() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def cool_down(limit: int, target: int, why: str) -> float:
    """Wait while the GPU is at/above `limit` until it is at/below `target`. Returns seconds waited.

    This laptop cannot shed sustained GPU heat: at ~93 C it throttles to 139 MHz and an earlier run
    ended with the GPU falling off the bus. Gating keeps it cool and the latencies honest."""
    t = gpu_temp()
    if t is None or t < limit:
        return 0.0
    log(f"  cooling ({why}): GPU {t} C, waiting for <= {target} C")
    start = time.perf_counter()
    while True:
        time.sleep(5)
        t = gpu_temp()
        if t is None or t <= target:
            break
        if time.perf_counter() - start > 1800:
            raise RuntimeError(f"GPU still {t} C after 30 min of cooling")
    return time.perf_counter() - start


def ha_prompt(model) -> str | None:
    """The same biasing prompt the server builds from Home Assistant's exposed names."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    from wyoming_faster_whisper.hass_api import HomeAssistant

    ctx = asyncio.run(HomeAssistant(token, api_url="http://supervisor/core/api").get_context())

    def count(text: str) -> int:
        return len(model.hf_tokenizer.encode(text, add_special_tokens=False).ids)

    return ctx.whisper_prompt(count, max_tokens=200)


# ---------------------------------------------------------------------------------------------
def resolve_model(spec: str) -> str:
    """'hf:owner/repo#subfolder' -> local path of that subfolder (CTranslate2 weights published
    inside a larger repo, e.g. bofenghuang/whisper-large-v3-french#ctranslate2). Anything else is
    passed to faster-whisper unchanged (a size name or a repo id)."""
    if not spec.startswith("hf:"):
        return spec
    from huggingface_hub import snapshot_download

    repo, _, sub = spec[3:].partition("#")
    root = snapshot_download(repo, allow_patterns=[f"{sub}/*"] if sub else None, cache_dir=str(MODELS))
    return str(Path(root) / sub) if sub else root


def run_config(cfg: dict, clips: list[dict]) -> dict:
    from faster_whisper import WhisperModel

    log(f"=== {cfg['name']}")
    if cfg.get("langs"):
        clips = [c for c in clips if c["lang"] in cfg["langs"]]
    t0 = time.perf_counter()
    model = WhisperModel(resolve_model(cfg["model"]), device=cfg["device"], compute_type=cfg["compute_type"],
                         cpu_threads=cfg.get("threads", 4), download_root=str(MODELS))
    load_s = time.perf_counter() - t0
    vram = vram_used_mib() if cfg["device"] == "cuda" else None
    prompt = ha_prompt(model) if cfg.get("prompt") else None
    if cfg.get("prompt"):
        log(f"prompt ({len(prompt or '')} chars): {(prompt or '')[:300]}")

    def transcribe(clip: dict) -> tuple[str, float, int]:
        before = FALLBACKS.count
        t = time.perf_counter()
        segs, _ = model.transcribe(clip["path"], language=clip["lang"], beam_size=cfg["beam"],
                                   initial_prompt=prompt, vad_filter=bool(cfg.get("vad")))
        text = " ".join(s.text for s in segs).strip()
        return text, time.perf_counter() - t, FALLBACKS.count - before

    for c in clips[:2]:  # warm-up (CUDA context, kernels)
        transcribe(c)

    gpu = cfg["device"] == "cuda"
    cooled = cool_down(78, 75, "before config") if gpu else 0.0
    telemetry = {"start": gpu_telemetry() if gpu else None}
    log(f"  gpu: {telemetry['start']}")
    rows = []
    t_run = time.perf_counter()
    for idx, c in enumerate(clips, 1):
        if gpu:
            cooled += cool_down(87, 80, "between clips")
        text, dt, fb = transcribe(c)
        if idx % 20 == 0:
            log(f"  {idx}/{len(clips)} clips, {time.perf_counter() - t_run:.0f}s"
                + (f", gpu: {gpu_telemetry()}" if gpu else ""))
        if idx == len(clips) // 2 and gpu:
            telemetry["mid"] = gpu_telemetry()
        ref = normalize(c["text"], c["lang"])
        hyp = normalize(text, c["lang"])
        rows.append({**c, "hyp": text, "latency_s": dt, "fallbacks": fb,
                     "wer": wer(ref, hyp) if c["kind"] != "silence" else None,
                     "exact": (ref == hyp) if c["kind"] != "silence" else None,
                     "halluc_words": len(hyp) if c["kind"] == "silence" else 0})

    def agg(sel):
        rs = [r for r in rows if sel(r)]
        if not rs:  # e.g. a French-only config has no English clips
            return None
        return {"n": len(rs), "wer_pct": round(100 * statistics.mean(r["wer"] for r in rs), 1),
                "exact_pct": round(100 * statistics.mean(r["exact"] for r in rs), 1)}

    lat = sorted(r["latency_s"] for r in rows if r["kind"] != "silence")
    res = {
        "name": cfg["name"], "model": cfg["model"], "device": cfg["device"],
        "compute_type": cfg["compute_type"], "beam": cfg["beam"], "prompt": bool(prompt),
        "vad": bool(cfg.get("vad")),
        "load_s": round(load_s, 1), "vram_mib": vram,
        "latency_mean_s": round(statistics.mean(lat), 3),
        "latency_p50_s": round(lat[len(lat) // 2], 3),
        "latency_p90_s": round(lat[int(len(lat) * 0.9)], 3),
        "latency_max_s": round(lat[-1], 3),
        "fr_clean": agg(lambda r: r["lang"] == "fr" and r["kind"] == "clean"),
        "fr_noisy": agg(lambda r: r["lang"] == "fr" and r["kind"] == "noisy"),
        "en_clean": agg(lambda r: r["lang"] == "en" and r["kind"] == "clean"),
        "en_noisy": agg(lambda r: r["lang"] == "en" and r["kind"] == "noisy"),
        "silence_halluc_words": sum(r["halluc_words"] for r in rows if r["kind"] == "silence"),
        "fallbacks_total": sum(r["fallbacks"] for r in rows),
        "clips_with_fallback": sum(1 for r in rows if r["fallbacks"]),
        "run_s": round(time.perf_counter() - t_run, 1),
        "cooling_wait_s": round(cooled, 1),
        "gpu": dict(telemetry, end=gpu_telemetry() if gpu else None),
        "slowest": [{"text": r["text"], "kind": r["kind"], "voice": r["voice"],
                     "latency_s": round(r["latency_s"], 2), "fallbacks": r["fallbacks"], "hyp": r["hyp"][:120]}
                    for r in sorted(rows, key=lambda r: -r["latency_s"])[:3]],
        "misses": [f"[{r['lang']} {r['voice']} {r['kind']}] {r['text']} -> {r['hyp'][:80]}"
                   for r in rows if (r["kind"] != "silence" and not r["exact"]) or r["halluc_words"]],
    }
    log(json.dumps(res, ensure_ascii=False))
    for r in rows:
        if (r["kind"] != "silence" and not r["exact"]) or r["halluc_words"]:
            log(f"  miss [{r['lang']} {r['voice']} {r['kind']}] '{r['text']}' -> '{r['hyp']}'")
    del model
    gc.collect()
    return res


# Catalogue. Run a subset by name (option benchmark_configs); nothing runs by default.
GPU_CONFIGS = [
    {"name": "turbo int8 b1", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 1},
    {"name": "turbo int8 b3", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 3},
    {"name": "turbo int8 b5", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 5},
    {"name": "turbo int8 b1 +names", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 1, "prompt": True},
    {"name": "turbo int8 b3 +names", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 3, "prompt": True},
    {"name": "turbo int8 b1 +names +vad", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 1, "prompt": True, "vad": True},
    {"name": "turbo int8 b1 +vad", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 1, "vad": True},
    {"name": "turbo int8 b5 +names +vad", "model": "large-v3-turbo", "compute_type": "int8_float32", "beam": 5, "prompt": True, "vad": True},
    {"name": "large-v3 int8 b5 +names +vad", "model": "large-v3", "compute_type": "int8_float32", "beam": 5, "prompt": True, "vad": True},
    {"name": "fr-large-v3 int8 b5 +names +vad", "model": "hf:bofenghuang/whisper-large-v3-french#ctranslate2",
     "compute_type": "int8_float32", "beam": 5, "prompt": True, "vad": True, "langs": ["fr"]},
    {"name": "fr-distil int8 b5 +names +vad", "model": "hf:bofenghuang/whisper-large-v3-french-distil-dec16#ctranslate2",
     "compute_type": "int8_float32", "beam": 5, "prompt": True, "vad": True, "langs": ["fr"]},
    {"name": "turbo fp32 b1", "model": "large-v3-turbo", "compute_type": "float32", "beam": 1},
    {"name": "large-v3 int8 b1", "model": "large-v3", "compute_type": "int8_float32", "beam": 1},
    {"name": "medium int8 b1", "model": "medium", "compute_type": "int8_float32", "beam": 1},
]
CPU_CONFIGS = [
    {"name": "CPU small int8 b5", "model": "small", "compute_type": "int8", "beam": 5, "threads": 4},
    {"name": "CPU base int8 b5", "model": "base", "compute_type": "int8", "beam": 5, "threads": 4},
    {"name": "CPU base int8 b5 +vad", "model": "base", "compute_type": "int8", "beam": 5, "threads": 4, "vad": True},
]


def main() -> int:
    import ctranslate2

    only = [a.strip() for a in sys.argv[1:] if a.strip()]
    log(f"ctranslate2 {ctranslate2.__version__}, CUDA devices: {ctranslate2.get_cuda_device_count()}")
    gpu = ctranslate2.get_cuda_device_count() > 0
    if gpu:
        log(f"CUDA compute types: {sorted(ctranslate2.get_supported_compute_types('cuda'))}")
    prev = BENCH / "results.json"
    if prev.exists():  # keep earlier runs
        prev.rename(BENCH / time.strftime("results-%Y%m%d-%H%M%S.json", time.localtime(prev.stat().st_mtime)))
    ensure_voices()
    clips, tts = make_clips()
    log(f"{len(clips)} clips; Piper CPU synthesis per phrase: {json.dumps(tts)}")

    configs = [dict(c, device="cuda") for c in GPU_CONFIGS] if gpu else []
    configs += [dict(c, device="cpu") for c in CPU_CONFIGS]
    by_name = {c["name"]: c for c in configs}
    unknown = [n for n in only if n not in by_name]
    if unknown:
        log(f"unknown config(s) {unknown}; known: {list(by_name)}")
    configs = [by_name[n] for n in only if n in by_name]

    results = []
    for cfg in configs:
        try:
            results.append(run_config(cfg, clips))
        except Exception as exc:  # noqa: BLE001
            log(f"FAILED {cfg['name']}: {exc!r}")
            results.append({"name": cfg["name"], "error": repr(exc)})
        (BENCH / "results.json").write_text(json.dumps({"piper_cpu": tts, "results": results},
                                                       ensure_ascii=False, indent=1))
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
