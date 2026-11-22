#!/usr/bin/env bash
# AI DevTeam installer for Linux and macOS (needs Docker). Run:
#   curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash
# It downloads the prebuilt image, starts the team and opens http://localhost:<port>.
# The AI model and GitHub/GitLab are then set up in the browser. Run it again to update.
# Options (environment): DEVTEAM_DIR (default ~/ai-devteam), DEVTEAM_REF (branch or tag), UI_PORT.
set -euo pipefail
REPO=nikvladimirovich2/ai-devteam
REF=${DEVTEAM_REF:-main}
DIR=${DEVTEAM_DIR:-$HOME/ai-devteam}
say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is not installed.
  Linux: https://docs.docker.com/engine/install/   macOS: https://www.docker.com/products/docker-desktop/
  Then run this command again."
docker info >/dev/null 2>&1 || die "Docker is installed but not running, or this user may not use it.
  Linux: sudo usermod -aG docker \$USER  (then log out and in).  macOS: start Docker Desktop."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing (Linux package: docker-compose-plugin)."

say "Installing AI DevTeam into $DIR"
mkdir -p "$DIR/certs"
cd "$DIR"
for f in docker-compose.yml .env.example; do
  curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/$f" -o "$f.new" && mv "$f.new" "$f"
done

if [ ! -f .env ]; then
  port=${UI_PORT:-8080}
  while (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do port=$((port + 1)); done   # first free port
  secret=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  umask 077
  printf '# AI DevTeam. The model and Git are set up in the browser; advanced options: .env.example\nINTERNAL_SECRET=%s\nUI_PORT=%s\nCERTS_DIR=./certs\n' \
    "$secret" "$port" > .env
fi
port=$(grep -E '^UI_PORT=' .env | tail -1 | cut -d= -f2)
url="http://localhost:${port:-8080}"

say "Downloading the image (first time ~1.7 GB) and starting the team"
docker compose pull
docker compose up -d --remove-orphans

for _ in $(seq 1 60); do curl -fs "$url/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fs "$url/health" >/dev/null 2>&1 || die "The team did not start. Logs: cd $DIR && docker compose logs lead"

say "Ready: $url"
echo "  Open it, then follow ⚙ Setup: the AI model key and a GitHub/GitLab token."
echo "  Company CA certificates (*.crt): put them into $DIR/certs and run: docker compose restart"
echo "  Stop: cd $DIR && docker compose down      Update: run this installer again"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then open "$url" || true; fi
