# Changelog

## 0.2.1

- ACPI event watcher (option `acpi_watch`, on by default). The laptop's firmware can order the GPU
  to a lower power level (ACPI notifications 0xD1–0xD5 from its embedded controller) and NVIDIA's
  Linux driver acts on them; nothing logged them. Kernel probes on the driver's ACPI notify handler,
  on its `_DSM` calls and on every ACPI notification now log each one with its UTC time, next to
  the PCIe watcher, in the log, on the sensor (`acpi_watch`) and in
  `/share/nvidia/acpi-events.log`. Read-only: probes only record.

## 0.2.0

- Option `driver_branch` (`580` default, `570`): the image carries NVIDIA 570.211.01 next to
  580.196.07, to test whether the GPU faults are a 580 regression (a report on llama.cpp shows the
  same Xid 13 types on 580; 570 builds unchanged for 6.18.58). The branch already loaded this boot
  stays in use; the chosen one is built in the background and loads at the next boot.
- ACPI tables are read again: the old code read the container's /sys, where Docker hides
  /sys/firmware ("ACPI tables not readable"). Now through the private sysfs, once per boot, to
  `/share/nvidia/acpi/` (DSDT, SSDTs and their decompilations).

## 0.1.10

- Quieter log while HA Core is down. At a host boot the add-on starts about 36 s before Core, and
  the Supervisor answers 502 until Core is up, which logged curl's error and a warning every minute.
  Now one warning with the HTTP code or curl error ("HA Core not reachable (HTTP 502), status not
  published yet"), and one line when publishing works again.

## 0.1.9

- Fix: the PCIe watcher and `pcie_max_gen` (0.1.8), and `console_unbind` (0.1.7), failed with
  "Read-only file system". The private sysfs mount shared the read-only superblock Docker created
  for the container's /sys. The add-on now remounts it writable (or, failing that, mounts a sysfs
  tied to a new network namespace) and logs the result ("private sysfs: rw,...").

## 0.1.8

- PCIe link error watcher (option `pcie_watch`, on by default). The GPU and its root port latch
  correctable link errors (on 2026-11-24: receiver error, bad TLP, replay timer timeout), but HAOS
  has no AER driver, so nothing ever reported when they happened. Every 0.5 s the add-on now reads
  and clears those bits. Each error gets a log line with its UTC time, the link speed and the
  seconds since the last speed change. The counts go to the sensor (`pcie_watch`), and the events
  to `/share/nvidia/pcie-events.log` with an fsync, because the host journal was lost in a hard
  power-off that day.
- Option `pcie_max_gen` (0 = no cap): caps the link at PCIe gen 1, 2 or 3 through the root port's
  Target Link Speed, as a BIOS "PCIe Gen" setting does. 0 undoes a cap set earlier in the boot.
  This is the test for the link hypothesis: the faults come with dense command traffic at gen 3.
- The sensor also shows the GPU's own link counters (`nvml_pcie`: replays since reset, replay
  rollovers, current and host-max PCIe generation). Stdin command `pcie` logs all of it on demand.

## 0.1.7

- Option `console_unbind` (off by default): detaches the text console from the EFI framebuffer,
  which draws into the start of the GPU's BAR1. Test for the GPU faults: if the driver did not keep
  that range for the console, cursor blinks and console lines write into memory the driver mapped
  there (e.g. a new CUDA program's command buffers). `fb_console_bound` on the sensor.

## 0.1.6

- Clock offsets through NVML (no X server needed): options `core_offset_mhz` (-500..0) and
  `mem_offset_mhz` (-1000..0), applied at every start. With both at 0 the add-on only probes which
  offsets the card accepts and logs it (`clock_offsets` on `sensor.nvidia_gpu_status`). On
  2026-11-24 a fault came on a 113.5 W spike at the 115 W cap while the boost clock climbed
  1442 -> 1885 MHz; a lower top clock means lower voltage and smaller spikes.

## 0.1.5

- Host reset for a wedged GPU. After Xid 79 the GPU's users hang inside the driver, the Supervisor's
  reboot waits forever on their container, and only a power cycle at the machine helped. The add-on
  can now flush the disks, remount them read-only and do a full platform reset (CF9h, which also
  cuts the GPU's power) without going through userspace. Manual: send `host-reset` to the add-on's
  stdin (`hassio.addon_stdin`). Automatic: option `auto_recover` (off by default) resets when the GPU
  has been lost for 2 minutes. At most 2 resets per 24 h. Needs the new `SYS_RAWIO` privilege.

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
