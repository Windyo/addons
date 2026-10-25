#!/usr/bin/env bash
# Whisper CUDA add-on, phase 1: build the NVIDIA driver for the running HAOS kernel,
# optionally load it, and report everything through sensor.whisper_cuda_status.
#
# States: no_gpu, building, build_failed, built, load_refused, load_failed, gpu_ready
set -uo pipefail
. /usr/local/bin/common.sh

KREL=$(uname -r)
CACHE="$DATA/$NV_VERSION/$KREL"
LOGS="$DATA/logs"
mkdir -p "$DATA" "$LOGS"
LOAD=$(opt load_driver)
JOBS=$(opt build_jobs)
RETRY=$(opt retry_after_crash)

log "Whisper CUDA: kernel $KREL, driver $NV_VERSION, load_driver=$LOAD"

# ---- 1. GPU present? ------------------------------------------------------------------------
GPU=""
for d in /sys/bus/pci/devices/*; do
  [ "$(cat "$d/vendor")" = 0x10de ] || continue
  case "$(cat "$d/class")" in
    0x0300*|0x0302*) GPU="${d##*/}"; GPU_ID=$(cat "$d/device"); break ;;
  esac
done
[ -n "$GPU" ] || hold no_gpu '{"reason": "no NVIDIA display/3D controller on the PCI bus"}'
GPU_ATTRS=$(jq -n --arg p "$GPU" --arg i "$GPU_ID" '{pci: $p, device_id: $i}')

# ---- 2. Driver for this exact kernel (build once, cached in /data) ---------------------------
if [ ! -f "$CACHE/nvidia.ko" ]; then
  publish building "$(jq -n --argjson g "$GPU_ATTRS" --arg j "$JOBS" \
    '$g + {note: "first start on this kernel: about 10-20 min", jobs: $j}')"
  BLOG="$LOGS/build-${KREL}.log"
  (
    flock -n 9 || { echo "another build is running"; exit 1; }
    rm -rf "$CACHE.tmp"
    /usr/local/bin/nv-build-modules.sh "$KREL" /proc/config.gz "$CACHE.tmp" "$JOBS" \
      && mv "$CACHE.tmp" "$CACHE"
  ) 9> "$DATA/build.lock" > "$BLOG" 2>&1
  if [ ! -f "$CACHE/nvidia.ko" ]; then
    hold build_failed "$(jq -n --argjson g "$GPU_ATTRS" --argjson t "$(tail_json "$BLOG" 30)" \
      '$g + {log_tail: $t}')"
  fi
  log "build finished"
fi
BUILT_ATTRS=$(jq -n --argjson g "$GPU_ATTRS" --arg v "$(cat "$CACHE/vermagic")" \
  --argjson drift "$(jq -Rsc 'split("\n") | map(select(length > 0))' "$CACHE/config-drift.txt")" \
  '$g + {vermagic: $v, config_drift: $drift}')

[ "$LOAD" = true ] || hold built "$(jq -n --argjson b "$BUILT_ATTRS" \
  '$b + {note: "driver built, not loaded (load_driver is off)"}')"

# ---- 3. Load, with a crash guard ------------------------------------------------------------
# A marker holds the boot id of an unfinished load. Finding it after a reboot means the last
# attempt may have taken the host down: never retry that automatically.
BOOT=$(cat /proc/sys/kernel/random/boot_id)
MARK="$DATA/load-attempt"
if [ -f "$MARK" ] && ! grep -q '^nvidia ' /proc/modules; then
  if [ "$(cat "$MARK")" != "$BOOT" ] && [ "$RETRY" != true ]; then
    hold load_refused "$(jq -n --argjson b "$BUILT_ATTRS" \
      '$b + {reason: "the previous load attempt never finished before a reboot (possible crash). Set retry_after_crash to try once more."}')"
  fi
fi

echo "$BOOT" > "$MARK"
sync
LLOG="$LOGS/load-${KREL}.log"
if ! /usr/local/bin/nv-load.sh "$CACHE" > "$LLOG" 2>&1; then
  rm -f "$MARK"   # a clean error return, not a crash
  dmesg 2>/dev/null | grep -iE 'nvrm|nvidia' | tail -n 20 >> "$LLOG" || true
  hold load_failed "$(jq -n --argjson b "$BUILT_ATTRS" --argjson t "$(tail_json "$LLOG" 30)" \
    '$b + {log_tail: $t}')"
fi
rm -f "$MARK"
cat "$LLOG"

# Keep the GPU initialised between clients so the driver manages clocks and power.
nvidia-smi -pm 1 > /dev/null 2>&1 || log "WARNING: persistence mode not enabled"

# ---- 4. Monitor -----------------------------------------------------------------------------
Q=name,pci.bus_id,memory.total,memory.used,temperature.gpu,power.draw,pstate,utilization.gpu,clocks.sm
while true; do
  if row=$(nvidia-smi --query-gpu="$Q" --format=csv,noheader,nounits 2>&1); then
    attrs=$(jq -n --argjson b "$BUILT_ATTRS" --arg r "$row" '
      ($r | split(", ")) as $f
      | $b + {gpu_name: $f[0], bus_id: $f[1], vram_total_mib: $f[2], vram_used_mib: $f[3],
              temperature_c: $f[4], power_w: $f[5], pstate: $f[6], utilization_pct: $f[7],
              sm_clock_mhz: $f[8]}')
    publish gpu_ready "$attrs"
  else
    publish load_failed "$(jq -n --argjson b "$BUILT_ATTRS" --arg e "$row" '$b + {log_tail: $e}')"
  fi
  sleep 60
done
