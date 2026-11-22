# Roadmap

Goal: a person who is not a developer installs AI DevTeam with one command, enters an AI key and a
GitHub/GitLab token in the browser, gives a task and gets a published, tested release.
Each item has a GitHub issue; ✅ = done.

## Stage 1 — start in one step (0.2)
- ✅ Prebuilt image on ghcr.io, certificates mounted at runtime instead of baked in.
- ✅ One-command installers: Windows (`install.ps1`, WSL containers) and Linux/macOS (`install.sh`).
- ✅ Setup wizard in the browser: provider presets, one key for the team, model check incl. tool calling,
  GitHub/GitLab token with a pre-filled link and an access check, interface password.
- ✅ Human-readable errors (English/Russian) with what to do next.

## Stage 2 — a task and a release for a non-developer (0.3)
- The Team Lead asks clarifying questions and shows the plan for approval before the team starts.
- Task templates: website, landing page, Telegram bot, REST API, internal tool.
- A real release: tag `v1.0.0`, GitHub/GitLab Release, README «how to run it» for a non-developer;
  GitHub/GitLab Pages for websites, so the report has a live link.
- Cost budget per task (tokens / money), an estimate before the start, stop with a report when exceeded.
- Notification when a task is done (Telegram, e-mail).
- The whole interface in English and Russian.

- Android projects: an optional image with JDK, Android SDK and Gradle, so QA builds the APK and runs
  JVM/Robolectric tests inside the team (today Android code is checked only by CI).

## Stage 3 — catch up with the established agents (0.4+)
- Work on an existing repository: import, issue → merge request with review.
- A separate sandbox container per task, several tasks in parallel.
- A benchmark of 10–20 reference tasks with results per model (success, cost, time) in the README.
- Documentation search / MCP tools for the agents; a browser for checking web UIs.
