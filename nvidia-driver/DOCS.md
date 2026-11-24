# NVIDIA Driver

Runs the NVIDIA driver on Home Assistant OS so other add-ons can use an NVIDIA GPU. Built for a
GTX 1070 (Pascal), which needs the **580** driver branch (570 is in the image for a test, see
`driver_branch`). It is the only add-on that touches the
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
| `driver_branch` | `580` | `570` loads NVIDIA 570.211.01 instead, to test whether the GPU faults are a 580 regression. See below. |
| `load_driver` | `true` | Off: build and verify only. On: load the driver into the kernel. |
| `build_jobs` | `4` | Compile jobs for the build. |
| `retry_after_crash` | `false` | Allow one more load after an attempt that never finished before a reboot. |
| `hot_c` | `85` | GPU temperature at which consumers back off. |
| `cool_c` | `78` | Temperature at which they return. |
| `auto_recover` | `false` | When the GPU falls off the bus (Xid 79) and stays gone 2 minutes, reset the host (see below). |
| `core_offset_mhz` | `0` | Negative core clock offset in MHz (NVML), applied at every start. Lowers the top boost clock; idle is unaffected. |
| `mem_offset_mhz` | `0` | Negative memory clock offset in MHz. |
| `console_unbind` | `false` | Detach the text console from the GPU's framebuffer (the laptop screen stops showing console text). |
| `pcie_watch` | `true` | Timestamp every PCIe link error of the GPU and its port (read and clear the latched bits every 0.5 s). Counts on the sensor (`pcie_watch`), events in the log and in `/share/nvidia/pcie-events.log`. |
| `pcie_max_gen` | `0` | Cap the GPU's PCIe link at this generation (1-3) through the port's Target Link Speed, as a BIOS "PCIe Gen" setting does. 0 = no cap; it also undoes a cap set earlier in the same boot. Holds from the next speed change (the driver changes speed between idle and load). |
| `acpi_watch` | `true` | Timestamp the ACPI events around the GPU through kernel probes (read-only): notifications to the GPU (the firmware's power-level orders D1–D5, GPS status), the driver's `_DSM` calls (GPS power steering and others) and every ACPI notification in the system (AC adapter, thermal zones). Counts on the sensor (`acpi_watch`), events in the log and in `/share/nvidia/acpi-events.log`. |
| `module_params` | `""` | Extra `nvidia.ko` parameters (space-separated `NVreg_...=value`), applied when the add-on loads the driver, i.e. after the next host reboot. |

## Switching the driver branch

1. Set `driver_branch` and restart the add-on. The loaded driver keeps running; the add-on builds
   the other branch's modules in the background (about 15 min, log: "driver_branch: 570 modules
   built"; the sensor shows `driver_next_boot`).
2. Reboot the host. The add-on loads the chosen branch.

Consumers must ship the same userspace version as the loaded module. Whisper CUDA and Ollama CUDA
ship 580: with 570 loaded they report `driver_mismatch` (Whisper runs on the CPU, Ollama has no
GPU). GPU Diagnostic ships both and uses the loaded one. Set `580` and reboot to go back.

## ACPI tables

At the first start of each boot the add-on copies the DSDT and SSDT tables (never MSDM or SLIC,
which hold the Windows licence) to `/share/nvidia/acpi/`, with `iasl` decompilations (`*.dsl`).
Docker hides `/sys/firmware` in the container, so they are read through the add-on's private
sysfs mount.

## Host reset after a lost GPU

After Xid 79 ("GPU has fallen off the bus") the processes using the GPU hang inside the driver and
can't be killed. A normal reboot then never completes, because the Supervisor waits for their
containers to stop. The add-on bypasses that: SysRq sync and read-only remount, then a full
platform reset through port CF9h, which also cuts the GPU's power, and the machine restarts by
itself. Unsaved data from the last seconds can be lost; the file systems recover at boot.

- Manual: call `hassio.addon_stdin` with `addon: 8634c18d_nvidia-driver` and `input: host-reset`.
  (`input: pcie` only logs the PCIe link state, the GPU's replay counters and the error counts.)
  This resets the host whatever the GPU state, so use it only when the GPU is lost.
- Automatic: switch on `auto_recover`.

At most 2 resets per 24 hours. Every reset is listed in `/share/nvidia/host-resets.log`, and the
last one is shown as `last_host_reset` on `sensor.nvidia_gpu_status`.
