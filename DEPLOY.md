# Deployment guide

Step-by-step installation of AI DevTeam on a clean host. Takes ~15 minutes plus image build time (~5 min).

## 1. Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Linux x86_64 (Debian 12/13, Ubuntu 22.04+, Astra, RHEL 9) | — |
| CPU / RAM | 4 vCPU / 8 GB | 8 vCPU / 16 GB (agents run pip/npm/pytest) |
| Disk | 20 GB | 50 GB (image ~1.7 GB + projects + pip/npm caches) |
| Software | Docker Engine 24+, Docker Compose v2, git | — |
| Network | HTTPS to the LLM API and to GitLab/GitHub; pypi.org / registry.npmjs.org for the agents | — |

The LLM itself is **not** part of this stack — the agents call an existing OpenAI-compatible endpoint
(vLLM, Ollama, LiteLLM proxy, GigaChat, …). No GPU is needed on this host.

## 2. Install Docker

Debian / Ubuntu (distribution packages):

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose docker-buildx git curl jq
sudo systemctl enable --now docker
docker version && docker compose version
```

(Or Docker CE from download.docker.com — any Docker 24+ with the `compose` plugin works.)
Non-root user: `sudo usermod -aG docker $USER` and log in again.

## 3. Get the code

```bash
sudo mkdir -p /opt/devteam && sudo chown $USER: /opt/devteam
git clone https://github.com/<you>/ai-devteam.git /opt/devteam
cd /opt/devteam
```

## 4. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Required:

| Variable | Example | Notes |
|---|---|---|
| `LLM_MODEL` | `hosted_vllm/Qwen/Qwen3-32B-AWQ` | LiteLLM format `<provider>/<model id from /v1/models>` |
| `LLM_BASE_URL` | `https://llm.corp.local/v1` | for vLLM/OpenAI-compatible APIs — including `/v1` |
| `LEAD_API_KEY` … `DEVOPS_API_KEY` | | 6 personal keys, one per role |
| `GIT_URL` | `https://gitlab.corp.local` or `https://github.com` | provider is detected from the URL |
| `GIT_TOKEN` | | see «Git tokens» below |
| `GIT_NAMESPACE` | `ai-team` | GitLab group (required) / GitHub org (empty = personal account) |

Recommended:

| Variable | Why |
|---|---|
| `UI_USER`, `UI_PASSWORD` | Basic Auth for the web UI |
| `ARCHITECT_MODEL`, `BACKEND_MODEL`, … | a different model per role — see [MODELS.md](MODELS.md) |

> **`$` in values.** Compose expands `$VAR` inside `.env`. A key containing `$` must be in single quotes:
> `QA_API_KEY='$abc#def'`, otherwise part of it is silently dropped.

### Git tokens

**GitLab** — Personal or Group Access Token, scopes `api` + `write_repository`
(the user must be allowed to create projects in `GIT_NAMESPACE`).

**GitHub** — fine-grained PAT: Settings → Developer settings → Fine-grained tokens:
- Resource owner = the org from `GIT_NAMESPACE` (or yourself), **Repository access: All repositories**
  (agents create new repositories — «Only select» does not cover them);
- Repository permissions: **Administration RW, Contents RW, Workflows RW, Actions R** (Metadata R is automatic).
- In an org: fine-grained tokens must be allowed (and approved if approval is on), SAML SSO authorized.

Classic PAT alternative: scopes `repo` + `workflow`.

### Self-signed / corporate certificates

If the LLM or GitLab is served over HTTPS with a private CA:

```bash
cp /path/to/ca-or-server.crt agent/certs/       # PEM, -----BEGIN CERTIFICATE-----
```

It is baked into the image (system store + certifi + `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`).
The certificate SAN must contain the host from `LLM_BASE_URL` (for an IP — `IP Address:`).
`agent/certs/*.crt` is git-ignored: copy it on every host.

## 5. LLM server requirements

The agents work through **tool calls** (`write_file`, `run_shell`, `publish_repository`).
The model endpoint must return native `tool_calls`:

- **vLLM**: start with `--enable-auto-tool-choice --tool-call-parser <parser>`
  (`hermes` for Qwen2.5/Qwen3, `llama3_json` for Llama 3.x, `mistral` for Mistral, …).
- **Ollama**: use a model with the `tools` capability.
- Context: ≥ 32k tokens recommended (`--max-model-len 32768`); the window is read automatically from
  vLLM `/v1/models` (`max_model_len`) or set via `LLM_CONTEXT_WINDOW`.

Quick manual check:

```bash
curl -s --cacert agent/certs/llm.crt "$LLM_BASE_URL/models" -H "Authorization: Bearer <key>" | jq '.data[].id'
```

## 6. Start

```bash
docker compose up -d --build
docker compose ps            # 6 containers, all (healthy) after ~30 s
./scripts/check.sh           # end-to-end pre-flight check
```

`scripts/check.sh` verifies: `.env` filled and `$`-quoting, certificates, container health,
**each role reaches the LLM with its own key**, native tool calling works, Git token/namespace,
the Git token is present only in `devops`, UI health.

## 7. Open the UI

The UI listens on `127.0.0.1:${UI_PORT:-8080}` only. From your workstation:

```bash
ssh -L 8080:localhost:8080 user@<host>                      # direct
ssh -J jump-user@jump-host:port -L 8080:localhost:8080 user@<host>   # via a jump host
# → http://localhost:8080
```

To expose it on the network: `UI_BIND=0.0.0.0` **and** `UI_USER`/`UI_PASSWORD`, preferably behind
a reverse proxy with TLS (nginx/traefik).

## 8. First task

Send something small first (see the README «Example task»). In the feed you should see:
`🔎 architect: +N files` → `🔎 backend/frontend: …` → QA verdict → `✅ pushed …` → `⏳ Waiting for CI…` →
`✅ CI passed` → final plain-text report with tokens per role.

## 9. Operations

```bash
docker compose logs -f lead devops         # logs
docker compose restart                     # after editing .env (or: up -d to recreate)
git pull && docker compose up -d --build   # update
docker compose down                        # stop, data kept
docker compose down -v                     # stop and DELETE task history and workspaces
```

Backup — two volumes:

```bash
docker run --rm -v devteam_leaddata:/d -v $PWD:/b alpine tar czf /b/leaddata.tgz -C /d .
docker run --rm -v devteam_workspace:/d -v $PWD:/b alpine tar czf /b/workspace.tgz -C /d .
```

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `The "X" variable is not set` on `docker compose` | a value in `.env` contains `$` — wrap it in single quotes |
| `CERTIFICATE_VERIFY_FAILED` | put the CA/server certificate into `agent/certs/`, `docker compose build && up -d` |
| `401` from the LLM | wrong `<ROLE>_API_KEY`; `./scripts/check.sh` shows which role |
| Agent «reported» work but `🔎 … did not create files` | model does not return tool calls → vLLM flags above, `LLM_FUNCTION_CALLING=auto` |
| `Action 'write_file' don't exist` / loops | weak tool-calling model for this role → use a stronger one (MODELS.md) |
| `GitLab namespace … not found` | wrong `GIT_NAMESPACE` or the token's user has no access to it |
| GitHub `403` on repo creation | fine-grained token not approved in the org / no Administration RW / SSO not authorized |
| `⚠ CI: no_pipeline` | no CI config pushed, CI disabled in the project, or no runners |
| CI red after `MAX_CI_ROUNDS` | open the task → **🔧 CI** to run more fix rounds, or fix manually |
| Lead container `unhealthy`, UI frozen | `docker compose logs lead`; `docker compose restart lead` (running task becomes `interrupted`) |
