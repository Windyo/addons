# Changelog

## 0.1.5

- New option `module_params` (empty by default): extra `nvidia.ko` parameters such as
  `NVreg_RegistryDwords=...`, used when the add-on loads the driver (so from the next host boot).
- At start and when the GPU stops answering, logs read-only platform state: PCIe link speed/width,
  ASPM, relaxed ordering/no-snoop and the latched error bits of the GPU and its root port (the HAOS
  kernel has no AER driver, so link errors are otherwise invisible), the driver's module parameters,
  the kernel command line, the console framebuffer's place in BAR1 and the console bindings.
- A new driver build keeps NVIDIA's conftest results and the add-on logs the ones that decide memory
  caching and console detection.

## 0.1.4

- At start, logs the laptop platform (DMI model, WMI interfaces, ACPI platform profile) and the GPU
  temperature targets, and tests whether `nvidia-smi -lgc` clock locking works (full supported range,
  so no effective change, then reset). Adds `platform` and `clock_lock_test` to `sensor.nvidia_gpu_status`.
- Logs the laptop's Gigabyte WMI methods (`WMBC`/`WMBD`, decompiled from the ACPI tables) and copies
  the decompiled tables to `/share/nvidia/acpi/`. Read-only.
- Fix: restarted after the GPU fell off the bus, the add-on reported `ready` for ~45 s. It now checks
  the GPU answers and starts in `gpu_error` if not.

## 0.1.3

- At start, logs (and adds to `sensor.nvidia_gpu_status` as `power_clock_controls` and
  `supported_graphics_clocks`) which power-limit and clock controls the card exposes. Read-only.

## 0.1.2

- Fix: with no NVIDIA errors in the kernel log (the normal case) `sensor.nvidia_gpu_status` lost all
  its attributes (temperature, VRAM, processes, `kernel_gpu_errors`) and the log showed
  `jq: invalid JSON text passed to --argjson` every minute. `state.json` was not affected.

## 0.1.1

- `sensor.nvidia_gpu_status` carries `kernel_gpu_errors`: the last NVIDIA error lines from the kernel
  log (Xid codes), also logged when the add-on enters `gpu_error`.

## 0.1.0

- First version. The driver build and load (crash guard, vermagic check, config drift gate,
  HAOS kernel patches) moved unchanged from whisper-cuda 0.2.7.
- Writes `/share/nvidia/state.json` every 15 s (boot id, state, thermal, temperature, VRAM) for
  GPU add-ons, and `sensor.nvidia_gpu_status` every minute with per-process VRAM.
- Owns the thermal policy (hot at 85 °C or a hardware slowdown, ok at 78 °C), as whisper-cuda did.
- If the driver is already loaded (by whisper-cuda 0.2.x), only checks the nodes and builds the
  module cache in the background for the next boot.
- `gpu_error` after three failed `nvidia-smi` polls in a row, so consumers stop using the GPU.
