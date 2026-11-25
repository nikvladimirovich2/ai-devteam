#!/usr/bin/env bash
# End-to-end smoke test without a real model or Git host (runs in CI, works locally with Docker):
# builds the image, starts the 6 agents + the mock LLM, configures them through the setup wizard
# API (like a user in the browser), runs one task through every role and checks the isolation.
# Publishing fails on purpose (no Git host): status done_no_push.
#
#   bash tests/smoke.sh          # KEEP=1 leaves the containers running
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="$(mktemp)"
PORT=18080
{
  echo "DEVTEAM_IMAGE=devteam-agent:latest"
  echo "INTERNAL_SECRET=$(openssl rand -hex 32)"
  echo "UI_PORT=$PORT"
  echo "MAX_CI_ROUNDS=0"
} > "$ENV_FILE"
DC=(docker compose --env-file "$ENV_FILE" -f docker-compose.yml -f docker-compose.build.yml -f tests/docker-compose.ci.yml)

fail() { echo "FAIL: $*"; "${DC[@]}" ps; "${DC[@]}" logs --tail 80; exit 1; }
cleanup() { [[ "${KEEP:-0}" == 1 ]] || "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; rm -f "$ENV_FILE"; }
trap cleanup EXIT
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
post() { curl -fs -X POST "$B$1" -H 'Content-Type: application/json' -d "$2" "${@:3}"; }

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

echo "== setup wizard"
[[ $(curl -fs "$B/api/setup" | json 'd["llm"]["configured"]') == False ]] || fail "model configured before setup"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/api/tasks" -H 'Content-Type: application/json' -d '{"prompt":"x"}')
[[ "$code" == 400 ]] || fail "task accepted without a model ($code)"
r=$(post /api/setup/llm '{"provider":"vllm","base_url":"http://mockllm:9000/v1","model":"mock","api_key":"sk-smoke-SECRET","lang":"ru"}')
echo "llm: $r"
[[ $(json 'd["ok"] and d.get("saved", False)' <<<"$r") == True ]] || fail "model check/save"
curl -fs "$B/api/setup" | grep -q "sk-smoke-SECRET" && fail "the key is returned by /api/setup"
# a role with its own model (same provider/server): architect on another model, with its own key
r=$(post /api/setup/role '{"role":"architect","model":"mock-architect","api_key":"sk-architect-SECRET"}')
echo "role: $r"
[[ $(json 'd["ok"] and d["model"] == "hosted_vllm/mock-architect"' <<<"$r") == True ]] || fail "role model check/save"
[[ $(curl -fs "$B/api/setup" | json 'd["roles"]["architect"]["effective"] + " " + d["roles"]["backend"]["effective"]') == "hosted_vllm/mock-architect hosted_vllm/mock" ]] \
  || fail "effective role models"
curl -fs "$B/api/setup" | grep -q "sk-architect-SECRET" && fail "a role key is returned by /api/setup"
r=$(post /api/setup/git '{"provider":"gitlab","url":"http://no-such-git.invalid","token":"glpat-smoke","namespace":"x","lang":"ru"}')
echo "git: $r"
[[ $(json 'd["code"]' <<<"$r") == unreachable ]] || fail "git check of a missing host should say unreachable"
grep -q "Сервер не отвечает" <<<"$r" || fail "no Russian message"
# notifications: a webhook (the mock LLM records it); the URL carries a key and must never come back
r=$(post /api/setup/notify '{"action":"test","webhook_url":"http://mockllm:9000/hook?key=smoke-hook-secret"}')
echo "notify test: $r"
[[ $(json 'd["ok"]' <<<"$r") == True ]] || fail "notification test"
post /api/setup/notify '{"action":"save","webhook_url":"http://mockllm:9000/hook?key=smoke-hook-secret"}' >/dev/null
curl -fs "$B/api/setup" | grep -q "smoke-hook-secret" && fail "the webhook URL is returned by /api/setup"
[[ $(curl -fs "$B/api/setup" | json 'd["notify"]["webhook"]') == True ]] || fail "webhook not saved"

echo "== one task through every role"
TID=$(post /api/tasks '{"prompt":"Tiny TODO API (smoke test)"}' | json 'd["id"]')
status=""
for i in $(seq 1 60); do
  status=$(curl -fs "$B/api/tasks/$TID" | json 'd["status"]')
  case "$status" in running|queued|pending) sleep 5 ;; *) break ;; esac
