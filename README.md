# Windyo's Home Assistant add-ons

| Add-on | What it does |
|---|---|
| [Whisper CUDA](whisper-cuda/) | NVIDIA driver + (later) Whisper speech-to-text on the GPU under HAOS. amd64, Pascal / driver 580. |
| [NVIDIA Driver](nvidia-driver/) | Builds, loads and monitors the NVIDIA 580 driver on HAOS; publishes /share/nvidia/state.json for GPU add-ons. amd64, Pascal. |
| [Ollama CUDA](ollama-cuda/) | Local LLM (Qwen3-4B) on the GPU for a chat-only fallback agent and AI Tasks, with a thermal gate. Needs NVIDIA Driver. amd64. |
| [Web Search](web-search/) | SearXNG + a small MCP server: web_search and read_page tools for the local LLM, nothing else. amd64. |
| [GPU Probe](gpu-probe/) | Throw-away test: can an add-on with no kernel rights use the GPU another add-on loaded? nvidia-smi + a small CUDA job. amd64. |
| [GPU Diagnostic](gpu-diag/) | Throw-away test: why does llama.cpp fault the GPU? VRAM test, llama.cpp kernel tests (sm_61), cuBLAS stress, stops at the first failure. amd64. |
| [CESU Declare](cesu-declare/) | Monthly CESU (URSSAF) declaration via Playwright. v0.1 is a dry-run explorer: fills the form, captures it, never submits. amd64. |
