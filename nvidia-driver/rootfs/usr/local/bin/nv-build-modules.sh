#!/usr/bin/env bash
# Build nvidia.ko + nvidia-uvm.ko for one exact kernel.
#
#   nv-build-modules.sh <kernel-release> <kernel-config[.gz]> <out-dir> [jobs] [patch-dir]
#
# patch-dir: HAOS's own kernel patches (buildroot-external/patches/linux), applied in order.
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
PATCHES=${5:-}
NV_VERSION=$(cat /opt/nvidia/VERSION)
ALLOW=/usr/local/share/nvidia-driver/config-drift-allow.txt

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

: > "$OUT/patches.txt"
if [ -n "$PATCHES" ]; then
  for p in "$PATCHES"/*.patch; do
    [ -e "$p" ] || continue
    step "Applying HAOS patch ${p##*/}"
    patch -d "$K" -p1 --forward --quiet < "$p"
    echo "${p##*/}" >> "$OUT/patches.txt"
  done
fi

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
    printf '%s kernel=%s rebuilt=%s\n' "$o" \
      "$(grep -E "^${o}=" "$WORK/config.orig" | cut -d= -f2- || echo unset)" \
      "$(grep -E "^${o}=" "$K/.config" | cut -d= -f2- || echo unset)"
  done | tee "$OUT/config-drift-detail.txt"
  exit 3
fi

rel=$(make -C "$K" -s kernelrelease)
[ "$rel" = "$KREL" ] || { echo "ERROR: source tree is '$rel', expected '$KREL'"; exit 4; }

step "Building vmlinux for Module.symvers (jobs=$JOBS)"
nice -n 19 make -C "$K" -s -j"$JOBS" vmlinux
make -C "$K" -s modules_prepare
cp "$K/vmlinux.symvers" "$K/Module.symvers"
grep -q $'\tvmlinux\t' "$K/Module.symvers" || { echo "ERROR: Module.symvers has no vmlinux exports"; exit 5; }

# Older branches call timer functions that newer kernels renamed (6.15: del_timer* -> timer_delete*;
# 6.16: from_timer -> timer_container_of). Rename only when the kernel lacks the old names.
if ! grep -qw del_timer_sync "$K/include/linux/timer.h" && grep -rqw del_timer_sync "$WORK/nv"; then
  step "timer API: del_timer[_sync] -> timer_delete[_sync]"
  grep -rlw -e del_timer_sync -e del_timer "$WORK/nv" --include='*.c' --include='*.h' \
    | xargs sed -i 's/\bdel_timer_sync(/timer_delete_sync(/g; s/\bdel_timer(/timer_delete(/g'
fi
if ! grep -qw from_timer "$K/include/linux/timer.h" && grep -rqw from_timer "$WORK/nv"; then
  step "timer API: from_timer -> timer_container_of"
  grep -rlw from_timer "$WORK/nv" --include='*.c' --include='*.h' | xargs sed -i 's/\bfrom_timer(/timer_container_of(/g'
fi

step "Building NVIDIA ${NV_VERSION} (nvidia, nvidia-uvm)"
cp -a /opt/nvidia/kernel-src "$WORK/nv"
# Branches before 560 pass their include paths and defines through EXTRA_CFLAGS, which kernels
# >= 6.15 no longer read (580 uses ccflags-y). Same rename NVIDIA made; a no-op for 580.
if grep -qw EXTRA_CFLAGS "$WORK/nv/Kbuild"; then
  step "Kbuild: EXTRA_CFLAGS -> ccflags-y (pre-6.15 build files)"
  sed -i 's/\bEXTRA_CFLAGS\b/ccflags-y/g' "$WORK/nv/Kbuild" "$WORK"/nv/*/*.Kbuild
fi
# GCC 13.3 here vs 13.4 for the kernel: same major, bug-fix release apart.
nice -n 19 make -C "$WORK/nv" -j"$JOBS" SYSSRC="$K" NV_KERNEL_MODULES="nvidia nvidia-uvm" \
  IGNORE_CC_MISMATCH=1 modules

for m in nvidia nvidia-uvm; do
  cp "$WORK/nv/${m}.ko" "$OUT/"
done
modinfo -F vermagic "$OUT/nvidia.ko" > "$OUT/vermagic"
# What NVIDIA's conftest detected in this kernel (cache attributes, console detection, ...).
mkdir -p "$OUT/conftest"
cp "$WORK"/nv/conftest/*.h "$OUT/conftest/" 2>/dev/null || true
echo "$NV_VERSION" > "$OUT/driver_version"
cp "$K/.config" "$OUT/kernel.config"

step "Done: $(cat "$OUT/vermagic")"
rm -rf "$WORK"
