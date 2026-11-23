#!/usr/bin/env bash
# NVIDIA driver add-on: the only add-on that touches the kernel.
#  1. Build the NVIDIA modules for the running HAOS kernel (once per kernel), load them, create
#     /dev/nvidia*, turn persistence mode on.
#  2. Every 15 s: apply the thermal policy and write /share/nvidia/state.json, the only interface
#     GPU consumers rely on. Every minute: sensor.nvidia_gpu_status.
# States: no_gpu, building, build_failed, built, load_refused, load_failed, ready, gpu_error.
# Stopping the add-on does not unload the driver; unloading needs a host reboot.
set -uo pipefail
. /usr/local/bin/common.sh

KREL=$(uname -r)
CACHE="$DATA/$NV_VERSION/$KREL"
LOGS="$DATA/logs"
SHARE=/share/nvidia
STATE_FILE="$SHARE/state.json"
HEARTBEAT=/tmp/heartbeat
mkdir -p "$DATA" "$LOGS" "$SHARE"
LOAD=$(opt load_driver)
JOBS=$(opt build_jobs)
RETRY=$(opt retry_after_crash)
HOT_C=$(opt hot_c)
COOL_C=$(opt cool_c)
BOOT=$(cat /proc/sys/kernel/random/boot_id)

GPU_STATE=starting
ATTRS='{}'
THERMAL=ok
TEMP=""
VRAM_T=""
VRAM_U=""

log "NVIDIA driver: kernel $KREL, driver $NV_VERSION, load_driver=$LOAD, thermal ${HOT_C}/${COOL_C} C"

