#!/usr/bin/env bash
# Load the cached NVIDIA modules into the running (host) kernel and create the device nodes
# inside this container.   nv-load.sh <module-dir>
set -euo pipefail
. /usr/local/bin/common.sh
DIR=${1:?module dir}
KREL=$(uname -r)

if grep -q '^nvidia ' /proc/modules; then
  loaded=$(cat /sys/module/nvidia/version 2>/dev/null || echo unknown)
  [ "$loaded" = "$NV_VERSION" ] || {
    log "ERROR: nvidia $loaded is already loaded, this image needs $NV_VERSION. Reboot the host."
    exit 10
  }
  log "nvidia $loaded already loaded"
else
  # The only check the kernel makes is an exact vermagic match. Do it ourselves first,
  # against a module that ships with this kernel.
  ref=$(find "/lib/modules/${KREL}" -name '*.ko' -print -quit)
  [ -n "$ref" ] || { log "ERROR: no reference module in /lib/modules/${KREL}"; exit 11; }
  want=$(modinfo -F vermagic "$ref")
  have=$(cat "$DIR/vermagic")
  [ "$have" = "$want" ] || { log "ERROR: vermagic '$have' != kernel '$want'"; exit 12; }

  for dep in $(modinfo -F depends "$DIR/nvidia.ko" | tr ',' ' '); do
    log "loading dependency $dep"
    modprobe "$dep"
  done
  log "insmod nvidia.ko"
  insmod "$DIR/nvidia.ko"
fi

if ! grep -q '^nvidia_uvm ' /proc/modules; then
  log "insmod nvidia-uvm.ko"
  insmod "$DIR/nvidia-uvm.ko"
fi

# The Supervisor bind-mounts the host's /dev read-only into every add-on, and the proprietary
# driver doesn't create its nodes itself (nvidia-modprobe/udev do that on other distros).
# Mount the kernel's devtmpfs (the same instance behind the host's /dev) at a private path, create
# the nodes there, and they appear in /dev here too. Needs CAP_SYS_ADMIN.
DT=/run/nv-devtmpfs
mkdir -p "$DT"
mountpoint -q "$DT" || mount -t devtmpfs devtmpfs "$DT"
mk() { [ -e "$DT/$1" ] || mknod -m 666 "$DT/$1" c "$2" "$3"; }
mk nvidiactl 195 255
n=0
for _ in /proc/driver/nvidia/gpus/*; do
  mk "nvidia${n}" 195 "$n"
  n=$((n + 1))
done
uvm=$(awk '$2 == "nvidia-uvm" { print $1 }' /proc/devices)
[ -n "$uvm" ] || { umount "$DT"; log "ERROR: nvidia-uvm has no char major"; exit 13; }
mk nvidia-uvm "$uvm" 0
mk nvidia-uvm-tools "$uvm" 1
umount "$DT"

for d in nvidiactl nvidia0 nvidia-uvm nvidia-uvm-tools; do
  [ -c "/dev/$d" ] || { log "ERROR: /dev/$d not visible in the container"; exit 14; }
done
log "device nodes ready (${n} GPU)"
