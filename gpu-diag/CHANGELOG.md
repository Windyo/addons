# Changelog

## 0.5.1

- Fix: the `prewarm_s` gpu-burn ran with 512 MB and died at start (it needs ~800 MB for its
  matrices), so llama-bench never ran and the stage showed as failed. It now uses `burn_mib`
  (default 2048), and a failed prewarm is logged as such.

## 0.5.0

- Clock timeline: NVML is sampled in-process 4 times a second (P-state, graphics and memory clocks,
  PCIe generation and width, power, temperature, utilization, clock event reasons). Each stage's
  result gets `clocks`: every change during the stage and the samples around the first error line;
  the changes are also logged. Every fault so far came from idle P8 within seconds of a job starting.
- The temperature cap reads those samples instead of starting `nvidia-smi` every 2 s (nvidia-smi
  stays as the fallback when NVML can't be opened).
- New option `prewarm_s` (default 0 = off): runs gpu-burn for that many seconds right before the
  `model` stage, so llama-bench starts with the card already out of P8. A/B test for the ramp.

## 0.4.0

- New option `vram_passes` (default 4): a long VRAM stream test (each pass ~50 ms; 3000 = ~3 minutes of
  full memory bandwidth). Token generation, which is memory-bound, faulted intermittently even with
  launch blocking, while compute-bound loads always passed.

## 0.3.0

- `model` stage: 64 generated tokens (was 32); `model_tests: tg` skips prompt processing;
  `model_env` passes llama.cpp/CUDA variables to llama-bench (e.g. `GGML_CUDA_DISABLE_FUSION=1`).
  On 2026-11-23 prompt processing passed and the first generated token faulted (Xid 13 + Xid 31).

## 0.2.0

- New `model` stage (before `burn`): llama-bench on the real Qwen3-4B Q4_K_M (downloaded once, sha256
  checked) with prompt batches of 32/256/512 tokens and 32 generated tokens, flash attention off.

## 0.1.0

- First version: VRAM pattern test, llama.cpp `test-backend-ops` (b11845, sm_61) on CUDA0, gpu-burn,
  in that order, stopping at the first failure. `sensor.gpu_diag_status`, `/data/results.json`.
