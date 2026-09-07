#!/usr/bin/env bash
# LiveKit availability watchdog.
#
# Probes the LiveKit HTTP health endpoint and logs UP/DOWN transitions with
# UTC timestamps, so outages can be correlated with server-side logs
# (docker compose logs livekit, docker inspect, LXC journal) afterwards.
#
# Usage: scripts/lk_watchdog.sh [url] [interval_s] [duration_s]
#   url       LiveKit base URL (default: $LIVEKIT_URL or ws://localhost:7880;
#             ws/wss schemes are converted to http/https automatically)
#   interval  seconds between probes (default 10)
#   duration  total seconds to run (default 86400 = 24 h)
#
# Output (stdout): one "<utc> TRANSITION <prev>-><new>" line per state change
# and a summary line at the end. Redirect stdout to a file, e.g.:
#   scripts/lk_watchdog.sh http://192.168.0.29:7880 10 86400 >> /tmp/lk-watchdog.log 2>&1 &
set -u

URL="${1:-}"
INTERVAL="${2:-10}"
DURATION="${3:-86400}"

if [ -z "$URL" ]; then
  URL="${LIVEKIT_URL:-ws://localhost:7880}"
fi
URL="$(printf '%s' "$URL" | sed -e 's|^ws://|http://|' -e 's|^wss://|https://|')"

echo "$(date -u +%FT%TZ) watchdog: probing $URL every ${INTERVAL}s for ${DURATION}s"

start=$(date +%s)
prev=""
ok=0
fail=0
while [ "$(( $(date +%s) - start ))" -lt "$DURATION" ]; do
  if curl -fsS -m 3 --noproxy '*' "$URL/" >/dev/null 2>&1; then
    state=UP
    ok=$((ok + 1))
  else
    state=DOWN
    fail=$((fail + 1))
  fi
  if [ "$state" != "$prev" ]; then
    echo "$(date -u +%FT%TZ) TRANSITION ${prev:-start}->${state}"
    prev="$state"
  fi
  sleep "$INTERVAL"
done
echo "$(date -u +%FT%TZ) watchdog: finished ok=$ok fail=$fail"
