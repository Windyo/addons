# Whisper CUDA

Runs the NVIDIA driver on Home Assistant OS so Whisper can use an NVIDIA GPU.
Built for a GTX 1070 (Pascal), which needs the **580** driver branch.

It serves Whisper speech-to-text over Wyoming on port 10300. Home Assistant discovers it automatically. If the GPU is unavailable it falls back to the CPU.

## How it works

The **NVIDIA Driver** add-on (same repository) builds and loads the driver and writes
`/share/nvidia/state.json`. This add-on only reads that file:

1. If the GPU is ready (fresh `ready` state for this boot, matching driver version), it serves
   Whisper on CUDA. If not, it serves on the CPU at once and switches to the GPU when it becomes
   ready, with no restart.
2. While the driver add-on reports the GPU `hot`, it serves from the CPU, and returns when it
   is `ok` again. If the driver add-on is stopped, it keeps the GPU it already uses and applies
   the same thresholds itself (85 °C or a hardware slowdown → CPU, back at 78 °C).
3. It reports to Home Assistant as `sensor.whisper_cuda_status`.

## Before installing

- Install and start the **NVIDIA Driver** add-on first.
- After installing, turn **Protection mode** off on the add-on page. Without it the add-on can't
  open `/dev/nvidia*` and stays on the CPU.

## Options

| Option | Default | Meaning |
|---|---|---|
| `model` | `large-v3-turbo` | Whisper model on the GPU. |
| `compute_type` | `int8_float32` | `int8_float32` or `float32` on Pascal; `float16` is slow there. |
| `beam_size` | `1` | 1 = fastest; with name biasing it matched beam 5 on this house's commands. |
| `ha_name_biasing` | `true` | Prompt Whisper with the names exposed to Assist. |
| `vad_filter` | `true` | Decode speech only; silence/noise returns nothing. |
| `cpu_fallback_model` | `small` | Model used on the CPU when the GPU is unavailable. |
| `benchmark` | `false` | Benchmark configurations at start and publish `sensor.whisper_cuda_benchmark`. |
| `debug` | `false` | Verbose server logs. |

## Thermal guard

Laptop GPUs can't always shed sustained heat. On the GTX 1070 laptop this was built for, a long
benchmark reached 97 °C, throttled to 139 MHz and finally dropped the GPU off the PCIe bus, which
hangs the whole host. The add-on therefore checks the GPU every 15 s: at 85 °C (or a hardware
thermal slowdown) it serves speech-to-text from the CPU until the card is back under 72 °C.
Normal voice use is short bursts and stays far below that.

## Status sensor

`sensor.whisper_cuda_status` states: `gpu_ready`, `gpu_unavailable` (with `gpu_reason`) or
`driver_mismatch`. The attributes show the device in use (`stt_device`), the model, any thermal
hold, whether the driver add-on's state is fresh, and GPU temperature, power, VRAM and clocks.

## Undo

Stop or uninstall the add-on. Nothing is installed into HAOS itself.
