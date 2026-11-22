#!/usr/bin/env bash
# End-to-end smoke test without a real model or Git host (runs in CI, works locally with Docker):
# builds the image, starts the 6 agents + the mock LLM, runs one task through every role, and
# checks the agent isolation. Publishing fails on purpose (no Git host): status done_no_push.
#
#   bash tests/smoke.sh          # KEEP=1 leaves the containers running
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="$(mktemp)"
PORT=18080
{
  echo "LLM_MODEL=openai/mock"
  echo "LLM_BASE_URL=http://mockllm:9000/v1"
  for r in LEAD ARCHITECT BACKEND FRONTEND QA DEVOPS; do echo "${r}_API_KEY=mock-${r,,}"; done
  echo "INTERNAL_SECRET=$(openssl rand -hex 32)"
  echo "UI_PORT=$PORT"
  echo "MAX_CI_ROUNDS=0"
} > "$ENV_FILE"
DC=(docker compose --env-file "$ENV_FILE" -f docker-compose.yml -f tests/docker-compose.ci.yml)

fail() { echo "FAIL: $*"; "${DC[@]}" ps; "${DC[@]}" logs --tail 80; exit 1; }
cleanup() { [[ "${KEEP:-0}" == 1 ]] || "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; rm -f "$ENV_FILE"; }
trap cleanup EXIT

"${DC[@]}" build
"${DC[@]}" up -d
B="http://127.0.0.1:$PORT"

echo "== waiting for the team"
for i in $(seq 1 60); do
  team=$(curl -fs "$B/api/team" || true)
  if [[ -n "$team" ]] && python3 -c 'import json,sys; t=json.load(sys.stdin); sys.exit(0 if all(v.get("ok") for v in t.values()) else 1)' <<<"$team"; then
    echo "all 6 agents up"; break
  fi
  [[ $i == 60 ]] && fail "agents not healthy after 5 min"
  sleep 5
done

echo "== one task through every role"
TID=$(curl -fs -X POST "$B/api/tasks" -H 'Content-Type: application/json' -d '{"prompt":"Tiny TODO API (smoke test)"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
status=""
for i in $(seq 1 60); do
  status=$(curl -fs "$B/api/tasks/$TID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  case "$status" in running|queued|pending) sleep 5 ;; *) break ;; esac
done
report=$(curl -fs "$B/api/tasks/$TID/report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["report"])')
echo "$report" | head -30
[[ "$status" == done_no_push ]] || fail "task status $status (expected done_no_push)"
grep -q "QA: passed" <<<"$report" || fail "QA did not pass"
grep -q "backend/test_app.py" <<<"$report" || fail "backend file missing"

echo "== isolation of the agents"
# the shell user of the agents cannot read the agent's environment (API key, Git token)
if docker exec -u runner devteam-devops sh -c 'cat /proc/1/environ >/dev/null 2>&1'; then fail "runner can read /proc/1/environ"; fi
# through the real tool path (sudo env -i): no secrets in the command's environment
n=$(docker exec -w /app devteam-backend python -c '
import subprocess, common
r = subprocess.run(common.shell_argv("env | grep -cE \"API_KEY|SECRET|TOKEN\" || true"), capture_output=True, text=True, env=common._shell_env())
print(r.stdout.strip())')
[[ "$n" == 0 ]] || fail "secrets in the shell environment: $n"
# internal endpoints reject calls without the token
for url in http://devops:8000/publish http://devops:8000/run http://lead:8000/api/internal/events; do
  code=$(docker exec -u runner devteam-backend curl -s -o /dev/null -w '%{http_code}' -X POST "$url" -H 'Content-Type: application/json' -d '{"task_id":"x","role":"devops","kind":"gitlab","message":"forged"}')
  [[ "$code" == 403 ]] || fail "$url without token -> $code (expected 403)"
done
echo "OK: smoke test passed"
