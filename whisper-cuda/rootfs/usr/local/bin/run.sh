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
if [ "$GPU_STATE" = gpu_ready ]; then
  DEVICE=cuda
  MODEL=$(opt model)
  COMPUTE=$(opt compute_type)
else
  DEVICE=cpu
  MODEL=$(opt cpu_fallback_model)
  COMPUTE=int8
  log "speech-to-text on CPU ($GPU_STATE)"
fi
BEAM=$(opt beam_size)
BIAS=$(opt ha_name_biasing)
STT_ATTRS=$(jq -n --arg d "$DEVICE" --arg m "$MODEL" --arg c "$COMPUTE" --arg b "$BEAM" --arg h "$BIAS" \
  '{stt_device: $d, stt_model: $m, stt_compute_type: $c, stt_beam_size: $b, stt_ha_name_biasing: $h}')

# ---- optional benchmark -----------------------------------------------------------------------
if [ "$(opt benchmark)" = true ]; then
  publish "$GPU_STATE" "$(jq -n --argjson a "$ATTRS" --argjson s "$STT_ATTRS" '$a + $s + {note: "benchmark running"}')"
  /usr/local/bin/bench.py 2>&1 | tee "$LOGS/bench.log"
  if [ "$GPU_STATE" = gpu_ready ]; then
    /usr/local/bin/bench_piper_gpu.sh 2>&1 | tee "$LOGS/bench_piper_gpu.log" || true
  fi
  if [ -f /data/bench/results.json ]; then
    pg='{}'
    [ -f /data/bench/piper_gpu.json ] && pg=$(jq -c . /data/bench/piper_gpu.json)
    publish_entity sensor.whisper_cuda_benchmark done \
      "$(jq -c --argjson pg "$pg" '{piper_cpu, piper_gpu: $pg, results}' /data/bench/results.json)"
  fi
fi

# ---- status monitor (background) -----------------------------------------------------------------
monitor() {
  local q=name,memory.total,memory.used,temperature.gpu,power.draw,pstate,utilization.gpu,clocks.sm row a up
  while true; do
    up=false
    nc -z -w 2 127.0.0.1 10300 2> /dev/null && up=true
    a=$(jq -n --argjson a "$ATTRS" --argjson s "$STT_ATTRS" --argjson u "$up" '$a + $s + {stt_listening: $u}')
    if [ "$GPU_STATE" = gpu_ready ] && row=$(nvidia-smi --query-gpu="$q" --format=csv,noheader,nounits 2>&1); then
      a=$(jq -n --argjson a "$a" --arg r "$row" '($r | split(", ")) as $f
        | $a + {gpu_name: $f[0], vram_total_mib: $f[1], vram_used_mib: $f[2], temperature_c: $f[3],
                power_w: $f[4], pstate: $f[5], utilization_pct: $f[6], sm_clock_mhz: $f[7]}')
    fi
    publish "$GPU_STATE" "$a"
    sleep 60
  done
}
monitor &

# ---- Home Assistant discovery (once the server answers) ----------------------------------------
(
  for _ in $(seq 1 600); do
    nc -z -w 2 127.0.0.1 10300 2> /dev/null && break
    sleep 2
  done
  discover_wyoming "tcp://$(hostname):10300"
) &

# ---- serve ------------------------------------------------------------------------------------
args=(--uri tcp://0.0.0.0:10300 --stt-library faster-whisper --model "$MODEL" --device "$DEVICE"
      --compute-type "$COMPUTE" --beam-size "$BEAM" --language auto
      --data-dir /data/models --download-dir /data/models)
if [ "$BIAS" = true ] && [ -n "${SUPERVISOR_TOKEN:-}" ]; then
  # Biases recognition toward the names of entities, areas and floors exposed to Assist.
  export WYO_WHISPER_HASS_TOKEN="$SUPERVISOR_TOKEN"
  args+=(--hass-api http://supervisor/core/api)
fi
[ "$(opt debug)" = true ] && args+=(--debug)
log "starting faster-whisper: ${args[*]}"
exec /opt/venv/bin/python3 -m wyoming_faster_whisper "${args[@]}"
