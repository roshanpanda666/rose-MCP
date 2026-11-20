#!/usr/bin/env bash
# Reload ROSE's HTTP server with the latest code, keeping the tunnel (and its URL) up.
# rose-remote.sh runs the server in a restart loop, so stopping it is enough.
set -euo pipefail
pid="$(pgrep -f '\.venv/bin/python3? rose/server\.py --http' | head -1 || true)"
if [ -z "$pid" ]; then
  echo "ROSE HTTP server is not running (start it with: systemctl --user start rose-remote)" >&2
  exit 1
fi
kill "$pid"
for _ in $(seq 30); do
  sleep 1
  new="$(pgrep -f '\.venv/bin/python3? rose/server\.py --http' | head -1 || true)"
  if [ -n "$new" ] && [ "$new" != "$pid" ] && curl -s -o /dev/null "http://127.0.0.1:8765/.well-known/oauth-authorization-server"; then
    echo "ROSE reloaded (pid $new). URL unchanged: $(cat ~/rose/public_url.txt 2>/dev/null)"
    exit 0
  fi
done
echo "ROSE did not come back; check ~/rose/logs/http.log" >&2
exit 1
