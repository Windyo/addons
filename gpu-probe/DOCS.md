# GPU Probe

A throw-away test. Before the NVIDIA driver moves into an add-on of its own, it answers one
question: can an add-on with **no kernel rights** use the GPU that another add-on loaded?

It gets only `full_access`, the device rule that lets it open `/dev/nvidia*`. It has no kernel
modules, no extra capabilities, and the default AppArmor profile. It never loads, unloads or
creates anything.

## What it checks

1. It waits until the driver is loaded and `/dev/nvidiactl`, `/dev/nvidia0` and
   `/dev/nvidia-uvm` exist. If it started before they did, it records when each one appeared.
2. The loaded driver must be 580.196.07, the same version as its own NVIDIA libraries. If it
   isn't, the probe stops (`driver_mismatch`) without opening the GPU.
3. It opens each device node.
4. It runs `nvidia-smi`.
5. It runs a small CUDA job: a kernel over 1 M numbers in GPU memory, then the same kernel in
   managed memory (the `nvidia-uvm` path), with every result checked. It takes milliseconds.

If a check fails, it is retried twice, 30 s apart, before the verdict.

## Result

`sensor.gpu_probe_status` is `waiting_gpu`, `testing`, `pass`, `fail` or `driver_mismatch`. The
attributes hold the details: the AppArmor profile, the capabilities, the `/dev` mount, and each
check. The last 10 runs are kept in the add-on's data, and `history` lists their verdicts.

## Use

1. Install it. The Supervisor builds it on the device, which downloads the NVIDIA driver package
   (about 370 MB).
2. Turn **Protection mode** off. With it on, `full_access` is ignored, and opening the nodes
   fails with `EPERM`.
3. Start it while Whisper CUDA has the driver loaded.

To test a start *before* the driver exists, leave it installed and reboot the host. It starts in
the `initialize` stage, before Whisper CUDA loads the driver.

Uninstall it when the test is done.
