# Ollama CUDA

A local LLM on the NVIDIA GPU, for Home Assistant's Ollama integration: a chat-only fallback
conversation agent (questions the house's own sentences don't match) and AI Tasks. It never gets
house control. Needs the **NVIDIA Driver** add-on.

## How it works

1. Waits until the NVIDIA Driver add-on reports the GPU ready, and checks the driver version.
   It never serves from the CPU (4 cores would take 10+ s per answer).
2. Runs Ollama 0.40.11 with only its CUDA 12 runner (the one with real Pascal kernels).
3. Pulls the model on first start (Qwen3-4B Instruct, about 2.5 GB), loads it and keeps it in
   VRAM, and checks that all of it is on the GPU.
4. **Thermal gate:** while the driver add-on reports the GPU hot, chat requests get HTTP 503, so
   Home Assistant says the agent is unavailable instead of heating the card further.
5. Reports `sensor.ollama_cuda_status`: state, model, VRAM, last answer time, refusals.

## Home Assistant

Ollama integration URL: `http://8634c18d-ollama-cuda:11434`. The port is not published to the
network (Ollama has no authentication).

## Before installing

- Install and start the **NVIDIA Driver** add-on first.
- After installing, turn **Protection mode** off. Without it the add-on can't open the GPU.
