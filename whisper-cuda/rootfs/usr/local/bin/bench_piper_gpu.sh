#!/usr/bin/env bash
# Experiment: Piper text-to-speech on the GPU vs the CPU, on this machine.
#
# The newest onnxruntime-gpu targets CUDA 13, which dropped Pascal, and cuDNN >= 9.11 dropped it too.
# So this pins the last CUDA 12 build (1.26.0, ships sm_60 SASS that runs on sm_61) with the
# last Pascal cuDNN (9.10.2, ships sm_61 SASS). They live in their own venv under /data so the
# image stays lean and the Whisper stack (CPU onnxruntime) is untouched.
# Writes /data/bench/piper_gpu.json.
set -euo pipefail
V=/data/bench/piper-gpu-venv
if [ ! -x "$V/bin/python" ] || [ ! -f "$V/.complete" ]; then
  echo "[piper-gpu] creating venv (first run downloads ~1.3 GB)"
  python3 -m venv "$V"
  "$V/bin/pip" install -q --no-cache-dir --only-binary=:all: \
    onnxruntime-gpu==1.26.0 nvidia-cudnn-cu12==9.10.2.21 nvidia-cublas-cu12==12.9.2.10 \
    "nvidia-cuda-runtime-cu12~=12.9" "nvidia-cufft-cu12~=11.4" "nvidia-curand-cu12~=10.3" \
    "nvidia-cuda-nvrtc-cu12~=12.9" numpy pathvalidate
  "$V/bin/pip" install -q --no-cache-dir --only-binary=:all: --no-deps piper-tts==1.8.0
  touch "$V/.complete"
fi
exec "$V/bin/python" - <<'EOF'
import json, statistics, sys, time
import onnxruntime as ort

ort.preload_dlls()  # load the CUDA/cuDNN libraries from the nvidia-* wheels
print("[piper-gpu] onnxruntime", ort.__version__, ort.get_available_providers(), flush=True)

from piper import PiperVoice  # noqa: E402

PHRASES = {
    "fr": ["Allume la lumière de la cuisine.", "C'est parti pour la télé !",
           "Il fait vingt et un degrés dans le bureau.", "Désolé, je n'ai pas compris.",
           "Bonne nuit. J'éteins les lumières et je ferme les stores."],
    "en": ["Turned on the kitchen lights.", "It is twenty one degrees in the office.",
           "Sorry, I couldn't understand that.", "Good night. Turning off the lights."],
}
VOICES = {"fr_FR-siwis-medium": "fr", "fr_FR-tom-medium": "fr",
          "en_US-libritts-high": "en", "en_GB-alan-medium": "en"}
out = {}
for name, lang in VOICES.items():
    path = f"/data/bench/voices/{name}.onnx"
    row = {}
    for dev in ("cpu", "cuda"):
        try:
            v = PiperVoice.load(path, use_cuda=(dev == "cuda"))
            providers = v.session.get_providers()
            for _ in range(3):  # warm-up
                list(v.synthesize(PHRASES[lang][0]))
            ts = []
            for _ in range(3):
                for p in PHRASES[lang]:
                    t = time.perf_counter()
                    list(v.synthesize(p))
                    ts.append(time.perf_counter() - t)
            row[dev] = {"mean_s": round(statistics.mean(ts), 4), "max_s": round(max(ts), 4),
                        "providers": providers}
        except Exception as exc:  # noqa: BLE001
            row[dev] = {"error": repr(exc)}
        print(f"[piper-gpu] {name} {dev}: {row[dev]}", flush=True)
    out[name] = row
json.dump(out, open("/data/bench/piper_gpu.json", "w"), indent=1)
print("[piper-gpu] done", flush=True)
EOF