done
report=$(curl -fs "$B/api/tasks/$TID/report" | json 'd["report"]')
echo "$report" | head -30
[[ "$status" == done_no_push ]] || fail "task status $status (expected done_no_push)"
grep -q "QA: passed" <<<"$report" || fail "QA did not pass"
grep -q "backend/test_app.py" <<<"$report" || fail "backend file missing"
grep -E "^ +Architect .*hosted_vllm/mock-architect$" <<<"$report" >/dev/null || fail "report: architect model"
grep -E "^ +Backend .*hosted_vllm/mock$" <<<"$report" >/dev/null || fail "report: backend model"
[[ $(curl -fs "$B/api/tasks/$TID" | json 'd["tokens"] > 0') == True ]] || fail "no tokens counted for the task"
for i in $(seq 1 10); do      # the notification is sent right after the status changes
  hooks=$(docker exec devteam-mockllm curl -fs http://localhost:9000/hooks)
  grep -q "$TID" <<<"$hooks" && break
  sleep 2
done
python3 -c "import json,sys; h=json.load(sys.stdin); t=[x for x in h if x.get('task_id')=='$TID']; assert t and t[0]['status']=='done_no_push' and t[0]['report'], h" <<<"$hooks" \
  || fail "no webhook notification for the task: $hooks"

echo "== budget: a run stops cleanly when the limit is used up"
r=$(post /api/setup/limits '{"budget_tokens":"1"}')
echo "limits: $r"
[[ $(json 'd["budget_tokens"]' <<<"$r") == 1 ]] || fail "limits not saved"
BID=$(post /api/tasks '{"prompt":"Budget smoke test"}' | json 'd["id"]')
bstatus=""
for i in $(seq 1 36); do
  bstatus=$(curl -fs "$B/api/tasks/$BID" | json 'd["status"]')
  case "$bstatus" in running|queued) sleep 5 ;; *) break ;; esac
done
curl -fs "$B/api/tasks/$BID/report" | json 'd["report"]'
[[ "$bstatus" == budget ]] || fail "budget task status $bstatus (expected budget)"
curl -fs "$B/api/tasks/$BID" | json 'd["result"]' | grep -q "💸" || fail "no budget message in the result"
post /api/setup/limits '{}' >/dev/null
[[ $(curl -fs "$B/api/setup" | json 'd["limits"]["budget_tokens"]') == 0 ]] || fail "limits not cleared"

echo "== Stop interrupts a running step (a 10-minute command is killed)"
SID=$(post /api/tasks '{"prompt":"SMOKE-SLOW stop test"}' | json 'd["id"]')
for i in $(seq 1 40); do
  ev=$(curl -s --max-time 2 "$B/api/tasks/$SID/events" || true)
  grep -q "sleep 600" <<<"$ev" && break
  [[ $i == 40 ]] && fail "the slow command did not start"
  sleep 3
done
sleep 3
docker exec devteam-backend pgrep -f "sleep 600" >/dev/null || fail "the slow command is not running before Stop"
post "/api/tasks/$SID/cancel" '{}' >/dev/null
t0=$(date +%s); sstatus=""
for i in $(seq 1 30); do
  sstatus=$(curl -fs "$B/api/tasks/$SID" | json 'd["status"]')
  case "$sstatus" in running|queued) sleep 2 ;; *) break ;; esac
done
echo "stopped in $(( $(date +%s) - t0 ))s: $sstatus"
[[ "$sstatus" == cancelled ]] || fail "status after Stop: $sstatus (expected cancelled)"
if docker exec devteam-backend pgrep -f "sleep 600" >/dev/null; then fail "the command survived Stop"; fi
ev=$(curl -s --max-time 2 "$B/api/tasks/$SID/events" || true)
grep -qi "stop requested" <<<"$ev" || fail "no stop event"

echo "== isolation of the agents"
# the shell user of the agents cannot read the agent's environment, nor the devops Git store
if docker exec -u runner devteam-devops sh -c 'cat /proc/1/environ >/dev/null 2>&1'; then fail "runner can read /proc/1/environ"; fi
if docker exec -u runner devteam-devops sh -c 'ls /devops-data >/dev/null 2>&1'; then fail "runner can read /devops-data"; fi
if docker exec -u runner devteam-lead sh -c 'ls /data >/dev/null 2>&1'; then fail "runner can read the lead database"; fi
# through the real tool path (sudo env -i): no secrets in the command's environment
n=$(docker exec -w /app devteam-backend python -c '
import subprocess, common
r = subprocess.run(common.shell_argv("env | grep -cE \"API_KEY|SECRET|TOKEN\" || true"), capture_output=True, text=True, env=common._shell_env())
print(r.stdout.strip())')
[[ "$n" == 0 ]] || fail "secrets in the shell environment: $n"
# internal endpoints reject calls without the token
for url in http://devops:8000/publish http://devops:8000/run http://devops:8000/config/git http://lead:8000/api/internal/events; do
  code=$(docker exec -u runner devteam-backend curl -s -o /dev/null -w '%{http_code}' -X POST "$url" -H 'Content-Type: application/json' -d '{"task_id":"x","role":"devops","kind":"gitlab","message":"forged"}')
  [[ "$code" == 403 ]] || fail "$url without token -> $code (expected 403)"
done

echo "== interface password"
post /api/setup/ui '{"user":"admin","password":"smoke-pass-123"}' >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' "$B/api/tasks")
[[ "$code" == 401 ]] || fail "no password asked after setting one ($code)"
curl -fs -u admin:smoke-pass-123 "$B/api/tasks" >/dev/null || fail "login with the new password"
echo "OK: smoke test passed"
