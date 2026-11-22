# Contributing

Thanks for helping! Bug reports, docs and code are all welcome.

## Before you start
- Open an issue first for anything bigger than a small fix, so we agree on the approach.
- Never commit `.env`, keys, tokens, certificates or real internal hostnames.

## Development setup
```bash
git clone https://github.com/nikvladimirovich2/ai-devteam.git && cd ai-devteam
bash tests/smoke.sh            # builds the image and runs a task with the mock LLM (needs Docker)
```
`tests/mock_llm.py` is an OpenAI-compatible fake model that drives the real tools, so the whole
pipeline (plan → roles → QA → DevOps) and the agent isolation are tested without a GPU or API key.
On Windows see DEPLOY.md § 11 (`scripts/wslc-up.ps1 -Mock`).

For a real run from the source tree:
```bash
echo "INTERNAL_SECRET=$(openssl rand -hex 32)" > .env
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```
then open http://localhost:8080 and finish ⚙ Setup (or put the model and Git into `.env`).
`docker-compose.yml` alone uses the prebuilt image `ghcr.io/nikvladimirovich2/ai-devteam`
(published by `.github/workflows/image.yml` from `main` and from `v*` tags).

Unit tests of the setup helpers: `pip install httpx pytest && python -m pytest -q tests`.

## Dependencies
Direct dependencies are in `agent/requirements.in`; the image installs the pinned `agent/requirements.lock`.
After changing `requirements.in`:
```bash
cd agent
uv pip compile requirements.in --python-version 3.12 --python-platform x86_64-manylinux_2_28 --annotation-style line -o requirements.lock
```
Then run the smoke test.

## Pull requests
- One topic per PR; [Conventional Commits](https://www.conventionalcommits.org/) messages
  (`fix: …`, `feat: …`, `docs: …`).
- CI (`.github/workflows/ci.yml`) must be green.
- Update README / DEPLOY / MODELS when settings or behaviour change, and CHANGELOG.md.
- By contributing you agree that your contribution is licensed under the AGPL-3.0 (see LICENSE).

## Code of conduct
Be respectful and constructive. Harassment or personal attacks are not tolerated; maintainers may
remove such content and block its authors.
