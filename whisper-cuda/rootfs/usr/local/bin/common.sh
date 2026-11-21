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

# publish_entity <entity_id> <state> <json-attributes>
publish_entity() {
  [ -n "${SUPERVISOR_TOKEN:-}" ] || return 0
  jq -n --arg s "$2" --argjson a "$3" '{state: $s, attributes: $a}' \
    | curl -fsS -m 10 -X POST \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
        -d @- "http://supervisor/core/api/states/$1" > /dev/null \
    || log "WARNING: could not publish $1"
}

# discover_wyoming <uri>: announce the server so Home Assistant offers to add it.
discover_wyoming() {
  [ -n "${SUPERVISOR_TOKEN:-}" ] || return 0
  jq -n --arg u "$1" '{service: "wyoming", config: {uri: $u}}' \
    | curl -fsS -m 10 -X POST \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
        -d @- http://supervisor/discovery > /dev/null \
    && log "announced $1 to Home Assistant" || log "WARNING: discovery failed"
}
