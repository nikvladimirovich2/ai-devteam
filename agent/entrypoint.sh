#!/bin/sh
# Start of every AI DevTeam container.
# 1. Certificates mounted at /certs (*.crt, PEM) are trusted by Python (httpx/requests/LiteLLM),
#    git, node and the commands of the agents: a private CA without rebuilding the image.
# 2. ROLE=lead starts the UI + orchestration, any other role a worker agent.
# 3. A command given to the container (e.g. the mock LLM in tests) runs instead.
set -e
if ls /certs/*.crt >/dev/null 2>&1; then
    bundle=/tmp/devteam-ca.crt
    cat /etc/ssl/certs/ca-certificates.crt /certs/*.crt > "$bundle"
    chmod 644 "$bundle"
    certifi=$(python -c "import certifi; print(certifi.where())")
    if [ -f "$certifi.orig" ] && [ -w "$certifi" ]; then cat "$certifi.orig" /certs/*.crt > "$certifi"; fi
    export REQUESTS_CA_BUNDLE="$bundle" SSL_CERT_FILE="$bundle" NODE_EXTRA_CA_CERTS="$bundle" GIT_SSL_CAINFO="$bundle"
    echo "devteam: trusting $(ls /certs/*.crt | wc -l) certificate file(s) from /certs"
fi
if [ "$#" -gt 0 ]; then exec "$@"; fi
if [ "$ROLE" = lead ]; then exec python lead.py; fi
exec python worker.py
