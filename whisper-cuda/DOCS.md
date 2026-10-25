# Whisper CUDA

Runs the NVIDIA driver on Home Assistant OS so Whisper can use an NVIDIA GPU.
Built for a GTX 1070 (Pascal), which needs the **580** driver branch.

It serves Whisper speech-to-text over Wyoming on port 10300. Home Assistant discovers it automatically. If the GPU is unavailable it falls back to the CPU.

## How it works

HAOS has no NVIDIA driver and no NVIDIA container runtime. This add-on:

1. reads the running kernel version (`uname -r`) and its config (`/proc/config.gz`),
2. downloads that exact kernel version from kernel.org and builds the NVIDIA 580.196.07
   kernel modules against it (once per kernel, about 10–20 min, cached in the add-on data),
3. checks that the module's version magic matches the running kernel exactly,
4. loads `nvidia.ko` and `nvidia-uvm.ko` into the HAOS kernel and creates `/dev/nvidia*`
   inside the container,
5. starts faster-whisper on the GPU (or on the CPU as a fallback),
6. reports to Home Assistant as `sensor.whisper_cuda_status`.

After an HAOS update the kernel changes, and the add-on builds the driver again on its next start.

## Before installing

- Take a full backup.
- After installing, turn **Protection mode** off on the add-on page. The add-on needs it to load a kernel module.
- Leave **Start on boot** off until the driver has run without problems for a while.

## Options

| Option | Default | Meaning |
|---|---|---|
| `load_driver` | `false` | Off: build and verify only. On: load the driver into the kernel. |
| `build_jobs` | `4` | Compile jobs for the build. |
| `retry_after_crash` | `false` | Allow one more load after an attempt that never finished before a reboot. |
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

`sensor.whisper_cuda_status` states: `no_gpu`, `building`, `build_failed`, `built`,
`load_refused`, `load_failed`, `gpu_ready`. The attributes include the kernel, driver, vermagic,
any config drift, log tails on failure, and GPU temperature, power, VRAM and clocks when ready.

## Undo

Stop or uninstall the add-on and reboot the host. Nothing is installed into HAOS itself.
