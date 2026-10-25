#!/usr/bin/env bash
# Build nvidia.ko + nvidia-uvm.ko for one exact kernel.
#
#   nv-build-modules.sh <kernel-release> <kernel-config[.gz]> <out-dir> [jobs]
#
# The HAOS kernel has MODVERSIONS and MODULE_FORCE_LOAD off, so a module loads only if its
# vermagic matches the running kernel exactly. We therefore build against the upstream source
# of that exact version with the kernel's own config (/proc/config.gz on the device).
# vmlinux is built once to get the real export list (Module.symvers): NVIDIA's conftests
# rely on it to detect which kernel symbols exist.
set -euo pipefail

KREL=${1:?kernel release, e.g. 6.18.52-haos}
KCONFIG=${2:?kernel config path}
OUT=${3:?output dir}
JOBS=${4:-4}
NV_VERSION=$(cat /opt/nvidia/VERSION)
ALLOW=/usr/local/share/whisper-cuda/config-drift-allow.txt

KVER=${KREL%%-*}
[ "$KVER" != "$KREL" ] || { echo "ERROR: kernel release '$KREL' has no local version suffix"; exit 2; }
MAJOR=${KVER%%.*}
WORK=${WORK:-/tmp/nvbuild}

step() { echo "==> $*"; }

rm -rf "$WORK"
mkdir -p "$WORK" "$OUT"
cd "$WORK"

step "Downloading linux-${KVER}"
base="https://cdn.kernel.org/pub/linux/kernel/v${MAJOR}.x"
curl -fsSLo linux.tar.xz "${base}/linux-${KVER}.tar.xz"
curl -fsSLo sha256sums.asc "${base}/sha256sums.asc"
want=$(awk -v f="linux-${KVER}.tar.xz" '$2 == f { print $1 }' sha256sums.asc)
[ -n "$want" ] || { echo "ERROR: no checksum for linux-${KVER}.tar.xz"; exit 2; }
echo "${want}  linux.tar.xz" | sha256sum -c -
tar -xf linux.tar.xz
rm -f linux.tar.xz
K="$WORK/linux-${KVER}"

step "Configuring from $KCONFIG"
case "$KCONFIG" in
  *.gz) zcat "$KCONFIG" > "$K/.config" ;;
  *)    cp "$KCONFIG" "$K/.config" ;;
esac
cp "$K/.config" "$WORK/config.orig"
make -C "$K" -s olddefconfig

# Config drift gate: re-running Kconfig with our toolchain must not change anything that
# can affect the kernel ABI. Only toolchain version stamps are allowed to differ.
diff <(grep '^CONFIG_' "$WORK/config.orig" | sort) <(grep '^CONFIG_' "$K/.config" | sort) \
  | sed -nE 's/^[<>] (CONFIG_[A-Z0-9_]+)=.*/\1/p' | sort -u > "$OUT/config-drift.txt" || true
unexpected=$(grep -vxF -f "$ALLOW" "$OUT/config-drift.txt" || true)
if [ -n "$unexpected" ]; then
  echo "ERROR: kernel config changed beyond the allowlist:"
  for o in $unexpected; do
    printf '  %-40s kernel: %-12s rebuilt: %s\n' "$o" \
      "$(grep -E "^${o}=" "$WORK/config.orig" | cut -d= -f2- || echo unset)" \
      "$(grep -E "^${o}=" "$K/.config" | cut -d= -f2- || echo unset)"
  done
  exit 3
fi

rel=$(make -C "$K" -s kernelrelease)
[ "$rel" = "$KREL" ] || { echo "ERROR: source tree is '$rel', expected '$KREL'"; exit 4; }

step "Building vmlinux for Module.symvers (jobs=$JOBS)"
nice -n 19 make -C "$K" -s -j"$JOBS" vmlinux
make -C "$K" -s modules_prepare
cp "$K/vmlinux.symvers" "$K/Module.symvers"
grep -q $'\tvmlinux\t' "$K/Module.symvers" || { echo "ERROR: Module.symvers has no vmlinux exports"; exit 5; }

step "Building NVIDIA ${NV_VERSION} (nvidia, nvidia-uvm)"
cp -a /opt/nvidia/kernel-src "$WORK/nv"
# GCC 13.3 here vs 13.4 for the kernel: same major, bug-fix release apart.
nice -n 19 make -C "$WORK/nv" -j"$JOBS" SYSSRC="$K" NV_KERNEL_MODULES="nvidia nvidia-uvm" \
  IGNORE_CC_MISMATCH=1 modules

for m in nvidia nvidia-uvm; do
  cp "$WORK/nv/${m}.ko" "$OUT/"
done
modinfo -F vermagic "$OUT/nvidia.ko" > "$OUT/vermagic"
echo "$NV_VERSION" > "$OUT/driver_version"
cp "$K/.config" "$OUT/kernel.config"

step "Done: $(cat "$OUT/vermagic")"
rm -rf "$WORK"
