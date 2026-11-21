#!/usr/bin/env bash
# Whisper CUDA add-on: speech-to-text only. The NVIDIA Driver add-on owns the kernel module.
#  1. GPU usable? Read /share/nvidia/state.json (the driver add-on's contract). If not, serve from
#     the CPU at once: speech-to-text never waits for the GPU.
#  2. Optional benchmark (option "benchmark").
#  3. Serve Wyoming faster-whisper on port 10300. A monitor restarts the server on CUDA when the
#     GPU becomes usable, and on the CPU while the GPU is hot or unusable.
# Status goes to sensor.whisper_cuda_status: gpu_ready, gpu_unavailable or driver_mismatch.
set -uo pipefail
. /usr/local/bin/common.sh

STATE_FILE=/share/nvidia/state.json
BOOT=$(cat /proc/sys/kernel/random/boot_id)
LOGS="$DATA/logs"
mkdir -p "$DATA" "$LOGS" /data/models
# Up to 0.2.x this add-on built and loaded the driver; that moved to the NVIDIA Driver add-on.
rm -rf "$DATA"/[0-9]*.*.* "$DATA/haos-patches" "$DATA/load-attempt" "$DATA/build.lock" "$LOGS"/build-* "$LOGS"/load-*
# Local thermal fallback, used only while state.json is stale (driver add-on stopped): the same
# thresholds as the driver add-on. Sustained load on this machine reached 97 C and dropped the
# GPU off the PCIe bus, taking the whole host down (2026-10-25).
HOT_C=85
COOL_C=78
HOLD=/tmp/thermal_hold
PIDF=/tmp/stt.pid
MODE=/tmp/stt.mode   # device the running server was started on
WANT=/tmp/stt.want   # device it should run on (written by the monitor, read by the serve loop)

log "Whisper CUDA: userspace $NV_VERSION, reading $STATE_FILE"

# gpu_check -> sets GPU_STATE (gpu_ready|gpu_unavailable|driver_mismatch), GPU_REASON, FRESH
# (state.json fresh: true/false) and THERMAL (ok|hot, from state.json when fresh).
gpu_check() {
  local st loaded age
  FRESH=false; THERMAL=ok; GPU_REASON=""
  if [ ! -c /dev/nvidia-uvm ] || ! loaded=$(cat /sys/module/nvidia/version 2>/dev/null); then
    GPU_STATE=gpu_unavailable; GPU_REASON="driver not loaded"; return
  fi
  if [ "$loaded" != "$NV_VERSION" ]; then
    GPU_STATE=driver_mismatch; GPU_REASON="kernel driver $loaded, userspace $NV_VERSION"; return
  fi
  if st=$(jq -c --arg b "$BOOT" '. + {same_boot: (.boot_id == $b),
            age: (now - (.updated | fromdateiso8601) | floor)}' "$STATE_FILE" 2>/dev/null); then
    age=$(jq -r .age <<< "$st")
    if [ "$(jq -r .same_boot <<< "$st")" = true ] && [ "$age" -lt 60 ]; then
      FRESH=true
      THERMAL=$(jq -r '.thermal // "ok"' <<< "$st")
      GPU_STATE=$( [ "$(jq -r .state <<< "$st")" = ready ] && echo gpu_ready || echo gpu_unavailable )
      [ "$GPU_STATE" = gpu_ready ] || GPU_REASON="driver add-on: $(jq -r .state <<< "$st")"
      return
    fi
  fi
  # No fresh contract (driver add-on stopped or restarting). The driver stays loaded: keep using
  # the GPU if this add-on already serves on it, but never start on it without a fresh "ready".
  if [ "$(cat "$MODE" 2>/dev/null)" = cuda ]; then
    GPU_STATE=gpu_ready; GPU_REASON="state.json stale: local thermal guard"
  else
    GPU_STATE=gpu_unavailable; GPU_REASON="no fresh ready in $STATE_FILE"
  fi
}

gpu_check
[ "$THERMAL" = hot ] && touch "$HOLD"

# ---- speech-to-text settings ------------------------------------------------------------------
BEAM=$(opt beam_size)
BIAS=$(opt ha_name_biasing)
VADF=$(opt vad_filter)
GPU_MODEL=$(opt model)
GPU_COMPUTE=$(opt compute_type)
CPU_MODEL=$(opt cpu_fallback_model)
[ "$GPU_STATE" = gpu_ready ] || log "speech-to-text on CPU ($GPU_STATE: $GPU_REASON)"

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

# ---- monitor: follow the driver state + thermal (background) ---------------------------------
# Restarts the server (kill; the serve loop starts it again) whenever the right device changes.
monitor() {
  local q=name,memory.total,memory.used,temperature.gpu,power.draw,pstate,utilization.gpu,clocks.sm,clocks_event_reasons.active
  local row temp reasons hw want up b a n=0
  while true; do
    gpu_check
    row=""
    if [ "$GPU_STATE" = gpu_ready ]; then
      row=$(nvidia-smi --query-gpu="$q" --format=csv,noheader,nounits 2>/dev/null || true)
    fi
    if [ "$FRESH" = true ]; then
      # The driver add-on owns the policy.
      if [ "$THERMAL" = hot ]; then touch "$HOLD"; else rm -f "$HOLD"; fi
    elif [ -n "$row" ]; then
      # Local fallback while the contract is stale: 0x40 = HW thermal slowdown.
      temp=$(echo "$row" | awk -F', ' '{print $4}')
      reasons=$(echo "$row" | awk -F', ' '{print $9}')
      hw=0
      [[ "$reasons" =~ ^0x[0-9a-fA-F]+$ ]] && (( (reasons & 0x40) != 0 )) && hw=1
      if [[ "$temp" =~ ^[0-9]+$ ]]; then
        if [ ! -f "$HOLD" ] && { [ "$temp" -ge "$HOT_C" ] || [ "$hw" = 1 ]; }; then touch "$HOLD"
        elif [ -f "$HOLD" ] && [ "$temp" -le "$COOL_C" ]; then rm -f "$HOLD"
        fi
      fi
    fi
    want=$(stt_mode)
    echo "$want" > "$WANT"
    if [ -f "$MODE" ] && [ "$(cat "$MODE")" != "$want" ]; then
      log "switching speech-to-text $(cat "$MODE") -> $want ($GPU_STATE${GPU_REASON:+: $GPU_REASON}$([ -f "$HOLD" ] && echo ', thermal hold'))"
      [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2> /dev/null
      n=0
    fi
    if [ $((n % 4)) -eq 0 ]; then   # publish every minute (and right after a switch)
      up=false
      nc -z -w 2 127.0.0.1 10300 2> /dev/null && up=true
      b=false
      [ -f /tmp/bench.running ] && b=true
      a=$(jq -n --argjson s "$(stt_attrs)" --argjson u "$up" --argjson b "$b" --arg r "$GPU_REASON" \
        --argjson f "$FRESH" '$s + {stt_listening: $u, benchmark_running: $b, gpu_reason: $r, driver_state_fresh: $f}')
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
stt_mode > "$WANT"
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
  dev=$(cat "$WANT" 2>/dev/null || echo cpu)
  echo "$dev" > "$MODE"
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
