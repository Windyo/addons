# Changelog

## 0.1.1

- Fix: check that the GPU can actually be opened before using it (Protection mode on → keeps
  waiting and says why, instead of failing inside Ollama).

## 0.1.0

- First version: Ollama 0.40.11 (cuda_v12 runner only, no cuDNN) on the GPU the NVIDIA Driver
  add-on loaded. Waits for the GPU, pulls and warms up the model with keep_alive -1, checks it is
  fully in VRAM, reports `sensor.ollama_cuda_status`.
- Thermal gate on port 11434: chats get 503 while the driver add-on reports the GPU hot.
- Optional benchmark (FA off/on, KV f16/q8_0, Qwen3-8B reference) → `sensor.ollama_cuda_benchmark`.
