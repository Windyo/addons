# Whisper CUDA

Runs the NVIDIA driver on Home Assistant OS so Whisper can use an NVIDIA GPU.
Built for a GTX 1070 (Pascal), which needs the **580** driver branch.

**Phase 1 (this version):** the add-on builds the driver and loads it. Whisper is not included yet.

## How it works

HAOS has no NVIDIA driver and no NVIDIA container runtime. This add-on:

1. reads the running kernel version (`uname -r`) and its config (`/proc/config.gz`),
2. downloads that exact kernel version from kernel.org and builds the NVIDIA 580.196.07
   kernel modules against it (once per kernel, about 10–20 min, cached in the add-on data),
3. checks that the module's version magic matches the running kernel exactly,
4. loads `nvidia.ko` and `nvidia-uvm.ko` into the HAOS kernel and creates `/dev/nvidia*`
   inside the container,
5. reports to Home Assistant as `sensor.whisper_cuda_status`.

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

## Status sensor

`sensor.whisper_cuda_status` states: `no_gpu`, `building`, `build_failed`, `built`,
`load_refused`, `load_failed`, `gpu_ready`. The attributes include the kernel, driver, vermagic,
any config drift, log tails on failure, and GPU temperature, power, VRAM and clocks when ready.

## Undo

Stop or uninstall the add-on and reboot the host. Nothing is installed into HAOS itself.
