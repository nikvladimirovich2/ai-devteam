#!/usr/bin/env bash
# Pre-flight check for AI DevTeam: .env filled, containers healthy,
# every role reaches the LLM with ITS OWN key, Git token works.
# Usage:  ./scripts/check.sh            (from the repository root, stack must be up)
set -uo pipefail
cd "$(dirname "$0")/.."

ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✘\033[0m %s\n' "$*"; FAIL=1; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
FAIL=0
ROLES="lead architect backend frontend qa devops"

echo "1) .env"
if [[ ! -f .env ]]; then bad ".env not found (cp .env.example .env)"; exit 1; fi
perm=$(stat -c %a .env); [[ "$perm" == 600 ]] && ok ".env mode 600" || warn ".env mode $perm (recommended: chmod 600 .env)"
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -E "s/^'(.*)'$/\1/; s/^\"(.*)\"$/\1/"; }
# Since 0.2 the model and Git can be set up in the browser (⚙ Setup) instead of .env:
# then the .env checks and the model/Git checks below are done by the wizard's buttons.
WIZARD=0
if [[ -z "$(get LLM_MODEL)" ]]; then
  WIZARD=1
  warn "LLM_MODEL is not in .env: the model and Git are set up in the browser (⚙ Setup → Check and save); checks 3-5 skipped"
fi
if [[ $WIZARD == 0 ]]; then
for v in LLM_MODEL LLM_BASE_URL GIT_URL GIT_TOKEN; do
  [[ -n "$(get $v)" ]] && ok "$v set" || bad "$v is empty"
