# Changelog

## 0.1.0

- First version: waits for the driver and its nodes, then checks the version, opens each node, and
  runs nvidia-smi and a CUDA job (device memory and managed memory). Reports
  `sensor.gpu_probe_status`, and keeps the last 10 runs.