# write_state: atomic write of the consumer contract (docs/plans/gpu-stack/plan.md §3).
# Consumers use the GPU only if state == ready, boot_id is theirs, and updated is < 60 s old.
write_state() {
  jq -n --arg b "$BOOT" --arg s "$GPU_STATE" --arg k "$KREL" --arg t "$THERMAL" \
    --arg d "$(cat /sys/module/nvidia/version 2>/dev/null || echo "$NV_VERSION")" \
    --arg temp "$TEMP" --arg vt "$VRAM_T" --arg vu "$VRAM_U" \
    '{boot_id: $b, state: $s, driver: $d, kernel: $k, thermal: $t,
      temperature_c: ($temp | tonumber? // null), vram_total_mib: ($vt | tonumber? // null),
      vram_used_mib: ($vu | tonumber? // null), updated: (now | todate)}' > "$STATE_FILE.tmp" \
    && mv "$STATE_FILE.tmp" "$STATE_FILE"
}

# set_state <state>: record a stage transition everywhere at once.
set_state() {
  GPU_STATE=$1
  write_state
  publish "$GPU_STATE" "$ATTRS"
}

# build_modules: build the cached modules for this kernel (one build at a time).
build_modules() {
  local blog="$LOGS/build-${KREL}.log" patch_dir
  patch_dir=$(fetch_haos_patches)
  (
    flock -n 9 || { echo "another build is running"; exit 1; }
    rm -rf "$CACHE.tmp"
    /usr/local/bin/nv-build-modules.sh "$KREL" /proc/config.gz "$CACHE.tmp" "$JOBS" "$patch_dir" \
      && mv "$CACHE.tmp" "$CACHE"
  ) 9> "$DATA/build.lock" > "$blog" 2>&1
}

# load_modules: nv-load.sh with its log; sets GPU_STATE=load_failed on a clean error.
load_modules() {
  local llog="$LOGS/load-${KREL}.log"
  if ! /usr/local/bin/nv-load.sh "$CACHE" > "$llog" 2>&1; then
    dmesg 2>/dev/null | grep -iE 'nvrm|nvidia' | tail -n 20 >> "$llog" || true
    GPU_STATE=load_failed
    ATTRS=$(jq -n --argjson b "$ATTRS" --argjson t "$(tail_json "$llog" 30)" '$b + {log_tail: $t}')
    return 1
  fi
  cat "$llog"
}

# gpu_stage sets GPU_STATE and ATTRS. It never exits the add-on.
gpu_stage() {
  local gpu="" gpu_id="" d
  for d in /sys/bus/pci/devices/*; do
    [ "$(cat "$d/vendor")" = 0x10de ] || continue
    case "$(cat "$d/class")" in
      0x0300*|0x0302*) gpu="${d##*/}"; gpu_id=$(cat "$d/device"); break ;;
    esac
  done
  if [ -z "$gpu" ]; then
    GPU_STATE=no_gpu; ATTRS='{"reason": "no NVIDIA display/3D controller on the PCI bus"}'; return
  fi
  ATTRS=$(jq -n --arg p "$gpu" --arg i "$gpu_id" '{pci: $p, device_id: $i}')

  # Already loaded this boot (by whisper-cuda 0.2.x or an earlier start of this add-on): only make
  # sure the nodes exist. Build the cache in the background so the next boot loads at once.
  if grep -q '^nvidia ' /proc/modules; then
    load_modules || return
    if [ ! -f "$CACHE/nvidia.ko" ]; then
      log "driver already loaded; building the module cache in the background for the next boot"
      build_modules &
    fi
    GPU_STATE=ready
    return
  fi

  if [ ! -f "$CACHE/nvidia.ko" ]; then
    ATTRS=$(jq -n --argjson g "$ATTRS" --arg j "$JOBS" \
      '$g + {note: "first start on this kernel: about 10-20 min", jobs: $j}')
    set_state building
    build_modules
    if [ ! -f "$CACHE/nvidia.ko" ]; then
      GPU_STATE=build_failed
      ATTRS=$(jq -n --argjson g "$ATTRS" --argjson t "$(tail_json "$LOGS/build-${KREL}.log" 30)" \
        '$g + {log_tail: $t}')
      return
    fi
    log "driver build finished"
    ATTRS=$(jq -n --argjson g "$ATTRS" '$g | del(.note, .jobs)')
  fi
  ATTRS=$(jq -n --argjson g "$ATTRS" --arg v "$(cat "$CACHE/vermagic")" \
    --argjson drift "$(jq -Rsc 'split("\n") | map(select(length > 0))' "$CACHE/config-drift.txt")" \
    '$g + {vermagic: $v, config_drift: $drift}')

  if [ "$LOAD" != true ]; then
    GPU_STATE=built
    ATTRS=$(jq -n --argjson b "$ATTRS" '$b + {note: "driver built, not loaded (load_driver is off)"}')
    return
  fi

  # Crash guard: a marker holds the boot id of an unfinished load. Finding it after a reboot
  # means the last attempt may have taken the host down, so never retry that automatically.
  local mark="$DATA/load-attempt"
  if [ -f "$mark" ] && [ "$(cat "$mark")" != "$BOOT" ] && [ "$RETRY" != true ]; then
    GPU_STATE=load_refused
    ATTRS=$(jq -n --argjson b "$ATTRS" \
      '$b + {reason: "the previous load attempt never finished before a reboot (possible crash). Set retry_after_crash to try once more."}')
    return
  fi
  echo "$BOOT" > "$mark"
  sync
  if ! load_modules; then
    rm -f "$mark"   # a clean error return, not a crash
    return
  fi
  rm -f "$mark"
  GPU_STATE=ready
}

gpu_stage
# Modules already loaded but the GPU gone (Xid 79 needs a power-off): say so now, not after 3 polls.
if [ "$GPU_STATE" = ready ] && ! nvidia-smi -L > /dev/null 2>&1; then
  GPU_STATE=gpu_error
  log "ERROR: nvidia-smi cannot reach the GPU -> gpu_error (a full power-off is needed after Xid 79)"
fi
if [ "$GPU_STATE" = ready ]; then
  # Keep the GPU initialised so the driver manages clocks and power between requests.
  nvidia-smi -pm 1 > /dev/null 2>&1 || log "WARNING: persistence mode not enabled"
  # Read-only: which power and clock controls this card exposes ([N/A] = not adjustable). The card
  # faults intermittently under sustained full load; a power or clock cap is the fix if one exists.
  caps_q=power.management,power.limit,power.default_limit,power.min_limit,power.max_limit,clocks.max.sm,clocks.max.mem,clocks.default_applications.graphics,clocks.default_applications.memory
  caps=$(nvidia-smi --query-gpu="$caps_q" --format=csv,noheader 2>&1 | head -1)
  log "power/clock controls ($caps_q): $caps"
  sup=$(nvidia-smi -q -d SUPPORTED_CLOCKS 2>&1 | grep -cE '^\s+Graphics\s*:' || true)
  log "supported graphics clocks listed: $sup"
  ATTRS=$(jq -n --argjson a "$ATTRS" --arg q "$caps_q" --arg v "$caps" --arg n "$sup" \
    '$a + {power_clock_controls: ([($q | split(",")), ($v | split(", "))] | transpose | map({(.[0]): .[1]}) | add),
           supported_graphics_clocks: ($n | tonumber? // $n)}')
  # NVML reports no power limit on this laptop GPU; vendor tools (Gigabyte Control Center) go through
  # the laptop firmware instead. Log what the platform exposes, read-only.
  dmi=$(for f in sys_vendor product_name product_version board_name bios_version; do
    printf '%s=%s; ' "$f" "$(cat /sys/class/dmi/id/$f 2>/dev/null || echo '?')"; done)
  log "platform: $dmi"
  log "WMI GUIDs: $(ls /sys/bus/wmi/devices 2>/dev/null | tr '\n' ' ')"
  # Gigabyte's Command & Control drives GB_WMIACPI_Get/Set (WMI methods WMBC/WMBD in the ACPI
  # tables): Smart Turbo, Turbo Mode, fan modes. Decompile the tables and log those two methods so the
  # command codes and EC fields this model uses are known before anything is written.
  if [ -r /sys/firmware/acpi/tables/DSDT ]; then
    acpi=/tmp/acpi; rm -rf "$acpi"; mkdir -p "$acpi"
    for t in /sys/firmware/acpi/tables/DSDT /sys/firmware/acpi/tables/SSDT*; do
      [ -r "$t" ] && cat "$t" > "$acpi/$(basename "$t").aml"
    done
    (cd "$acpi" && iasl -d ./*.aml > /dev/null 2>&1 || true)
    mkdir -p /share/nvidia/acpi && cp "$acpi"/*.dsl /share/nvidia/acpi/ 2>/dev/null || true
    for m in WMBC WMBD; do
      awk -v m="$m" '$0 ~ "Method \\(" m "," {p=1; d=0}
        p {f=FILENAME; sub(/.*\//, "", f); print f ": " $0; n=gsub(/\{/,"{"); c=gsub(/\}/,"}"); d+=n-c; if (d<=0 && n+c>0 && ++k) p=0; if (++lines>600) exit}' \
        "$acpi"/*.dsl | while IFS= read -r l; do log "acpi $m: $l"; done
    done
  else
    log "ACPI tables not readable"
  fi
  log "ACPI platform_profile: $(cat /sys/firmware/acpi/platform_profile 2>/dev/null || echo none) (choices: $(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null || echo none))"
  log "temperature controls: $(nvidia-smi -q -d TEMPERATURE 2>&1 | grep -iE 'target|slowdown|shutdown|max operating' | sed 's/  */ /g' | tr '\n' ';')"
  # Clock lock test with the full supported range (no effective change), then reset. Tells whether
  # nvidia-smi -lgc works on this card at all.
  lo=$(nvidia-smi -q -d SUPPORTED_CLOCKS 2>/dev/null | awk '/Graphics/ {gsub(/[^0-9]/, "", $3); print $3}' | sort -n | head -1)
  hi=$(nvidia-smi -q -d SUPPORTED_CLOCKS 2>/dev/null | awk '/Graphics/ {gsub(/[^0-9]/, "", $3); print $3}' | sort -n | tail -1)
  lgc=$(nvidia-smi -lgc "${lo:-0},${hi:-0}" 2>&1 | tr '\n' ' ')
  rgc=$(nvidia-smi -rgc 2>&1 | tr '\n' ' ')
  log "clock lock test -lgc ${lo},${hi}: $lgc | reset: $rgc"
  ATTRS=$(jq -n --argjson a "$ATTRS" --arg d "$dmi" --arg l "$lgc" \
    '$a + {platform: $d, clock_lock_test: $l}')
fi
set_state "$GPU_STATE"

# kernel_gpu_lines -> JSON array of the last NVIDIA error lines in the kernel log (Xid codes say
# what the GPU did: 13 graphics exception, 31 MMU fault, 43 channel reset, 62 microcontroller halt,
# 79 fallen off the bus). Reading the kernel log needs CAP_SYSLOG or CAP_SYS_ADMIN (we have the latter).
kernel_gpu_lines() {
  # No match is the normal case: grep's status must not fail the pipeline (pipefail), or the
  # fallback below would print a second array and break every caller's --argjson.
  { dmesg 2>/dev/null | grep -E 'NVRM: (Xid|GPU at|GPU has fallen|.*fault)|nvidia.*(error|fault)' || true; } \
    | tail -n 12 | jq -Rsc 'split("\n") | map(select(length > 0))'
}

# ---- monitor: thermal policy, state.json, sensor ----------------------------------------------
# Thermal policy (laptop GPU): hot at HOT_C or on a hardware thermal slowdown, ok again at COOL_C.
# Consumers follow "thermal" (Whisper moves to the CPU, Ollama refuses chats). Sustained load on
# this machine reached 97 C and eventually dropped the GPU off the PCIe bus (2026-10-25).
trap 'exit 0' TERM INT
q=name,memory.total,memory.used,temperature.gpu,power.draw,pstate,utilization.gpu,clocks.sm,clocks_event_reasons.active
n=0
fails=0
while true; do
  row=""
  procs='[]'
  if [ "$GPU_STATE" = ready ] || [ "$GPU_STATE" = gpu_error ]; then
    if row=$(nvidia-smi --query-gpu="$q" --format=csv,noheader,nounits 2>/dev/null) && [ -n "$row" ]; then
      fails=0
      if [ "$GPU_STATE" = gpu_error ]; then GPU_STATE=ready; log "nvidia-smi answers again"; n=0; fi
      IFS=',' read -r _ VRAM_T VRAM_U TEMP _ _ _ _ reasons <<< "${row// /}"
      hw=0
      [[ "$reasons" =~ ^0x[0-9a-fA-F]+$ ]] && (( (reasons & 0x40) != 0 )) && hw=1   # HW thermal slowdown
      if [[ "$TEMP" =~ ^[0-9]+$ ]]; then
        if [ "$THERMAL" = ok ] && { [ "$TEMP" -ge "$HOT_C" ] || [ "$hw" = 1 ]; }; then
          THERMAL=hot; n=0
          log "THERMAL: GPU ${TEMP} C (reasons $reasons) -> hot"
        elif [ "$THERMAL" = hot ] && [ "$TEMP" -le "$COOL_C" ] && [ "$hw" = 0 ]; then
          THERMAL=ok; n=0
          log "THERMAL: GPU back to ${TEMP} C -> ok"
        fi
      fi
      procs=$({ nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null || true; } \
        | jq -Rsc 'split("\n") | map(select(length > 0) | split(", ") | {pid: .[0], process: .[1], vram_mib: .[2]})')
    else
      row=""
      fails=$((fails + 1))
      # Three silent polls in a row: tell consumers to stop using the GPU.
      if [ "$fails" -ge 3 ] && [ "$GPU_STATE" = ready ]; then
        GPU_STATE=gpu_error; n=0
        log "ERROR: nvidia-smi failed $fails times in a row -> gpu_error"
        kernel_gpu_lines | jq -r '.[]' | while read -r l; do log "kernel: $l"; done
      fi
    fi
  fi
  write_state
  touch "$HEARTBEAT"
  if [ $((n % 4)) -eq 0 ]; then   # every minute, and right after a change
    a=$(jq -n --argjson a "$ATTRS" --arg t "$THERMAL" --argjson p "$procs" --arg h "$HOT_C" --arg c "$COOL_C" \
      --argjson nb "$([ -f "$CACHE/nvidia.ko" ] && echo true || echo false)" \
      --argjson k "$(kernel_gpu_lines)" \
      '$a + {thermal: $t, hot_c: $h, cool_c: $c, processes: $p, next_boot_cache: $nb, kernel_gpu_errors: $k}')
    if [ -n "$row" ]; then
      a=$(jq -n --argjson a "$a" --arg r "$row" '($r | split(", ")) as $f
        | $a + {gpu_name: $f[0], vram_total_mib: $f[1], vram_used_mib: $f[2], temperature_c: $f[3],
                power_w: $f[4], pstate: $f[5], utilization_pct: $f[6], sm_clock_mhz: $f[7],
                throttle_reasons: $f[8]}')
    fi
    publish "$GPU_STATE" "$a"
  fi
  n=$((n + 1))
  sleep 15 &
  wait $!
done
