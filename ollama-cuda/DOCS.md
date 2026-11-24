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

### Search router (option `search_router`, on)
Only for an agent that has the Web Search add-on's `web_search` tool. The small model rarely
decides to search by itself and answers "I couldn't find…" instead, so the add-on decides:
- a fresh question (news, latest, price, weather, score, "search…", or the French equivalents) is
  answered at once with "Let me check." / "Je regarde." and a search for the question;
- otherwise the model answers, but if its first sentence is "I couldn't find / I can't provide…"
  without a search, that sentence is dropped (never spoken) and the search runs instead;
- after a search, the model's answer passes unchanged.
The reply language follows the agent's instructions line "Reply in French." / "Reply in English.".

## Before installing

- Install and start the **NVIDIA Driver** add-on first.
- After installing, turn **Protection mode** off. Without it the add-on can't open the GPU.
