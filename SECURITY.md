# Security policy

AI DevTeam lets language models **run shell commands and write code** on the host where it is deployed,
and gives one of them (DevOps) a Git token. Treat it like a CI runner that executes untrusted code.

## Running it safely
- Run it on a dedicated VM or host, not on a machine with other secrets.
- Keep the UI on `127.0.0.1` (default) or put it behind a reverse proxy with TLS and `UI_USER`/`UI_PASSWORD`.
  The start scripts refuse a public bind without a password.
- Give the Git token the minimum rights, preferably for a separate group / organization or account
  used only by the agents.
- Set a strong `INTERNAL_SECRET` (`openssl rand -hex 32`).
- Tasks, READMEs of downloaded packages and web pages can contain prompt injections. The isolation
  below limits what a hijacked agent can reach, it does not make running untrusted tasks safe.

## What the project does to isolate the agents
- Each container gets only its own model key; `GIT_TOKEN` exists only in `devops`.
- Commands of the agents run as a separate user (`runner`) with a clean environment: they cannot read the
  agent's environment or memory (`/proc/<pid>/environ`). The Git token is never on a command line.
- Internal requests between the containers are signed with per-role tokens derived from `INTERNAL_SECRET`;
  unsigned calls get `403`.
- No Docker socket in the containers; agents run without root.

Known limits: all tasks share the `workspace` volume, and outbound network access is not filtered.

## Reporting a vulnerability
Please report privately through **GitHub → Security → Report a vulnerability**
(https://github.com/nikvladimirovich2/ai-devteam/security/advisories/new). Do not open a public issue.
Include steps to reproduce and the version or commit. You will get an answer within 7 days.

Supported version: the latest release and `main`.
