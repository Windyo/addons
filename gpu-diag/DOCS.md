# GPU Diagnostic

A throw-away add-on that answers one question: when llama.cpp (inside Ollama) faults the GPU
(Xid 13 "Illegal Instruction Encoding", then Xid 79 "fallen off the bus"), is it the VRAM, the
llama.cpp kernels, the card under full load, or Ollama's build?

Start it by hand with the NVIDIA Driver add-on `ready` and the GPU cool. It runs once and exits.

| Stage | What | A failure means |
|---|---|---|
| `vram` | pattern test over free VRAM (keeps `reserve_mib` free for Whisper) | bad VRAM |
| `ops` | llama.cpp `test-backend-ops` built for sm_61 only; `matmul` = the `MUL_MAT` cases, `all` = every op; `CUDA_LAUNCH_BLOCKING=1` | the kernels themselves fault on this card |
| `model` | llama-bench (same build) on the real Qwen3-4B Q4_K_M, downloaded once (2.5 GB): prompt batches of 32/256/512 tokens (`model_tests: pp_tg`), 64 generated tokens; `model_env` adds variables (e.g. `GGML_CUDA_DISABLE_FUSION=1`) | the real workload faults the card even with our build |
| `burn` | gpu-burn: cuBLAS SGEMM at full load for `burn_seconds`, stopped at `max_temp_c` | the card faults under full load (power, heat, hardware) |

Every stage can be switched off on its own (`vram`, `ops: skip`, `model`, `burn_seconds: 0`).
All pass: the fault is specific to Ollama's prebuilt runner.

After a fault the card needs a **full power-off** of the host before any GPU use; the add-on says
so in `sensor.gpu_diag_status` (`stopped`).
