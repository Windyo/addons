#!/usr/bin/env bash
# Shared helpers for the Whisper CUDA add-on.
NV_VERSION=$(cat /opt/nvidia/VERSION)
DATA=/data/nvidia
STATUS_ENTITY=sensor.whisper_cuda_status

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

opt() { jq -r ".$1" /data/options.json; }

# publish <state> [json-attributes]
# Writes the add-on status to Home Assistant (needs homeassistant_api: true).
publish() {
  local state=$1 attrs=${2:-'{}'}
  log "status: $state"
  [ -n "${SUPERVISOR_TOKEN:-}" ] || return 0
  jq -n --arg s "$state" --argjson a "$attrs" --arg k "$(uname -r)" --arg d "$NV_VERSION" \
    '{state: $s, attributes: ({friendly_name: "Whisper CUDA status", icon: "mdi:expansion-card",
       kernel: $k, driver: $d, updated: (now | todate)} + $a)}' \
    | curl -fsS -m 10 -X POST \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
        -d @- "http://supervisor/core/api/states/${STATUS_ENTITY}" > /dev/null \
    || log "WARNING: could not publish status"
}

# tail_json <file> [lines] -> JSON string with the last lines of a log
tail_json() { tail -n "${2:-25}" "$1" 2>/dev/null | jq -Rs . ; }

# hold <state> [json-attributes]: keep re-publishing (states vanish on a Core restart).
hold() {
  local state=$1 attrs=${2:-'{}'}
  while true; do
    publish "$state" "$attrs"
    sleep 300
  done
}
