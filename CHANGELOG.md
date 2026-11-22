# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/), versions: [SemVer](https://semver.org/).

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
