#!/usr/bin/env bash
# Start ROSE remote access: Cloudflare quick tunnel + ROSE HTTP server (auth required).
#
# Order matters for safety:
#   1. refuse to start unless ROSE auth (passphrase or token) is configured
#   2. open the tunnel (it serves only 502s until ROSE is up) and read its random URL
#   3. point ROSE's public_url at it and start ROSE on 127.0.0.1
#   4. self-check: the public URL must answer 401 to an unauthenticated request,
#      otherwise everything is shut down
# The connector URL is printed, saved to ~/rose/public_url.txt and shown as a notification.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROSE_HOME="${ROSE_HOME:-$HOME/rose}"
LOG_DIR="$ROSE_HOME/logs"
mkdir -p "$LOG_DIR"
cd "$ROOT"

PORT="$(uv run python -c 'import sys; sys.path.insert(0,"."); from rose.config import load_config; print(load_config().get("http_port", 8765))')"

if ! uv run python -c 'import sys; sys.path.insert(0,"."); from rose.auth import auth_configured; sys.exit(0 if auth_configured() else 1)'; then
  echo "ROSE auth is not configured. Run: uv run python -m rose.auth set-passphrase" >&2
  exit 1
fi

pids=()
cleanup() {
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  rm -f "$ROSE_HOME/public_url.txt"
}
trap cleanup EXIT INT TERM

TUNNEL_LOG="$LOG_DIR/tunnel.log"
: > "$TUNNEL_LOG"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > "$TUNNEL_LOG" 2>&1 &
pids+=($!)

URL=""
for _ in $(seq 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 0.5
done
if [ -z "$URL" ]; then
  echo "cloudflared did not report a URL; see $TUNNEL_LOG" >&2
  exit 1
fi

# Point ROSE at the new public URL (OAuth issuer + allowed Host header).
uv run python - "$URL" <<'EOF'
import sys
sys.path.insert(0, ".")
from rose.config import set_public_url
set_public_url(sys.argv[1])
EOF

# The server runs in a restart loop so code updates can be loaded without touching the tunnel
# (a new quick tunnel means a new URL): scripts/rose-reload.sh just restarts the server process.
( while true; do
    uv run rose/server.py --http --port "$PORT" >> "$LOG_DIR/http.log" 2>&1 || true
    sleep 2
  done ) &
pids+=($!)

# Wait for ROSE locally, then for the tunnel to route to it.
for _ in $(seq 40); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/.well-known/oauth-authorization-server" && break
  sleep 0.5
done

STATUS=""
for _ in $(seq 60); do
  STATUS="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' || true)"
  [ "$STATUS" = "401" ] && break
  case "$STATUS" in 2*) break ;; esac
  sleep 1
done
if [ "$STATUS" != "401" ]; then
  echo "SAFETY CHECK FAILED: unauthenticated request to $URL/mcp returned '$STATUS' (expected 401). Shutting down." >&2
  exit 1
fi

echo "$URL/mcp" > "$ROSE_HOME/public_url.txt"
echo "ROSE is live (auth enforced): $URL/mcp"
notify-send --app-name=ROSE "ROSE remote access is live" "Connector URL: $URL/mcp" 2>/dev/null || true

# Stay up while both processes run; if either dies, stop the other (systemd restarts us).
wait -n "${pids[@]}"
echo "A ROSE remote process exited; shutting down." >&2
exit 1
