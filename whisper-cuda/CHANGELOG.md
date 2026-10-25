# Changelog

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
