# Changelog

## 0.2.0

- New `model` stage (before `burn`): llama-bench on the real Qwen3-4B Q4_K_M (downloaded once, sha256
  checked) with prompt batches of 32/256/512 tokens and 32 generated tokens, flash attention off.

## 0.1.0

- First version: VRAM pattern test, llama.cpp `test-backend-ops` (b11845, sm_61) on CUDA0, gpu-burn,
  in that order, stopping at the first failure. `sensor.gpu_diag_status`, `/data/results.json`.
