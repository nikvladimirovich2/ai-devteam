# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/), versions: [SemVer](https://semver.org/).

## [0.2.4] - 2026-11-23

### Added
- «🎲 Example task» button next to Send: puts a random tiny project (tip calculator, password generator, to-do list,
  unit converter, word counter, Pomodoro timer) into the text box — a quick way to try the team and check that the
  model and the git service work. Nothing is sent until Send.

## [0.2.3] - 2026-11-23

### Fixed
- A model server on the same computer (Ollama, LM Studio, vLLM): `localhost` / `127.0.0.1` in the model address now
  means the computer, not the container — it is replaced by `host.docker.internal` (Docker) or the IP of the
  `vEthernet (WSL)` adapter (`HOST_GATEWAY`, set by `wslc-up.ps1`). The wizard explains `OLLAMA_HOST=0.0.0.0` and the
  firewall when such a server does not answer. Docs: DEPLOY.md § 5, all languages.

## [0.2.2] - 2026-11-22

### Added
- Stop / start / update / uninstall guide (DEPLOY.md § 12, all languages); `scripts/uninstall.ps1` for Windows
  (keeps or deletes the data, gives the WSL container disk back), an «AI DevTeam - stop» desktop shortcut;
  `install.sh --uninstall [--keep-data]` for Linux/macOS.
- CI tests `install.sh` end to end: install, stop/start, uninstall.

### Fixed
- Windows: WSL keeps separate containers for a normal and an administrator PowerShell. The installer now refuses to
  run as administrator (the desktop shortcuts would not see that team), `wslc-up.ps1` warns when a copy runs in the
  other session, `wslc-down.ps1` / `uninstall.ps1` clean every session they can open.
- `uninstall.ps1` also removes untagged images left by updates and deletes the WSL container disk reliably.
- The installer warns when there is less than 5 GB free and stops below 3 GB.

## [0.2.1] - 2026-11-22

### Added
- Setup wizard: **models per role** (optional): a role gets another model of the team's provider and optionally its own
  key, with the same check; tips which model fits which role (MODELS.md). Changing the provider resets them.
- The final report shows the model of each role next to its tokens.

## [0.2.0] - 2026-11-22

Start in one step: install with one command, set up everything in the browser.

### Added
- One-command installers: `install.ps1` (Windows, WSL containers, desktop shortcut) and `install.sh` (Linux/macOS, Docker).
- Prebuilt image `ghcr.io/nikvladimirovich2/ai-devteam` (workflow `image.yml`); `docker-compose.build.yml` builds from source.
- Setup wizard in the web interface (⚙ Setup, English/Russian): model provider presets (OpenAI, Anthropic, Gemini,
  OpenRouter, DeepSeek, Ollama, vLLM, other), model list, a check that the model calls tools; GitHub/GitLab with a
  link to the token page with the right scopes and an access check; interface password. `.env` is optional now.
- One API key for the whole team (`LLM_API_KEY`), personal keys per role still possible.
- Company CA certificates at runtime (`CERTS_DIR` mounted at `/certs`), no rebuild needed.
- Human-readable error messages (en/ru) in the wizard and in the task feed.
- Deployment guide in Russian, Serbian and Chinese; support the author (GitHub Sponsors, USDT TRC20, USDC Solana).

### Security
- The Git token set in the wizard is checked and stored only by devops (`devopsdata` volume, 0600, not readable by the
  agents' shell user); the lead never stores it. `/config/git` is a signed internal endpoint.

## [0.1.0] - 2026-11-22
First public release.

### Added
- Six role agents (Lead, Architect, Backend, Frontend, QA, DevOps) on CrewAI, each in its own container
  with its own model key; web UI with a live feed, revisions, final report with tokens per role.
- Publishing to GitLab or GitHub, waiting for CI and fixing it automatically (`MAX_CI_ROUNDS`).
- Self-hosted models (vLLM, Ollama, OpenAI-compatible, GigaChat), private CA certificates.
- Windows: WSL containers (`scripts/wslc-up.ps1`), DEPLOY.md § 11.
- End-to-end smoke test with a mock LLM (`tests/smoke.sh`) and GitHub Actions CI.

### Security
- Agent commands run as a separate `runner` user with a clean environment; the Git token is no longer
  passed on the `git push` command line.
- Internal requests between the containers are signed (`INTERNAL_SECRET`), unsigned calls get 403.
