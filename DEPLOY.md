**English** · [Русский](DEPLOY.ru.md) · [Srpski](DEPLOY.sr.md) · [中文](DEPLOY.zh.md)

# Deployment guide

Step-by-step installation of AI DevTeam on a clean host. Takes ~15 minutes.

**The quick way** is the one-command installer from the [README](README.md#quick-start): it downloads the
prebuilt image (`ghcr.io/nikvladimirovich2/ai-devteam`), starts the team and opens the browser, where the
setup wizard (⚙ Setup) asks for the model key and the GitHub/GitLab token. This guide is for servers,
closed networks and everything that can be configured in `.env` instead.

**Windows?** Run it with the WSL container CLI (`wslc`), no Docker Desktop needed: see
[11. Windows: WSL containers (wslc)](#11-windows-wsl-containers-wslc).

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
echo "INTERNAL_SECRET=$(openssl rand -hex 32)" >> .env
$EDITOR .env
```

Only `INTERNAL_SECRET` is required. The model, the key and Git can be set **either** here **or** in the
browser (⚙ Setup → *Check and save*). Values in `.env` win; the wizard shows them as «set in .env».
Wizard settings are stored in the volumes: model and key in `leaddata` (lead), the Git token in
`devopsdata` (devops only, mode 0600, not readable by the agents' shell user).

Configuring in `.env`:

| Variable | Example | Notes |
|---|---|---|
| `LLM_MODEL` | `hosted_vllm/Qwen/Qwen3-32B-AWQ` | LiteLLM format `<provider>/<model id from /v1/models>` |
| `LLM_BASE_URL` | `https://llm.corp.local/v1` | for vLLM/OpenAI-compatible APIs — including `/v1` |
| `LLM_API_KEY` | | one key for the whole team… |
| `LEAD_API_KEY` … `DEVOPS_API_KEY` | | …or 6 personal keys, one per role (win over `LLM_API_KEY`) |
| `GIT_URL` | `https://gitlab.corp.local` or `https://github.com` | provider is detected from the URL |
| `GIT_TOKEN` | | see «Git tokens» below |
| `GIT_NAMESPACE` | `ai-team` | GitLab group (required) / GitHub org (empty = personal account) |
| `INTERNAL_SECRET` | `openssl rand -hex 32` | ≥ 32 characters; signs the requests between the containers (see below) |

Recommended:

| Variable | Why |
|---|---|
| `UI_USER`, `UI_PASSWORD` | Basic Auth for the web UI |
| `ARCHITECT_MODEL`, `BACKEND_MODEL`, … | a different model per role — see [MODELS.md](MODELS.md) |

> **`$` in values.** Compose expands `$VAR` inside `.env`. A key containing `$` must be in single quotes:
> `QA_API_KEY='$abc#def'`, otherwise part of it is silently dropped.

### Isolation of the agents

The agents run shell commands the model writes, and a prompt injection (a README of a package, a web
page, the task text) can make the model run anything. Two layers keep the keys and the Git token away
from those commands:

- **Separate shell user.** The service runs as `agent` (uid 1000) and holds the role's API key (and,
  in `devops`, `GIT_TOKEN`). Every command from `run_shell` runs as `runner` (uid 1001) with a clean
  environment, so `/proc/<pid>/environ` and the memory of the agent are not readable. Both users
  share a group, so both can edit the project files. The Git token is passed to `git push` through
  the environment, never on the command line (`/proc/<pid>/cmdline` is readable by every user).
- **Signed internal requests.** The lead and the workers sign every internal request with a per-role
  HMAC token derived from `INTERNAL_SECRET`. Without it, `POST devops:8000/publish` and
  `POST lead:8000/api/internal/events` return `403`, so a command cannot publish under the DevOps
  token or post a fake `✅ pushed` event. The role of an event is taken from the token, not from
  the request body. A worker only holds its own token; the secret itself stays in the lead.

`scripts/check.sh` verifies both (`shell user cannot read the agent environment`, `internal
endpoints reject unsigned calls`). Still: give the token only the rights it needs, preferably for
a separate org / account.

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
docker compose restart
```

The folder (`CERTS_DIR`, default `./agent/certs`; the installers use `./certs`) is mounted at `/certs` in every
container; at start the certificates are added to the trusted bundle for Python (certifi,
`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`), git (`GIT_SSL_CAINFO`) and node (`NODE_EXTRA_CA_CERTS`). No rebuild needed.
When you build the image yourself, certificates in `agent/certs/` are also baked into it.
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
docker compose up -d         # pulls the prebuilt image
docker compose ps            # 6 containers, all (healthy) after ~30 s
./scripts/check.sh           # end-to-end pre-flight check
```

No access to ghcr.io (closed network)? Build the image on the host instead:
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`,
or load it from a file: `docker save ghcr.io/nikvladimirovich2/ai-devteam:latest | gzip > devteam.tgz` on a machine
with internet access, then `docker load < devteam.tgz`.

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
docker compose pull && docker compose up -d   # update (source builds: git pull + the build file)
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
| `CERTIFICATE_VERIFY_FAILED` | put the CA/server certificate into the certs folder (`CERTS_DIR`), `docker compose restart` |
| The wizard says «The key was rejected» / «does not call tools» | the message tells what to do; the technical detail is under it |
| `401` from the LLM | wrong `<ROLE>_API_KEY`; `./scripts/check.sh` shows which role |
| Agent «reported» work but `🔎 … did not create files` | model does not return tool calls → vLLM flags above, `LLM_FUNCTION_CALLING=auto` |
| `Action 'write_file' don't exist` / loops | weak tool-calling model for this role → use a stronger one (MODELS.md) |
| `GitLab namespace … not found` | wrong `GIT_NAMESPACE` or the token's user has no access to it |
| GitHub `403` on repo creation | fine-grained token not approved in the org / no Administration RW / SSO not authorized |
| `⚠ CI: no_pipeline` | no CI config pushed, CI disabled in the project, or no runners |
| CI red after `MAX_CI_ROUNDS` | open the task → **🔧 CI** to run more fix rounds, or fix manually |
| Lead container `unhealthy`, UI frozen | `docker compose logs lead`; `docker compose restart lead` (running task becomes `interrupted`) |
| `INTERNAL_SECRET is missing or shorter than 32 characters` | add `INTERNAL_SECRET=$(openssl rand -hex 32)` to `.env`, `docker compose up -d` |
| `403 internal endpoint` in the logs | containers started with different `INTERNAL_SECRET` values (old containers): recreate all with `docker compose up -d --force-recreate` |

## 11. Windows: WSL containers (wslc)

`wslc.exe` is the container CLI built into WSL. It runs the same image in a lightweight Linux VM,
without Docker Desktop. Tested on Windows 10 22H2 (build 19044) with WSL 3.0.3.

### 11.1 Requirements

| | Minimum | Notes |
|---|---|---|
| Windows | Windows 10 2004+ (build 19041) or Windows 11, x64 | |
| WSL | **3.0.1 or newer** (`wsl --version`) | `wslc` ships with WSL, nothing else to install |
| Virtualization | VT-x/AMD-V in the BIOS + the «Virtual Machine Platform» feature | inside a VM: nested virtualization |
| RAM | 8 GB on the host | measured: ~2 GB for the 6 idle agents. The wslc VM gets 4 GB by default (`wslc settings`); with pip/npm/pytest running in parallel raise it to 6 GB |
| Disk | 10 GB free on the drive with `%LOCALAPPDATA%\wslc` | image 1.66 GB, the VM disk `storage.vhdx` grows to ~3.3 GB after the build, plus projects and caches |
| Software | PowerShell 5.1+ (built in), git | `docker` / `docker compose` are **not** needed |
| Network | the LLM endpoint, GitLab/GitHub, pypi.org, registry.npmjs.org | the VM uses the Windows network, including VPN |

The LLM is not part of the stack: a laptop without a GPU works fine as long as `LLM_BASE_URL` points to a model server.

### 11.2 Install / update WSL

In PowerShell **as administrator**, only if WSL has never been installed:

```powershell
wsl --install --no-distribution     # enables «Virtual Machine Platform»; restart Windows afterwards
```

Then (as a normal user):

```powershell
wsl --update
wsl --version                       # WSL version: 3.0.1 or newer
wslc version
wslc run --rm hello-world           # smoke test of the container runtime
```

### 11.3 Install (one command)

In PowerShell (no administrator rights needed once WSL is installed):

```powershell
irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 | iex
```

The installer:
- runs `wsl --update` if `wslc` is missing (and says what to do if WSL is not installed at all);
- puts the start/stop scripts into `%USERPROFILE%\ai-devteam`, picks a free port (8080, 8081, …) for `.env`;
- downloads the prebuilt image (~1.7 GB) and starts the six containers;
- opens the browser on the setup wizard and creates the **AI DevTeam** desktop shortcut (start after a restart).

Company certificates: `%USERPROFILE%\ai-devteam\certs\*.crt`, then start again with the shortcut.

### 11.4 From the source code (developers)

```powershell
git clone https://github.com/nikvladimirovich2/ai-devteam.git
cd ai-devteam
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build   # builds the image here (~8 min)
```

Windows specifics of `.env` (optional, see [4. Configure `.env`](#4-configure-env)):

- `INTERNAL_SECRET` may stay empty: the start script generates it and writes it into `.env`.
- Values are read literally by the script, so `$` in keys needs no quoting (quotes are still accepted).
- Port 8080 is often busy on a developer machine: set e.g. `UI_PORT=8099`.
  `netstat -ano | findstr :8099` shows whether the port is free.
- Corporate certificates go to `agent\certs\*.crt` (or `CERTS_DIR`).
- Do not commit `.env`. It is in `.gitignore`. To restrict it to your user: `icacls .env /inheritance:r /grant:r "$($env:USERNAME):F"`.
- Git with `core.autocrlf=true` (the Git for Windows default) checks the files out with CRLF.
  This was tested: the image builds and runs. The script writes the per-role env files with LF itself.

wslc has no `compose` yet, so `scripts\wslc-up.ps1` does what `docker-compose.yml` does: one network
`devteam`, volumes `devteam_workspace`, `devteam_leaddata` and `devteam_devopsdata`, six containers. Each
container gets **only its own** key; the Git token only `devteam-devops`. The temporary env files are
deleted right after each start. The script refuses `UI_BIND` other than `127.0.0.1` without `UI_USER`/`UI_PASSWORD`.

```powershell
wslc list                                                              # 6 containers, (healthy) after ~30 s
```

Smoke test without a model (uses `tests/mock_llm.py`; publishing fails without a Git host, which is expected):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build -Mock
```

Send a small task in the UI: the feed shows architect → backend/frontend → QA (pytest) → DevOps,
the task ends as `done_no_push`.

Security check (the same as in `check.sh`):

```powershell
wslc exec -u runner devteam-devops sh -c "cat /proc/1/environ > /dev/null && echo LEAK || echo ok"   # ok
wslc exec -u runner devteam-backend curl -s -o /dev/null -w "%{http_code}" -X POST http://devops:8000/publish -d "{}"   # 403
```

`scripts/check.sh` itself calls `docker` and does not work with wslc.

### 11.5 Operations

```powershell
wslc logs -f devteam-lead                                   # logs (any devteam-<role>)
wslc stats                                                  # CPU / memory per container
.\scripts\wslc-up.ps1                                       # after editing .env: recreates the containers
.\scripts\wslc-up.ps1                                       # update: downloads the latest image (source: git pull; -Build)
.\scripts\wslc-down.ps1                                     # stop, task history and projects kept
.\scripts\wslc-down.ps1 -Volumes                            # stop and DELETE history and projects
wslc settings                                               # VM CPU / memory / storage location
```

Differences from Docker Compose:

- **No restart policy.** After a Windows restart run `wslc-up.ps1` again (the installer's desktop shortcut does that). Volumes survive.
  For autostart: Task Scheduler → «At log on» → `powershell -ExecutionPolicy Bypass -File <path>\scripts\wslc-up.ps1`.
- The test GitLab from `tests/docker-compose.test.yml` is not started by the script (needs ~4 GB RAM).
- Backup: `wslc run --rm -v devteam_leaddata:/d -v ${PWD}:/b alpine tar czf /b/leaddata.tgz -C /d .`
  (the same for `devteam_workspace`).

### 11.6 Free the disk space

```powershell
.\scripts\wslc-down.ps1 -Volumes
wslc rmi devteam-agent:latest
wslc image prune -a -f
```

Deleting images does not shrink the VM disk `%LOCALAPPDATA%\wslc\sessions\<session>\storage.vhdx`
(~3 GB after a build). To get the space back: `wslc system session terminate`, then delete that
`storage.vhdx`. wslc creates a new empty disk on the next start. **All images and volumes of that
session are lost.**

### 11.7 Troubleshooting (Windows)

| Symptom | Cause / fix |
|---|---|
| `wslc` not found | WSL older than 3.0.1: `wsl --update` |
| `running scripts is disabled on this system` | start with `powershell -ExecutionPolicy Bypass -File ...` |
| `bind: address already in use` / UI not reachable | the port is taken by another app: change `UI_PORT` |
| A container exits right after the start | `wslc logs devteam-<role>`; usually an empty `LLM_MODEL` or a wrong key |
| Build fails on `pip install` / `apt-get` with TLS errors | corporate proxy/CA: put the CA into `agent\certs\`, rebuild with `-Rebuild` |
| Agents are slow or get killed during tests | VM memory: `wslc settings` → raise memory, then `wslc system session terminate` |
| `.env` changes do not apply | run `wslc-up.ps1` again: it recreates the containers |

## 12. Stop, start, update, uninstall

Stopping keeps everything: task history, project folders, keys and tokens from ⚙ Setup. Only **uninstall
with data** deletes them. Repositories already published to GitHub/GitLab are never touched.

### Windows (installed with `install.ps1`)

| Action | How |
|---|---|
| Stop | double-click **AI DevTeam - stop** on the desktop, or `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\wslc-down.ps1"` |
| Start (also after a Windows restart) | double-click **AI DevTeam** on the desktop |
| Update to the latest version | run the installer again: `irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 \| iex` (data and settings are kept) |
| Status, logs | `wslc list` · `wslc logs -f devteam-lead` |
| Uninstall, keep the data | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1" -KeepData` |
| Uninstall completely | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1"` (asks before deleting the data; `-Yes` = no questions) |

`uninstall.ps1` removes the containers, the network, the image (~1.7 GB), both desktop shortcuts and the
`ai-devteam` folder, and — unless `-KeepData` — the volumes `devteam_workspace`, `devteam_leaddata`,
`devteam_devopsdata`. When no other WSL containers or images are left, it also deletes the disk of the WSL
container VM (`%LOCALAPPDATA%\wslc\sessions\…\storage.vhdx`), which gives the remaining disk space back.
WSL itself stays installed.

> **Normal vs administrator PowerShell.** WSL keeps the containers of a normal user and of an administrator
> apart (two «sessions» with their own data). Install and start AI DevTeam in a **normal** PowerShell — the
> desktop shortcuts run that way; the installer refuses to run as administrator. `wslc-down.ps1` and
> `uninstall.ps1` clean every session they can open; started as administrator they also see the normal one.

### Linux / macOS (installed with `install.sh`)

```bash
cd ~/ai-devteam
docker compose stop          # stop (data kept)
docker compose start         # start again
docker compose ps            # status
docker compose logs -f lead  # logs
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash   # update
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash -s -- --uninstall              # remove everything
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash -s -- --uninstall --keep-data  # keep the data volumes
```

The containers restart by themselves after a reboot (`restart: unless-stopped`) unless you stopped them.

### Installed from the source code

```bash
docker compose down                          # stop and remove the containers, data kept
docker compose down -v                       # … and delete the data volumes
docker compose down -v --rmi all             # … and the image
docker image prune -f                        # leftovers of local builds
```

Windows source checkout: `.\scripts\wslc-down.ps1` (stop), `.\scripts\wslc-down.ps1 -Volumes` (and the data),
`.\scripts\uninstall.ps1` (everything except the folder itself).
