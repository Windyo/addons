#!/usr/bin/env bash
# Shared helpers for the NVIDIA driver add-on.
NV_VERSION=$(cat /opt/nvidia/VERSION)
DATA=/data/nvidia
STATUS_ENTITY=sensor.nvidia_gpu_status

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

opt() { jq -r ".$1" /data/options.json; }

# publish <state> [json-attributes]
# Writes the add-on status to Home Assistant (needs homeassistant_api: true). Core comes up after
# the add-on at boot (the Supervisor answers 502 until then): one line when publishing starts
# failing, one when it works again, nothing in between.
publish() {
  local state=$1 attrs=${2:-'{}'} code rc
  [ "$state" = "${_LAST_STATE:-}" ] || log "status: $state"
  _LAST_STATE=$state
  [ -n "${SUPERVISOR_TOKEN:-}" ] || return 0
  if code=$(jq -n --arg s "$state" --argjson a "$attrs" --arg k "$(uname -r)" --arg d "$NV_VERSION" \
    '{state: $s, attributes: ({friendly_name: "NVIDIA GPU status", icon: "mdi:expansion-card",
       kernel: $k, driver: $d, updated: (now | todate)} + $a)}' \
    | curl -fs -m 10 -o /dev/null -w '%{http_code}' -X POST \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
        -d @- "http://supervisor/core/api/states/${STATUS_ENTITY}"); then
    [ -z "${_PUBLISH_ERR:-}" ] || log "HA Core reachable again, status published"
    _PUBLISH_ERR=""
  else
    rc=$?
    [ "$rc" = 22 ] && code="HTTP $code" || code="curl error $rc"
    [ -n "${_PUBLISH_ERR:-}" ] || log "WARNING: HA Core not reachable ($code), status not published yet"
    _PUBLISH_ERR=1
  fi
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

# fetch_haos_patches -> prints a directory holding HAOS's kernel patches for the installed OS
# version (from the operating-system repo at that tag). Falls back to the bundled 18.3 copy.
fetch_haos_patches() {
  local ver dir api list
  ver=$(curl -fsS -m 10 -H "Authorization: Bearer ${SUPERVISOR_TOKEN:-}" http://supervisor/os/info \
        | jq -r '.data.version // empty' 2>/dev/null)
  if [ -n "$ver" ]; then
    dir="$DATA/haos-patches/$ver"
    if [ -f "$dir/.complete" ]; then echo "$dir"; return; fi
    api="https://api.github.com/repos/home-assistant/operating-system/contents/buildroot-external/patches/linux?ref=${ver}"
    if list=$(curl -fsS -m 20 "$api" | jq -r '.[] | select(.name | endswith(".patch")) | "\(.name) \(.download_url)"'); then
      rm -rf "$dir"; mkdir -p "$dir"
      local ok=1 name url
      while read -r name url; do
        [ -n "$name" ] || continue
        curl -fsS -m 30 -o "$dir/$name" "$url" || ok=0
      done <<< "$list"
      if [ "$ok" = 1 ]; then touch "$dir/.complete"; log "HAOS $ver kernel patches: $(ls "$dir" | wc -l)" >&2; echo "$dir"; return; fi
    fi
    log "WARNING: could not fetch kernel patches for HAOS $ver, using the bundled 18.3 set" >&2
  fi
  echo /usr/local/share/nvidia-driver/linux-patches
}