done
fi
s="$(get INTERNAL_SECRET)"
(( ${#s} >= 32 )) && ok "INTERNAL_SECRET set" || bad "INTERNAL_SECRET is shorter than 32 characters (openssl rand -hex 32)"
for r in $ROLES; do
  [[ $WIZARD == 1 ]] && break
  V="$(echo $r | tr a-z A-Z)_API_KEY"; val="$(get $V)"
  if [[ -z "$val" && -n "$(get LLM_API_KEY)" ]]; then continue; fi   # one key for the whole team
  if [[ -z "$val" ]]; then bad "$V is empty (or set LLM_API_KEY for all roles)"; continue; fi
  raw=$(grep -E "^$V=" .env | tail -1 | cut -d= -f2-)
  if [[ "$raw" == *'$'* && "$raw" != \'*\' ]]; then
    bad "$V contains \$ but is not in single quotes — Compose will cut it (use $V='...')"
  fi
done
ls agent/certs/*.crt >/dev/null 2>&1 && ok "CA certificates in agent/certs: $(ls agent/certs/*.crt | xargs -n1 basename | tr '\n' ' ')" \
  || warn "agent/certs/ has no *.crt (needed only for self-signed/corporate HTTPS)"

echo "2) containers"
for r in $ROLES; do
  st=$(docker inspect -f '{{.State.Health.Status}}' "devteam-$r" 2>/dev/null || echo missing)
  [[ "$st" == healthy ]] && ok "devteam-$r $st" || bad "devteam-$r $st (docker compose up -d; docker compose logs $r)"
done
[[ $FAIL -ne 0 ]] && { echo; echo "Fix the problems above and run again."; exit 1; }

if [[ $WIZARD == 1 ]]; then
  echo "6) UI"
  port=$(get UI_PORT); port=${port:-8080}
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health")
  [[ "$code" == 200 ]] && ok "http://127.0.0.1:$port/health → 200" || bad "UI health → $code"
  echo; [[ $FAIL -eq 0 ]] && echo "All checks passed. Open the UI and finish ⚙ Setup." || { echo "There are errors (see ✘)."; exit 1; }
  exit 0
fi

echo "3) LLM: each role with its own key"
for r in $ROLES; do
  out=$(docker exec -w /app "devteam-$r" timeout 180 python -c '
import common
llm = common.build_llm(common.ROLE)
r = llm.call("Reply with the single word: pong")
print("OK ctx=%s fc=%s | %s" % (llm.context_window_size, llm.supports_function_calling(), str(r).strip()[-40:].replace("\n"," ")))
' 2>&1 | grep -E '^OK|Error|error' | tail -1)
  [[ "$out" == OK* ]] && ok "$r: $out" || bad "$r: ${out:-no answer}"
done

echo "4) LLM tool calling (native)"
out=$(docker exec -w /app devteam-backend timeout 180 python -c '
import os, litellm, common
m, base = os.environ["LLM_MODEL"], os.environ.get("LLM_BASE_URL") or None
key = os.environ.get("BACKEND_API_KEY") or os.environ.get("LLM_API_KEY") or "none"
tools=[{"type":"function","function":{"name":"write_file","description":"write a file","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}}]
r = litellm.completion(model=m, api_base=base, api_key=key, tools=tools, tool_choice="auto", timeout=150,
      messages=[{"role":"user","content":"Create file hello.txt with content hi. Use the tool."}])
tc = r.choices[0].message.tool_calls
print("OK tool_call=" + tc[0].function.name if tc else "NO tool_calls in response")
' 2>&1 | grep -E '^OK|^NO|Error' | tail -1)
[[ "$out" == OK* ]] && ok "$out" || bad "${out:-no answer} — vLLM needs --enable-auto-tool-choice --tool-call-parser <parser>"

echo "5) Git (token in devops only)"
out=$(docker exec -w /app devteam-devops timeout 60 python -c '
import httpx, repo_tool
c = repo_tool.cfg(); v = not c["insecure"]
if c["provider"] == "github":
    host = c["url"].split("//")[-1].split("/")[0]
    api = "https://api.github.com" if host in ("github.com","www.github.com") else c["url"] + "/api/v3"
    h = {"Authorization": "Bearer " + c["token"], "Accept": "application/vnd.github+json"}
    u = httpx.get(api + "/user", headers=h, verify=v, timeout=30)
    if u.status_code != 200: raise SystemExit("token: HTTP %s" % u.status_code)
    msg = "github user=" + u.json()["login"]
    if c["namespace"]:
        o = httpx.get(api + "/orgs/" + c["namespace"], headers=h, verify=v, timeout=30)
        msg += " org=%s:%s" % (c["namespace"], o.status_code)
else:
    import urllib.parse as q
    h = {"PRIVATE-TOKEN": c["token"]}
    u = httpx.get(c["url"] + "/api/v4/user", headers=h, verify=v, timeout=30)
    if u.status_code != 200: raise SystemExit("token: HTTP %s" % u.status_code)
    n = httpx.get(c["url"] + "/api/v4/namespaces/" + q.quote(c["namespace"], safe=""), headers=h, verify=v, timeout=30)
    s = httpx.get(c["url"] + "/api/v4/personal_access_tokens/self", headers=h, verify=v, timeout=30)
    scopes = s.json().get("scopes") if s.status_code == 200 else "?"
    msg = "gitlab user=%s namespace=%s:%s scopes=%s" % (u.json()["username"], c["namespace"], n.status_code, scopes)
print("OK " + msg)
' 2>&1 | tail -1)
[[ "$out" == OK* ]] && ok "$out" || bad "$out"
for r in lead architect backend frontend qa; do
  docker exec "devteam-$r" sh -c 'test -z "$GIT_TOKEN"' && : || bad "GIT_TOKEN leaked into devteam-$r"
done
# The agents' shell runs as "runner": it must not see the agent's environment (keys, token)
# and must not be able to talk to the internal endpoints without a token.
out=$(docker exec -u runner devteam-devops sh -c 'cat /proc/1/environ >/dev/null 2>&1 && echo LEAK || echo ok')
[[ "$out" == ok ]] && ok "shell user cannot read the agent environment" || bad "shell user can read /proc/1/environ"
code=$(docker exec -u runner devteam-backend curl -s -o /dev/null -w '%{http_code}' -X POST http://devops:8000/publish -H 'Content-Type: application/json' -d '{}')
[[ "$code" == 403 ]] && ok "internal endpoints reject unsigned calls (403)" || bad "devops /publish without token → $code (expected 403)"

echo "6) UI"
port=$(get UI_PORT); port=${port:-8080}
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health")
[[ "$code" == 200 ]] && ok "http://127.0.0.1:$port/health → 200" || bad "UI health → $code"

echo
[[ $FAIL -eq 0 ]] && echo "All checks passed." || { echo "There are errors (see ✘)."; exit 1; }
