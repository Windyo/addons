#!/usr/bin/env bash
# Web search add-on: SearXNG (127.0.0.1:8888, internal to the container) + the MCP server (:8765).
# If either process exits, the add-on exits and the Supervisor watchdog restarts it.
set -euo pipefail

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

CONF=/data/searxng
mkdir -p "$CONF"
# The secret is generated on the device, never stored in git.
[ -s "$CONF/secret" ] || python3 -c 'import secrets; print(secrets.token_hex(32))' > "$CONF/secret"
cat > "$CONF/settings.yml" <<EOF
use_default_settings:
  engines:
    keep_only: [duckduckgo, brave, bing, qwant, wikipedia, wikidata, bing news, qwant news]
general:
  instance_name: home-web-search
server:
  bind_address: 127.0.0.1
  port: 8888
  secret_key: "$(cat "$CONF/secret")"
  limiter: false
  public_instance: false
  image_proxy: false
search:
  safe_search: 1
  formats: [html, json]
outgoing:
  request_timeout: 4.0
EOF
export SEARXNG_SETTINGS_PATH="$CONF/settings.yml"

log "starting SearXNG"
(cd /opt/searxng/src && exec /opt/searxng/venv/bin/python -m searx.webapp) &
for _ in $(seq 1 60); do
  /opt/mcp/bin/python -c 'import httpx; httpx.get("http://127.0.0.1:8888/healthz", timeout=2).raise_for_status()' 2>/dev/null && break
  sleep 1
done
log "starting MCP server on :8765/mcp"
/opt/mcp/bin/python /usr/local/bin/mcp_server.py &

trap 'kill 0' TERM INT
wait -n || true
log "a process exited; stopping so the watchdog restarts the add-on"
kill 0
