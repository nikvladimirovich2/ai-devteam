FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    CREWAI_DISABLE_TELEMETRY=true OTEL_SDK_DISABLED=true CREWAI_TRACING_ENABLED=false

# Tools agents use to build/test the generated products
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential nodejs npm sqlite3 jq procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Corporate CA certificates (optional): put PEM *.crt into agent/certs/ and rebuild.
# Added to the system store AND to certifi (litellm/httpx/requests use certifi by default).
COPY certs/ /usr/local/share/ca-certificates/corp/
RUN set -e; update-ca-certificates; \
    CERTIFI=$(python -c "import certifi; print(certifi.where())"); \
    for c in /usr/local/share/ca-certificates/corp/*.crt; do [ -f "$c" ] && cat "$c" >> "$CERTIFI" || true; done
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

RUN useradd -m -u 1000 agent && mkdir -p /workspace /data && chown agent:agent /workspace /data
COPY --chown=agent:agent app/ /app/
WORKDIR /app
USER agent
RUN git config --global --add safe.directory '*' && git config --global init.defaultBranch main

EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s CMD curl -fs http://localhost:8000/health || exit 1
# ROLE=lead -> lead.py (UI + orchestration); everything else -> worker.py
CMD ["sh", "-c", "if [ \"$ROLE\" = lead ]; then exec python lead.py; else exec python worker.py; fi"]
