#!/usr/bin/env bash
#
# Deploy gym-api naar cloudwijk-server (163.172.152.203)
#
# Wat doet dit script:
#   1. Tar de relevante files (backend/, static/, index.html, manifest.json, sw.js)
#   2. scp naar /tmp/gym-src.tar.gz op de server
#   3. ssh: extract, docker build vanaf project-root met -f backend/Dockerfile
#   4. Stop+rename oude container, start nieuwe met --env-file /opt/gym/.env
#   5. Verify routes + health, cleanup
#
# Vereisten op server:
#   - /opt/gym/.env compleet met 8 vars (APP_ENV, DATABASE_URL, SECRET_KEY,
#     CORS_ORIGINS, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID_JOOST,
#     REMINDER_SECRET)
#   - Docker network 'cloudwijk_default' bestaat
#
# Gebruik:
#   ./deploy.sh                 # full rebuild + swap
#   ./deploy.sh --no-build      # skip image rebuild (snel, alleen container restart)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER="root@163.172.152.203"
SSH_KEY="${HOME}/.ssh/id_ed25519"
TAR_PATH="/tmp/gym-src.tar.gz"
REMOTE_TMP="/tmp/gym-build"
IMAGE_TAG="gym-api:latest"

cd "$SCRIPT_DIR"

if [[ "${1:-}" == "--no-build" ]]; then
  echo "==> SKIP build — alleen container restart op server"
  ssh -i "$SSH_KEY" "$SERVER" 'docker restart gym-api && sleep 4 && docker ps --filter name=^gym-api$ --format "{{.Names}}: {{.Status}}"'
  exit 0
fi

echo "==> 0. sync root-level index.html/sw.js/manifest.json → static/"
echo "    (static/ is wat Dockerfile copyt; root is source-of-truth voor edits)"
cp index.html static/index.html
cp sw.js static/sw.js
cp manifest.json static/manifest.json

echo ""
echo "==> 1. tar project (excl venv/cache/db/git)"
tar czf "$TAR_PATH" \
  --exclude='backend/__pycache__' \
  --exclude='backend/app/__pycache__' \
  --exclude='backend/app/routes/__pycache__' \
  --exclude='backend/.venv' \
  --exclude='backend/*.db' \
  --exclude='backend/Dockerfile.dev' \
  backend/ static/ index.html manifest.json sw.js
echo "    $(ls -lh "$TAR_PATH" | awk '{print $5, $9}')"

echo ""
echo "==> 2. scp naar server"
scp -q -i "$SSH_KEY" "$TAR_PATH" "$SERVER:$TAR_PATH"

echo ""
echo "==> 3. server-side build + container swap"
ssh -i "$SSH_KEY" "$SERVER" "TAR_PATH='$TAR_PATH' REMOTE_TMP='$REMOTE_TMP' IMAGE_TAG='$IMAGE_TAG' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

echo "    -- extract"
rm -rf "$REMOTE_TMP"
mkdir -p "$REMOTE_TMP"
tar xzf "$TAR_PATH" -C "$REMOTE_TMP"

echo "    -- docker build (vanaf project-root, -f backend/Dockerfile)"
cd "$REMOTE_TMP"
docker build --quiet -f backend/Dockerfile -t "$IMAGE_TAG" -t "gym-api:$(date +%Y%m%d-%H%M%S)" .

echo "    -- stop + rename oude container"
if docker ps -a --format '{{.Names}}' | grep -qx gym-api; then
  docker stop gym-api >/dev/null
  # remove any stale backup
  docker rm gym-api-prev 2>/dev/null || true
  docker rename gym-api gym-api-prev
fi

echo "    -- start nieuwe container"
docker run -d --name gym-api \
  --restart unless-stopped \
  --network cloudwijk_default \
  -p 8002:8001 \
  --env-file /opt/gym/.env \
  "$IMAGE_TAG" >/dev/null

echo "    -- wachten op startup (8s)"
sleep 8

echo "    -- status"
docker ps --filter name=^gym-api$ --format '       {{.Names}}: {{.Status}}'

echo "    -- nutrition routes check"
ROUTES=$(docker exec gym-api curl -sf http://localhost:8001/openapi.json 2>/dev/null | \
  python3 -c "import sys,json; print(sum(1 for p in json.load(sys.stdin).get('paths',{}) if 'nutrition' in p))" 2>/dev/null || echo "0")
echo "       nutrition route count: $ROUTES"
if [ "$ROUTES" -lt 5 ]; then
  echo "    !! ROUTES TE LAAG — rolling back"
  docker stop gym-api && docker rm gym-api
  docker rename gym-api-prev gym-api
  docker start gym-api
  echo "    rolled back. aborting."
  exit 1
fi

echo "    -- cleanup"
docker rm gym-api-prev >/dev/null 2>&1 || true
rm -rf "$REMOTE_TMP" "$TAR_PATH"
echo "    done."
REMOTE_SCRIPT

rm -f "$TAR_PATH"
echo ""
echo "==> 4. lokale cleanup"
echo ""
echo "Klaar. https://gym.cloudwijk.nl"
