#!/usr/bin/env bash
# Whisper CUDA add-on.
#  1. GPU stage: build the NVIDIA driver for the running HAOS kernel (once per kernel), load it,
#     create /dev/nvidia*. Any failure falls back to the CPU instead of stopping speech-to-text.
#  2. Optional benchmark (option "benchmark").
#  3. Serve Wyoming faster-whisper on port 10300 (foreground, so the watchdog follows it).
# Status goes to sensor.whisper_cuda_status. GPU states: no_gpu, building, build_failed, built,
# load_refused, load_failed, gpu_ready.
set -uo pipefail
. /usr/local/bin/common.sh

KREL=$(uname -r)
CACHE="$DATA/$NV_VERSION/$KREL"
LOGS="$DATA/logs"
mkdir -p "$DATA" "$LOGS" /data/models
LOAD=$(opt load_driver)
JOBS=$(opt build_jobs)
RETRY=$(opt retry_after_crash)

log "Whisper CUDA: kernel $KREL, driver $NV_VERSION, load_driver=$LOAD"

GPU_STATE=""
ATTRS='{}'
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

  if [ ! -f "$CACHE/nvidia.ko" ]; then
    publish building "$(jq -n --argjson g "$ATTRS" --arg j "$JOBS" \
      '$g + {note: "first start on this kernel: about 10-20 min, speech-to-text starts afterwards", jobs: $j}')"
    local blog="$LOGS/build-${KREL}.log" patch_dir
    patch_dir=$(fetch_haos_patches)
    (
      flock -n 9 || { echo "another build is running"; exit 1; }
      rm -rf "$CACHE.tmp"
      /usr/local/bin/nv-build-modules.sh "$KREL" /proc/config.gz "$CACHE.tmp" "$JOBS" "$patch_dir" \
        && mv "$CACHE.tmp" "$CACHE"
    ) 9> "$DATA/build.lock" > "$blog" 2>&1
    if [ ! -f "$CACHE/nvidia.ko" ]; then
      GPU_STATE=build_failed
      ATTRS=$(jq -n --argjson g "$ATTRS" --argjson t "$(tail_json "$blog" 30)" '$g + {log_tail: $t}')
      return
    fi
    log "driver build finished"
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
  local boot mark llog
  boot=$(cat /proc/sys/kernel/random/boot_id)
  mark="$DATA/load-attempt"
  if [ -f "$mark" ] && ! grep -q '^nvidia ' /proc/modules \
     && [ "$(cat "$mark")" != "$boot" ] && [ "$RETRY" != true ]; then
    GPU_STATE=load_refused
    ATTRS=$(jq -n --argjson b "$ATTRS" \
      '$b + {reason: "the previous load attempt never finished before a reboot (possible crash). Set retry_after_crash to try once more."}')
    return
  fi
  echo "$boot" > "$mark"
  sync
  llog="$LOGS/load-${KREL}.log"
  if ! /usr/local/bin/nv-load.sh "$CACHE" > "$llog" 2>&1; then
    rm -f "$mark"   # a clean error return, not a crash
    dmesg 2>/dev/null | grep -iE 'nvrm|nvidia' | tail -n 20 >> "$llog" || true
    GPU_STATE=load_failed
    ATTRS=$(jq -n --argjson b "$ATTRS" --argjson t "$(tail_json "$llog" 30)" '$b + {log_tail: $t}')
    return
  fi
  rm -f "$mark"
  cat "$llog"
  # Keep the GPU initialised so the driver manages clocks and power between requests.
  nvidia-smi -pm 1 > /dev/null 2>&1 || log "WARNING: persistence mode not enabled"
  GPU_STATE=gpu_ready
}

gpu_stage

# ---- speech-to-text settings ------------------------------------------------------------------
BEAM=$(opt beam_size)
BIAS=$(opt ha_name_biasing)
VADF=$(opt vad_filter)
GPU_MODEL=$(opt model)
GPU_COMPUTE=$(opt compute_type)
CPU_MODEL=$(opt cpu_fallback_model)
# Thermal guard (laptop GPU): at HOT_C or a hardware thermal slowdown, serve from the CPU until the
# card is back under COOL_C. Sustained load on this machine reached 97 C, throttled to 139 MHz and
# eventually dropped the GPU off the PCIe bus, taking the whole host down (2026-10-25).
HOT_C=85
COOL_C=72
HOLD=/tmp/thermal_hold
PIDF=/tmp/stt.pid
[ "$GPU_STATE" = gpu_ready ] || log "speech-to-text on CPU ($GPU_STATE)"

# stt_mode -> "cuda" or "cpu" for the next server start
stt_mode() {
  if [ "$GPU_STATE" = gpu_ready ] && [ ! -f "$HOLD" ]; then echo cuda; else echo cpu; fi
}

stt_attrs() {
  local d m c
  d=$(stt_mode)
  if [ "$d" = cuda ]; then m=$GPU_MODEL; c=$GPU_COMPUTE; else m=$CPU_MODEL; c=int8; fi
  jq -n --arg d "$d" --arg m "$m" --arg c "$c" --arg b "$BEAM" --arg h "$BIAS" --arg v "$VADF" \
    --argjson t "$([ -f "$HOLD" ] && echo true || echo false)" \
    '{stt_device: $d, stt_model: $m, stt_compute_type: $c, stt_beam_size: $b, stt_ha_name_biasing: $h,
      stt_vad_filter: $v, thermal_hold: $t}'
}

