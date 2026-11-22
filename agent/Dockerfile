FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    CREWAI_DISABLE_TELEMETRY=true OTEL_SDK_DISABLED=true CREWAI_TRACING_ENABLED=false

# Tools agents use to build/test the generated products
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential nodejs npm sqlite3 jq procps sudo \
    && rm -rf /var/lib/apt/lists/*

# Exact versions (generated from requirements.in, see CONTRIBUTING.md), so every build is the same
COPY requirements.lock /tmp/requirements.lock
RUN pip install -r /tmp/requirements.lock

# Corporate CA certificates (optional): put PEM *.crt into agent/certs/ and rebuild.
# Added to the system store AND to certifi (litellm/httpx/requests use certifi by default).
COPY certs/ /usr/local/share/ca-certificates/corp/
RUN set -e; update-ca-certificates; \
    CERTIFI=$(python -c "import certifi; print(certifi.where())"); \
    for c in /usr/local/share/ca-certificates/corp/*.crt; do [ -f "$c" ] && cat "$c" >> "$CERTIFI" || true; done
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# Two users. "agent" runs the service and holds the API key / Git token in its environment.
# "runner" executes the commands the model writes (ShellTool -> sudo -u runner). A different uid
# cannot read /proc/<agent pid>/environ or the agent's memory, so a prompt injection that makes
# the model run `cat /proc/1/environ` gets nothing. They share the "agent" group and the setgid
# /workspace, so both can edit project files.
RUN useradd -m -u 1000 agent && useradd -m -u 1001 -g agent -s /bin/bash runner \
    && mkdir -p /workspace /data && chown agent:agent /workspace /data && chmod 2775 /workspace \
    && printf 'Defaults:agent !use_pty, !lecture, env_reset\nagent ALL=(runner) NOPASSWD: ALL\n' > /etc/sudoers.d/agent-runner \
    && chmod 0440 /etc/sudoers.d/agent-runner && visudo -cf /etc/sudoers.d/agent-runner \
    && git config --system --add safe.directory '*' && git config --system init.defaultBranch main
COPY --chown=agent:agent app/ /app/
RUN chmod -R go-w /app
WORKDIR /app
USER agent

EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s CMD curl -fs http://localhost:8000/health || exit 1
# ROLE=lead -> lead.py (UI + orchestration); everything else -> worker.py
CMD ["sh", "-c", "if [ \"$ROLE\" = lead ]; then exec python lead.py; else exec python worker.py; fi"]
