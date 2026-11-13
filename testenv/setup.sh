#!/usr/bin/env sh
# Start the local test server. Run inside WSL from anywhere:  sh testenv/setup.sh
# Generates test-only secrets on first run (gitignored): .env (DB password), .keys/id_ed25519 (SSH key).
set -eu
cd "$(dirname "$0")"
if [ "${1:-}" = "--reset" ]; then  # wipe all test data (containers + volumes) and start fresh
  docker compose down -v --remove-orphans
  rm -f .seed.json
fi
[ -f .env ] || printf 'POSTGRES_PASSWORD=%s\n' "$(head -c 18 /dev/urandom | base64 | tr -d '/+=')" > .env
mkdir -p .keys
[ -f .keys/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -C mm-purge-test -f .keys/id_ed25519
chmod 600 .keys/id_ed25519
docker compose up -d --build
printf 'Waiting for Mattermost'
for _ in $(seq 1 90); do
  if docker exec mattermost mmctl system status >/dev/null 2>&1; then echo ' ready.'; exit 0; fi
  printf '.'; sleep 2
done
echo ' timed out'; docker compose logs --tail 40 mattermost; exit 1