# ---- status monitor + thermal guard (background) ----------------------------------------------
monitor() {
  local q=name,memory.total,memory.used,temperature.gpu,power.draw,pstate,utilization.gpu,clocks.sm,clocks_event_reasons.active
  local row a up b temp reasons hw n=0
  while true; do
    row=""
    if [ "$GPU_STATE" = gpu_ready ]; then
      row=$(nvidia-smi --query-gpu="$q" --format=csv,noheader,nounits 2>/dev/null || true)
      temp=$(echo "$row" | awk -F', ' '{print $4}')
      reasons=$(echo "$row" | awk -F', ' '{print $9}')
      if [[ "$temp" =~ ^[0-9]+$ ]]; then
        # 0x40 = HW thermal slowdown
        hw=0
        [[ "$reasons" =~ ^0x[0-9a-fA-F]+$ ]] && (( (reasons & 0x40) != 0 )) && hw=1
        if [ ! -f "$HOLD" ] && { [ "$temp" -ge "$HOT_C" ] || [ "$hw" = 1 ]; }; then
          log "THERMAL: GPU ${temp} C (reasons $reasons), moving speech-to-text to the CPU"
          touch "$HOLD"
          [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2> /dev/null
          n=0
        elif [ -f "$HOLD" ] && [ "$temp" -le "$COOL_C" ]; then
          log "THERMAL: GPU back to ${temp} C, returning speech-to-text to the GPU"
          rm -f "$HOLD"
          [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2> /dev/null
          n=0
        fi
      fi
    fi
    if [ $((n % 4)) -eq 0 ]; then   # publish every minute (and right after a switch)
      up=false
      nc -z -w 2 127.0.0.1 10300 2> /dev/null && up=true
      b=false
      [ -f /tmp/bench.running ] && b=true
      a=$(jq -n --argjson a "$ATTRS" --argjson s "$(stt_attrs)" --argjson u "$up" --argjson b "$b" \
        '$a + $s + {stt_listening: $u, benchmark_running: $b}')
      if [ -n "$row" ]; then
        a=$(jq -n --argjson a "$a" --arg r "$row" '($r | split(", ")) as $f
          | $a + {gpu_name: $f[0], vram_total_mib: $f[1], vram_used_mib: $f[2], temperature_c: $f[3],
                  power_w: $f[4], pstate: $f[5], utilization_pct: $f[6], sm_clock_mhz: $f[7],
                  throttle_reasons: $f[8]}')
      fi
      publish "$GPU_STATE" "$a"
    fi
    n=$((n + 1))
    sleep 15
  done
}
monitor &

# ---- optional benchmark -----------------------------------------------------------------------
if [ "$(opt benchmark)" = true ]; then
  touch /tmp/bench.running
  IFS=',' read -r -a BENCH_CONFIGS <<< "$(opt benchmark_configs)"
  /usr/local/bin/bench.py "${BENCH_CONFIGS[@]}" 2>&1 | tee "$LOGS/bench.log"
  if [ -f /data/bench/results.json ]; then
    publish_entity sensor.whisper_cuda_benchmark done "$(jq -c '{piper_cpu, results}' /data/bench/results.json)"
  fi
  rm -f /tmp/bench.running
fi

# ---- Home Assistant discovery (once the server answers) ----------------------------------------
(
  for _ in $(seq 1 600); do
    nc -z -w 2 127.0.0.1 10300 2> /dev/null && break
    sleep 2
  done
  discover_wyoming "tcp://$(hostname):10300"
) &

# ---- serve (restarted by the thermal guard to switch GPU <-> CPU) ------------------------------
if [ "$BIAS" = true ] && [ -n "${SUPERVISOR_TOKEN:-}" ]; then
  # Biases recognition toward the names of entities, areas and floors exposed to Assist.
  export WYO_WHISPER_HASS_TOKEN="$SUPERVISOR_TOKEN"
fi
trap '[ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2> /dev/null; exit 0' TERM INT
while true; do
  dev=$(stt_mode)
  if [ "$dev" = cuda ]; then model=$GPU_MODEL; compute=$GPU_COMPUTE; else model=$CPU_MODEL; compute=int8; fi
  # "hf:owner/repo#subfolder": CTranslate2 weights inside a larger repo -> download, pass the path.
  case "$model" in
    hf:*) model=$(/opt/venv/bin/python3 -c 'import sys; sys.path.insert(0, "/usr/local/bin"); from bench import resolve_model; print(resolve_model(sys.argv[1]))' "$model") ;;
  esac
  args=(--uri tcp://0.0.0.0:10300 --stt-library faster-whisper --model "$model" --device "$dev"
        --compute-type "$compute" --beam-size "$BEAM" --language auto
        --data-dir /data/models --download-dir /data/models)
  [ "$BIAS" = true ] && [ -n "${SUPERVISOR_TOKEN:-}" ] && args+=(--hass-api http://supervisor/core/api)
  [ "$VADF" = true ] && args+=(--vad-filter)
  [ "$(opt debug)" = true ] && args+=(--debug)
  log "starting faster-whisper: ${args[*]}"
  /opt/venv/bin/python3 -m wyoming_faster_whisper "${args[@]}" &
  echo $! > "$PIDF"
  wait "$(cat "$PIDF")"
  log "faster-whisper exited ($?), restarting"
  sleep 2
done
