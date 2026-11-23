# Changelog

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
