# Changelog

## 0.1.6

- The start line logs launch blocking and the answer cap too.

## 0.1.5

- Fix: the one-token test after loading timed out at 60 s with `cuda_launch_blocking` on (the first
  token after a cold load is slow); now 300 s, and its duration is logged.
- New option `max_answer_tokens` (default 400): chats without their own `num_predict` get this cap
  (the benchmark too). One benchmark answer was still generating after 5 minutes at full GPU load.

## 0.1.4

- New option `cuda_fusion` (default off: sets `GGML_CUDA_DISABLE_FUSION=1`). With llama.cpp's own
  sm_61 build the real model processed prompts fine but the first generated token faulted the GPU
  (Xid 13, Xid 31 MMU read fault); fusion is only used on that path.

## 0.1.3

- New option `cuda_library` (default `sm61`): the runner uses Ollama v0.40.11's ggml CUDA library
  rebuilt in CI from the same source and release preset for sm_61 only. The release's library
  (`release`) faulted the GTX 1070 on its first prompt batch (Xid 13, then off the bus).

## 0.1.2

- Fix: when Ollama's runner died mid-answer, the proxy crashed on the half-written stream
  (`IncompleteRead`). It now ends the reply as failed and runs the GPU fault check.
- Diagnostic option `cuda_launch_blocking`: a GPU fault then names the kernel that caused it.

## 0.1.1

- Fix: check that the GPU can actually be opened before using it (Protection mode on → keeps
  waiting and says why, instead of failing inside Ollama).
- GPU fault guard: the warm-up now generates one real token. A failed warm-up, a chat the runner
  fails (HTTP 5xx, then a failed test generation) or the driver add-on's `gpu_error` puts the
  add-on in `gpu_fault` for the rest of the boot: Ollama is stopped and chats get 503 until the
  host reboots. On this card the job after a first fault hung the GPU (2026-10-25, 2026-11-22).
- Chats get 503 whenever the add-on isn't ready (loading, waiting for the GPU, benchmark).

## 0.1.0

- First version: Ollama 0.40.11 (cuda_v12 runner only, no cuDNN) on the GPU the NVIDIA Driver
  add-on loaded. Waits for the GPU, pulls and warms up the model with keep_alive -1, checks it is
  fully in VRAM, reports `sensor.ollama_cuda_status`.
- Thermal gate on port 11434: chats get 503 while the driver add-on reports the GPU hot.
- Optional benchmark (FA off/on, KV f16/q8_0, Qwen3-8B reference) → `sensor.ollama_cuda_benchmark`.
