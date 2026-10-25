# Changelog

## 0.2.5

- Accuracy-first candidates: large-v3 and the French fine-tune bofenghuang/whisper-large-v3-french
  (CTranslate2 weights in its `ctranslate2/` subfolder), both beam 5 + names + VAD, against turbo.
- Model spec `hf:owner/repo#subfolder` for the server and the benchmark; per-config language filter.

## 0.2.4

- Thermal guard: GPU >= 85 C or hardware thermal slowdown -> serve from the CPU until <= 72 C.
  (A sustained benchmark reached 97 C, throttled to 139 MHz, then Xid 13/62/154/79: GPU fell off the bus.)
- Defaults from the benchmark: beam 1 + HA name biasing (beam-5 accuracy at beam-1 cost) + VAD filter
  (silence no longer triggers six fallback re-decodes into junk).
- Benchmark waits for the GPU to cool (<= 65 C before a config, <= 70 C whenever it reaches 80 C).
- Removed the Piper GPU experiment: no gain for the French voice (0.092 s CPU vs 0.097 s GPU), and it
  was running when the GPU faulted. Piper stays on the CPU.

## 0.2.3

- Benchmark runs a chosen short list (`benchmark_configs`, default 3 candidates) instead of everything.
- Logs progress every 20 clips with GPU clock/max clock, temperature, power and throttle reasons;
  counts faster-whisper temperature fallbacks (full re-decodes) per config; lists the slowest clips.
- Status sensor updates live during a benchmark (`benchmark_running`, `throttle_reasons`).
- Piper GPU experiment behind its own switch (`benchmark_piper_gpu`). Earlier results are archived.

## 0.2.2

- Benchmark also measures Piper on the GPU vs the CPU (`bench_piper_gpu.sh`): onnxruntime-gpu 1.26.0
  (last CUDA 12 build, sm_60 SASS) + cuDNN 9.10.2 (last with Pascal), in its own venv under /data.

## 0.2.1

- Pin PyAV 18.1.0: PyAV 19 removed `open(metadata_errors=)`, which faster-whisper 1.2.1 passes
  (caught by the CI benchmark smoke test before release).

## 0.2.0

- Phase 2: Wyoming faster-whisper 3.8.1 server on port 10300, announced to Home Assistant.
  CUDA through CTranslate2 4.8.3 + cuBLAS 12.9 (both ship Pascal sm_61 code; no cuDNN needed).
- Automatic CPU fallback whenever the GPU stage fails, so speech-to-text keeps working.
- Options: model, compute_type, beam_size, ha_name_biasing, cpu_fallback_model, benchmark, debug.
- Benchmark (option): Piper-synthesised French/English commands for this house, clean and noisy,
  across models, compute types, beam sizes and name biasing; results in sensor.whisper_cuda_benchmark.
- Full Python dependency lock (requirements.lock).

## 0.1.2

- Create `/dev/nvidia*` through a private devtmpfs mount: the Supervisor gives every add-on a
  read-only bind of the host's `/dev`, so `mknod /dev/nvidiactl` failed. Adds `SYS_ADMIN`.
- First load on the GTX 1070 (0.1.1) worked: `nvidia` and `nvidia_uvm` loaded cleanly on 6.18.52-haos.

## 0.1.1

- Apply HAOS's own kernel patches (fetched for the installed OS version, bundled 18.3 set as
  fallback). Without them the config drift gate rejected `CONFIG_IPV6_REACHABILITY_PROBE`.

## 0.1.0

- Rewrite. Phase 1: builds NVIDIA 580.196.07 (`nvidia`, `nvidia-uvm`) for the running HAOS
  kernel, verifies its vermagic, optionally loads it, reports `sensor.whisper_cuda_status`.
- Replaces the June 2025 attempt, which shipped CUDA libraries but never loaded a kernel driver.
