# NVIDIA Driver

Runs the NVIDIA driver on Home Assistant OS so other add-ons can use an NVIDIA GPU. Built for a
GTX 1070 (Pascal), which needs the **580** driver branch. It is the only add-on that touches the
kernel; GPU add-ons (Whisper CUDA, Ollama CUDA) only read its state and open `/dev/nvidia*`.

## How it works

HAOS has no NVIDIA driver and no NVIDIA container runtime. This add-on:

1. reads the running kernel version and its config (`/proc/config.gz`),
2. downloads that exact kernel from kernel.org, applies the HAOS kernel patches of the installed
   OS version, and builds the NVIDIA 580.196.07 modules against it (once per kernel, about
   10–20 min, cached in the add-on data),
3. checks that the module's version magic matches the running kernel exactly,
4. loads `nvidia.ko` and `nvidia-uvm.ko` and creates `/dev/nvidia*` on the host,
5. watches the GPU every 15 s and writes `/share/nvidia/state.json`,
6. reports to Home Assistant as `sensor.nvidia_gpu_status`.

After an HAOS update the kernel changes, and the add-on builds the driver again on its next start.
Stopping the add-on does not unload the driver; that needs a reboot.

## `/share/nvidia/state.json`

```json
{"boot_id": "…", "state": "ready", "driver": "580.196.07", "kernel": "6.18.58-haos",
 "thermal": "ok", "temperature_c": 47, "vram_total_mib": 8192, "vram_used_mib": 1161,
 "updated": "2026-11-20T18:00:00Z"}
```

A consumer uses the GPU only if `state` is `ready`, `boot_id` equals its own
`/proc/sys/kernel/random/boot_id`, `updated` is less than 60 s old, and `/dev/nvidia-uvm` exists.
`thermal` is `hot` at the hot threshold or on a hardware thermal slowdown, and `ok` again at the
cool threshold.

## Before installing

- Take a full backup.
- After installing, turn **Protection mode** off. The add-on needs it to load a kernel module.
- Leave **Start on boot** off until a reboot test has passed.

## Options

| Option | Default | Meaning |
|---|---|---|
| `load_driver` | `true` | Off: build and verify only. On: load the driver into the kernel. |
| `build_jobs` | `4` | Compile jobs for the build. |
| `retry_after_crash` | `false` | Allow one more load after an attempt that never finished before a reboot. |
| `hot_c` | `85` | GPU temperature at which consumers back off. |
| `cool_c` | `78` | Temperature at which they return. |
