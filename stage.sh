#!/bin/sh
# Tells the dashboard which setup stage this run is in (a short code only: no names or addresses).
[ -n "$RUNNER_API_URL" ] || exit 0
curl -s -m 15 -o /dev/null -X POST "$RUNNER_API_URL/miinspact-report" \
  -H "X-Runner-Token: $RUNNER_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"heartbeat_only\": true, \"run_id\": \"$RUN_ID\", \"run_started_at\": ${RUN_STARTED:-0}, \"stage\": \"$1\"}" || true
