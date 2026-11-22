[English](DEPLOY.md) · [Русский](DEPLOY.ru.md) · [Srpski](DEPLOY.sr.md) · **中文**

# 部署指南

在一台全新主机上分步安装 AI DevTeam，约需 15 分钟。

**快捷方式**是使用 [README](README.md#quick-start) 中的一键安装程序：它会下载
预构建镜像（`ghcr.io/nikvladimirovich2/ai-devteam`），启动团队并打开浏览器，
由设置向导（⚙ Setup）询问模型密钥和 GitHub/GitLab 令牌。本指南面向服务器、
封闭网络，以及所有希望改用 `.env` 进行配置的场景。

**使用 Windows？** 可通过 WSL 容器 CLI（`wslc`）运行，无需 Docker Desktop：参见
[11. Windows: WSL 容器 (wslc)](#11-windows-wsl-容器-wslc)。

## 1. 系统要求

| | 最低 | 推荐 |
|---|---|---|
| 操作系统 | Linux x86_64（Debian 12/13、Ubuntu 22.04+、Astra、RHEL 9） | — |
| CPU / 内存 | 4 vCPU / 8 GB | 8 vCPU / 16 GB（智能体会运行 pip/npm/pytest） |
| 磁盘 | 20 GB | 50 GB（镜像约 1.7 GB + 项目 + pip/npm 缓存） |
| 软件 | Docker Engine 24+、Docker Compose v2、git | — |
| 网络 | 可通过 HTTPS 访问 LLM API 和 GitLab/GitHub；智能体需访问 pypi.org / registry.npmjs.org | — |

LLM 本身**不**属于本技术栈——智能体调用的是已有的 OpenAI 兼容端点
（vLLM、Ollama、LiteLLM 代理、GigaChat 等）。本主机无需 GPU。

## 2. 安装 Docker

Debian / Ubuntu（发行版软件包）：

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose docker-buildx git curl jq
sudo systemctl enable --now docker
docker version && docker compose version
```

（也可使用 download.docker.com 提供的 Docker CE——任何带 `compose` 插件的 Docker 24+ 均可。）
非 root 用户：执行 `sudo usermod -aG docker $USER` 后重新登录。

## 3. 获取代码

```bash
sudo mkdir -p /opt/devteam && sudo chown $USER: /opt/devteam
git clone https://github.com/<you>/ai-devteam.git /opt/devteam
cd /opt/devteam
```

## 4. 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env
echo "INTERNAL_SECRET=$(openssl rand -hex 32)" >> .env
$EDITOR .env
```

只有 `INTERNAL_SECRET` 是必填项。模型、密钥和 Git **既可以**在这里设置，**也可以**在
浏览器中设置（⚙ Setup → *Check and save*，即“检查并保存”）。`.env` 中的值优先；向导会将其显示为 «set in .env»（已在 .env 中设置）。
向导的设置保存在卷中：模型和密钥保存在 `leaddata`（lead），Git 令牌保存在
`devopsdata`（仅 devops 可用，权限 0600，智能体的 shell 用户无法读取）。

在 `.env` 中配置：

| 变量 | 示例 | 说明 |
|---|---|---|
| `LLM_MODEL` | `hosted_vllm/Qwen/Qwen3-32B-AWQ` | LiteLLM 格式 `<provider>/<model id from /v1/models>` |
| `LLM_BASE_URL` | `https://llm.corp.local/v1` | 用于 vLLM/OpenAI 兼容 API——需包含 `/v1` |
| `LLM_API_KEY` | | 整个团队共用一个密钥…… |
| `LEAD_API_KEY` … `DEVOPS_API_KEY` | | ……或者 6 个独立密钥，每个角色一个（优先于 `LLM_API_KEY`） |
| `GIT_URL` | `https://gitlab.corp.local` 或 `https://github.com` | 根据 URL 自动识别提供方 |
| `GIT_TOKEN` | | 参见下文 «Git 令牌» |
| `GIT_NAMESPACE` | `ai-team` | GitLab 群组（必填）/ GitHub 组织（留空 = 个人账号） |
| `INTERNAL_SECRET` | `openssl rand -hex 32` | ≥ 32 个字符；用于对容器之间的请求签名（见下文） |

推荐配置：

| 变量 | 用途 |
|---|---|
| `UI_USER`、`UI_PASSWORD` | Web UI 的 Basic Auth |
| `ARCHITECT_MODEL`、`BACKEND_MODEL`、… | 每个角色使用不同的模型——参见 [MODELS.md](MODELS.md) |

> **值中的 `$`。** Compose 会展开 `.env` 中的 `$VAR`。包含 `$` 的密钥必须用单引号括起来：
> `QA_API_KEY='$abc#def'`，否则其中一部分会被悄无声息地丢弃。

### 智能体隔离

智能体会执行模型编写的 shell 命令，而提示词注入（某个软件包的 README、某个网页、
任务文本）可能让模型执行任意命令。有两层机制让密钥和 Git 令牌远离
这些命令：

- **独立的 shell 用户。** 服务以 `agent`（uid 1000）身份运行，持有该角色的 API 密钥（在
  `devops` 中还持有 `GIT_TOKEN`）。`run_shell` 发出的每条命令都以 `runner`（uid 1001）身份在干净的
  环境中运行，因此无法读取智能体的 `/proc/<pid>/environ` 和内存。两个用户
  属于同一个组，因此都能编辑项目文件。Git 令牌通过环境变量传递给 `git push`，
  绝不出现在命令行中（`/proc/<pid>/cmdline` 对所有用户可读）。
- **签名的内部请求。** lead 和各 worker 使用由 `INTERNAL_SECRET` 派生的、按角色区分的
  HMAC 令牌对每个内部请求签名。没有该令牌时，`POST devops:8000/publish` 和
  `POST lead:8000/api/internal/events` 会返回 `403`，因此命令无法借用 DevOps
  令牌发布，也无法伪造 `✅ pushed` 事件。事件的角色取自令牌，而不是
  请求体。每个 worker 只持有自己的令牌；密钥本身只保存在 lead 中。

`scripts/check.sh` 会验证这两点（`shell user cannot read the agent environment`、`internal
endpoints reject unsigned calls`）。尽管如此：仅授予令牌所需的权限，最好使用
单独的组织 / 账号。

### Git 令牌

**GitLab** — Personal 或 Group Access Token，作用域 `api` + `write_repository`
（该用户必须有权在 `GIT_NAMESPACE` 中创建项目）。

**GitHub** — fine-grained PAT：Settings → Developer settings → Fine-grained tokens：
- Resource owner = `GIT_NAMESPACE` 中的组织（或你自己），**Repository access: All repositories**
  （智能体会创建新仓库——«Only select» 无法覆盖它们）；
- Repository permissions：**Administration RW、Contents RW、Workflows RW、Actions R**（Metadata R 会自动授予）。
- 在组织中：必须允许使用 fine-grained 令牌（如开启了审批，还需获得批准），并完成 SAML SSO 授权。

也可使用 Classic PAT：作用域 `repo` + `workflow`。

### 自签名 / 企业证书

如果 LLM 或 GitLab 通过使用私有 CA 的 HTTPS 提供服务：

```bash
cp /path/to/ca-or-server.crt agent/certs/       # PEM, -----BEGIN CERTIFICATE-----
docker compose restart
```

该文件夹（`CERTS_DIR`，默认 `./agent/certs`；安装程序使用 `./certs`）会挂载到每个容器的 `/certs`；
启动时，证书会被加入 Python（certifi、
`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`）、git（`GIT_SSL_CAINFO`）和 node（`NODE_EXTRA_CA_CERTS`）的受信任证书包中。无需重新构建。
自行构建镜像时，`agent/certs/` 中的证书也会被打包进镜像。
证书的 SAN 必须包含 `LLM_BASE_URL` 中的主机（如果是 IP——使用 `IP Address:`）。
`agent/certs/*.crt` 已被 git 忽略：需要在每台主机上分别复制。

## 5. LLM 服务器要求

智能体通过**工具调用**（`write_file`、`run_shell`、`publish_repository`）工作。
模型端点必须返回原生的 `tool_calls`：

- **vLLM**：启动时加上 `--enable-auto-tool-choice --tool-call-parser <parser>`
  （Qwen2.5/Qwen3 用 `hermes`，Llama 3.x 用 `llama3_json`，Mistral 用 `mistral`，……）。
- **Ollama**：使用具备 `tools` 能力的模型。
- 上下文：建议 ≥ 32k 令牌（`--max-model-len 32768`）；上下文窗口会自动从
  vLLM `/v1/models`（`max_model_len`）读取，或通过 `LLM_CONTEXT_WINDOW` 设置。

快速手动检查：

```bash
curl -s --cacert agent/certs/llm.crt "$LLM_BASE_URL/models" -H "Authorization: Bearer <key>" | jq '.data[].id'
```

## 6. 启动

```bash
docker compose up -d         # pulls the prebuilt image
docker compose ps            # 6 containers, all (healthy) after ~30 s
./scripts/check.sh           # end-to-end pre-flight check
```

无法访问 ghcr.io（封闭网络）？可以改为在主机上构建镜像：
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`，
或者从文件加载：在可以访问互联网的机器上执行 `docker save ghcr.io/nikvladimirovich2/ai-devteam:latest | gzip > devteam.tgz`，
然后执行 `docker load < devteam.tgz`。

`scripts/check.sh` 会验证：`.env` 是否已填写以及 `$` 引号是否正确、证书、容器健康状态、
**每个角色是否都能用自己的密钥访问 LLM**、原生工具调用是否可用、Git 令牌/命名空间、
Git 令牌是否只存在于 `devops` 中、UI 健康状态。

## 7. 打开 UI

UI 仅监听 `127.0.0.1:${UI_PORT:-8080}`。在你的工作站上：

```bash
ssh -L 8080:localhost:8080 user@<host>                      # direct
ssh -J jump-user@jump-host:port -L 8080:localhost:8080 user@<host>   # via a jump host
# → http://localhost:8080
```

如需在网络上公开：设置 `UI_BIND=0.0.0.0` **并且**设置 `UI_USER`/`UI_PASSWORD`，最好置于
带 TLS 的反向代理（nginx/traefik）之后。

## 8. 第一个任务

先发送一个小任务（参见 README 中的 «Example task»）。在动态信息流中应能看到：
`🔎 architect: +N files` → `🔎 backend/frontend: …` → QA 结论 → `✅ pushed …` → `⏳ Waiting for CI…` →
`✅ CI passed` → 最终的纯文本报告，其中包含各角色的令牌用量。

## 9. 运维

```bash
docker compose logs -f lead devops         # logs
docker compose restart                     # after editing .env (or: up -d to recreate)
docker compose pull && docker compose up -d   # update (source builds: git pull + the build file)
docker compose down                        # stop, data kept
docker compose down -v                     # stop and DELETE task history and workspaces
```

备份——两个卷：

```bash
docker run --rm -v devteam_leaddata:/d -v $PWD:/b alpine tar czf /b/leaddata.tgz -C /d .
docker run --rm -v devteam_workspace:/d -v $PWD:/b alpine tar czf /b/workspace.tgz -C /d .
```

## 10. 故障排除

| 现象 | 原因 / 解决方法 |
|---|---|
| 执行 `docker compose` 时出现 `The "X" variable is not set` | `.env` 中的某个值包含 `$`——用单引号括起来 |
| `CERTIFICATE_VERIFY_FAILED` | 将 CA/服务器证书放入证书文件夹（`CERTS_DIR`），执行 `docker compose restart` |
| 向导提示 «The key was rejected» / «does not call tools» | 提示信息会说明该怎么做；技术细节显示在其下方 |
| LLM 返回 `401` | `<ROLE>_API_KEY` 错误；`./scripts/check.sh` 会显示是哪个角色 |
| 智能体«报告»已完成工作，但出现 `🔎 … did not create files` | 模型未返回工具调用 → 参见上文的 vLLM 参数，`LLM_FUNCTION_CALLING=auto` |
| `Action 'write_file' don't exist` / 循环 | 该角色使用的模型工具调用能力较弱 → 换用更强的模型（MODELS.md） |
| `GitLab namespace … not found` | `GIT_NAMESPACE` 错误，或令牌所属用户无权访问 |
| 创建仓库时 GitHub 返回 `403` | fine-grained 令牌未在组织中获得批准 / 没有 Administration RW / SSO 未授权 |
| `⚠ CI: no_pipeline` | 未推送 CI 配置、项目中禁用了 CI，或没有 runner |
| 达到 `MAX_CI_ROUNDS` 后 CI 仍为红色 | 打开任务 → **🔧 CI** 以运行更多轮修复，或手动修复 |
| Lead 容器 `unhealthy`，UI 卡住 | `docker compose logs lead`；`docker compose restart lead`（正在运行的任务会变为 `interrupted`） |
| `INTERNAL_SECRET is missing or shorter than 32 characters` | 将 `INTERNAL_SECRET=$(openssl rand -hex 32)` 添加到 `.env`，执行 `docker compose up -d` |
| 日志中出现 `403 internal endpoint` | 容器启动时使用了不同的 `INTERNAL_SECRET` 值（旧容器）：使用 `docker compose up -d --force-recreate` 重新创建所有容器 |

## 11. Windows: WSL 容器 (wslc)

`wslc.exe` 是 WSL 内置的容器 CLI。它在轻量级 Linux 虚拟机中运行同一个镜像，
无需 Docker Desktop。已在 Windows 10 22H2（build 19044）+ WSL 3.0.3 上测试。

### 11.1 系统要求

| | 最低 | 说明 |
|---|---|---|
| Windows | Windows 10 2004+（build 19041）或 Windows 11，x64 | |
| WSL | **3.0.1 或更高版本**（`wsl --version`） | `wslc` 随 WSL 一起提供，无需另外安装 |
| 虚拟化 | BIOS 中启用 VT-x/AMD-V + «Virtual Machine Platform»（虚拟机平台）功能 | 在虚拟机内运行时：需要嵌套虚拟化 |
| 内存 | 主机 8 GB | 实测：6 个空闲智能体约占 2 GB。wslc 虚拟机默认分配 4 GB（`wslc settings`）；如需并行运行 pip/npm/pytest，请提高到 6 GB |
| 磁盘 | `%LOCALAPPDATA%\wslc` 所在驱动器有 10 GB 可用空间 | 镜像 1.66 GB，构建后虚拟机磁盘 `storage.vhdx` 会增长到约 3.3 GB，另加项目和缓存 |
| 软件 | PowerShell 5.1+（系统自带）、git | **不**需要 `docker` / `docker compose` |
| 网络 | LLM 端点、GitLab/GitHub、pypi.org、registry.npmjs.org | 虚拟机使用 Windows 网络，包括 VPN |

LLM 不属于本技术栈：只要 `LLM_BASE_URL` 指向模型服务器，没有 GPU 的笔记本电脑也能正常运行。

### 11.2 安装 / 更新 WSL

仅当从未安装过 WSL 时，在**以管理员身份运行**的 PowerShell 中执行：

```powershell
wsl --install --no-distribution     # enables «Virtual Machine Platform»; restart Windows afterwards
```

然后（以普通用户身份）：

```powershell
wsl --update
wsl --version                       # WSL version: 3.0.1 or newer
wslc version
wslc run --rm hello-world           # smoke test of the container runtime
```

### 11.3 安装（一条命令）

在 PowerShell 中执行（WSL 安装完成后无需管理员权限）：

```powershell
irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 | iex
```

安装程序会：
- 如果缺少 `wslc`，执行 `wsl --update`（如果根本未安装 WSL，会提示该怎么做）；
- 将启动/停止脚本放入 `%USERPROFILE%\ai-devteam`，并为 `.env` 选择一个空闲端口（8080、8081、……）；
- 下载预构建镜像（约 1.7 GB）并启动六个容器；
- 在浏览器中打开设置向导，并创建 **AI DevTeam** 桌面快捷方式（重启后用于启动）。

企业证书：放入 `%USERPROFILE%\ai-devteam\certs\*.crt`，然后通过快捷方式重新启动。

### 11.4 从源代码安装（开发者）

```powershell
git clone https://github.com/nikvladimirovich2/ai-devteam.git
cd ai-devteam
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build   # builds the image here (~8 min)
```

`.env` 在 Windows 上的特殊之处（可选，参见 [4. 配置 `.env`](#4-配置-env)）：

- `INTERNAL_SECRET` 可以留空：启动脚本会生成它并写入 `.env`。
- 脚本按字面读取各个值，因此密钥中的 `$` 无需加引号（加引号也可以）。
- 开发机上 8080 端口经常被占用：可设置例如 `UI_PORT=8099`。
  `netstat -ano | findstr :8099` 可查看该端口是否空闲。
- 企业证书放入 `agent\certs\*.crt`（或 `CERTS_DIR`）。
- 不要提交 `.env`。它已在 `.gitignore` 中。如需仅限当前用户访问：`icacls .env /inheritance:r /grant:r "$($env:USERNAME):F"`。
- 启用 `core.autocrlf=true`（Git for Windows 的默认设置）时，Git 会以 CRLF 检出文件。
  这一点已经过测试：镜像可以正常构建和运行。脚本会自行以 LF 写入各角色的 env 文件。

wslc 目前还没有 `compose`，因此 `scripts\wslc-up.ps1` 完成 `docker-compose.yml` 的工作：一个网络
`devteam`，卷 `devteam_workspace`、`devteam_leaddata` 和 `devteam_devopsdata`，六个容器。每个
容器**只**获得自己的密钥；Git 令牌只提供给 `devteam-devops`。临时 env 文件
在每次启动后立即删除。如果 `UI_BIND` 不是 `127.0.0.1` 且未设置 `UI_USER`/`UI_PASSWORD`，脚本会拒绝启动。

```powershell
wslc list                                                              # 6 containers, (healthy) after ~30 s
```

不使用模型的冒烟测试（使用 `tests/mock_llm.py`；由于没有 Git 主机，发布会失败，这是预期行为）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build -Mock
```

在 UI 中发送一个小任务：信息流中会依次显示 architect → backend/frontend → QA（pytest）→ DevOps，
任务以 `done_no_push` 结束。

安全检查（与 `check.sh` 中的相同）：

```powershell
wslc exec -u runner devteam-devops sh -c "cat /proc/1/environ > /dev/null && echo LEAK || echo ok"   # ok
wslc exec -u runner devteam-backend curl -s -o /dev/null -w "%{http_code}" -X POST http://devops:8000/publish -d "{}"   # 403
```

`scripts/check.sh` 本身调用的是 `docker`，无法与 wslc 配合使用。

### 11.5 运维

```powershell
wslc logs -f devteam-lead                                   # logs (any devteam-<role>)
wslc stats                                                  # CPU / memory per container
.\scripts\wslc-up.ps1                                       # after editing .env: recreates the containers
.\scripts\wslc-up.ps1                                       # update: downloads the latest image (source: git pull; -Build)
.\scripts\wslc-down.ps1                                     # stop, task history and projects kept
.\scripts\wslc-down.ps1 -Volumes                            # stop and DELETE history and projects
wslc settings                                               # VM CPU / memory / storage location
```

与 Docker Compose 的区别：

- **没有重启策略。** Windows 重启后需要再次运行 `wslc-up.ps1`（安装程序创建的桌面快捷方式就是做这件事）。卷会保留。
  如需开机自启：任务计划程序 → «At log on»（登录时）→ `powershell -ExecutionPolicy Bypass -File <path>\scripts\wslc-up.ps1`。
- 脚本不会启动 `tests/docker-compose.test.yml` 中的测试 GitLab（需要约 4 GB 内存）。
- 备份：`wslc run --rm -v devteam_leaddata:/d -v ${PWD}:/b alpine tar czf /b/leaddata.tgz -C /d .`
  （`devteam_workspace` 同理）。

### 11.6 释放磁盘空间

```powershell
.\scripts\wslc-down.ps1 -Volumes
wslc rmi devteam-agent:latest
wslc image prune -a -f
```

删除镜像并不会缩小虚拟机磁盘 `%LOCALAPPDATA%\wslc\sessions\<session>\storage.vhdx`
（构建后约 3 GB）。要回收空间：执行 `wslc system session terminate`，然后删除该
`storage.vhdx`。下次启动时 wslc 会创建一个新的空磁盘。**该会话的所有镜像和卷
都会丢失。**

### 11.7 故障排除（Windows）

| 现象 | 原因 / 解决方法 |
|---|---|
| 找不到 `wslc` | WSL 版本低于 3.0.1：执行 `wsl --update` |
| `running scripts is disabled on this system` | 使用 `powershell -ExecutionPolicy Bypass -File ...` 启动 |
| `bind: address already in use` / 无法访问 UI | 端口被其他应用占用：修改 `UI_PORT` |
| 容器启动后立即退出 | `wslc logs devteam-<role>`；通常是 `LLM_MODEL` 为空或密钥错误 |
| 构建在 `pip install` / `apt-get` 时因 TLS 错误失败 | 企业代理/CA：将 CA 放入 `agent\certs\`，使用 `-Rebuild` 重新构建 |
| 智能体运行缓慢或在测试期间被终止 | 虚拟机内存：`wslc settings` → 增加内存，然后执行 `wslc system session terminate` |
| `.env` 的修改未生效 | 再次运行 `wslc-up.ps1`：它会重新创建容器 |

## 12. 停止、启动、更新、卸载

停止不会删除任何内容：任务历史、项目文件夹以及在 `⚙ Setup`（设置向导）中保存的密钥和令牌都会保留。
只有**连同数据一起卸载**才会删除它们。已经发布到 GitHub/GitLab 的仓库永远不会被改动。

### Windows（通过 `install.ps1` 安装）

| 操作 | 方法 |
|---|---|
| 停止 | 双击桌面上的 **AI DevTeam - stop** 快捷方式，或运行 `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\wslc-down.ps1"` |
| 启动（包括 Windows 重启之后） | 双击桌面上的 **AI DevTeam** 快捷方式 |
| 更新到最新版本 | 再次运行安装程序：`irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 \| iex`（数据和设置会保留） |
| 状态、日志 | `wslc list` · `wslc logs -f devteam-lead` |
| 卸载但保留数据 | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1" -KeepData` |
| 完全卸载 | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1"`（删除数据前会询问确认；`-Yes` 表示不询问） |

`uninstall.ps1` 会删除容器、网络、镜像（约 1.7 GB）、桌面上的两个快捷方式以及 `ai-devteam` 文件夹；
如果没有指定 `-KeepData`，还会删除卷 `devteam_workspace`、`devteam_leaddata`、`devteam_devopsdata`。
如果系统中已没有其他 WSL 容器或镜像，还会删除 WSL 容器虚拟机的磁盘
（`%LOCALAPPDATA%\wslc\sessions\…\storage.vhdx`），从而释放剩余的磁盘空间。WSL 本身仍保持安装。

> **普通 PowerShell 与管理员 PowerShell。** WSL 将普通用户和管理员的容器分开保存（两个拥有各自数据的“会话”）。
> 请在**普通** PowerShell 中安装和启动 AI DevTeam——桌面快捷方式也是这样运行的；安装程序拒绝以管理员身份运行。
> `wslc-down.ps1` 和 `uninstall.ps1` 会清理它们能访问的所有会话；以管理员身份运行时也能看到普通会话。

### Linux / macOS（通过 `install.sh` 安装）

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

重启后容器会自动启动（`restart: unless-stopped`），除非您之前手动停止了它们。

### 从源代码安装

```bash
docker compose down                          # stop and remove the containers, data kept
docker compose down -v                       # … and delete the data volumes
docker compose down -v --rmi all             # … and the image
docker image prune -f                        # leftovers of local builds
```

Windows 上的源代码检出：`.\scripts\wslc-down.ps1`（停止），`.\scripts\wslc-down.ps1 -Volumes`（连同数据），
`.\scripts\uninstall.ps1`（删除除文件夹本身以外的全部内容）。
