# Changelog

## 0.1.2

- Create `/dev/nvidia*` through a private devtmpfs mount: the Supervisor gives every add-on a
  read-only bind of the host's `/dev`, so `mknod /dev/nvidiactl` failed. Adds `SYS_ADMIN`.
- First load on the GTX 1070 (0.1.1) worked: `nvidia` and `nvidia_uvm` loaded cleanly on 6.18.52-haos.

## 0.1.1

- Apply HAOS's own kernel patches (fetched for the installed OS version, bundled 18.3 set as
  fallback). Without them the config drift gate rejected `CONFIG_IPV6_REACHABILITY_PROBE`.

## 0.1.0

- Rewrite. Phase 1: builds NVIDIA 580.196.07 (`nvidia`, `nvidia-uvm`) for the running HAOS
  kernel, verifies its vermagic, optionally loads it, reports `sensor.whisper_cuda_status`.
- Replaces the June 2025 attempt, which shipped CUDA libraries but never loaded a kernel driver.
