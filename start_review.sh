#!/bin/bash
# simsa 검토 서버 로컬 실행 — PostgreSQL 컨테이너 자동 기동 포함.
# usage: ./start_review.sh [--host 127.0.0.1] [--port 8766]
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "Docker 가 실행 중이 아닙니다. Docker Desktop 을 먼저 켜주세요." >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q '^simsa-postgres$'; then
  if docker ps -a --format '{{.Names}}' | grep -q '^simsa-postgres$'; then
    echo "simsa-postgres 컨테이너 시작..."
    docker start simsa-postgres >/dev/null
  else
    echo "simsa-postgres 컨테이너 생성..."
    docker run -d --name simsa-postgres --restart unless-stopped \
      -e POSTGRES_USER=simsa -e POSTGRES_PASSWORD=simsa_local_dev -e POSTGRES_DB=simsa \
      -p 5544:5432 -v simsa_pgdata:/var/lib/postgresql/data postgres:16 >/dev/null
  fi
fi

echo "PostgreSQL 준비 대기..."
until docker exec simsa-postgres pg_isready -U simsa -q 2>/dev/null; do sleep 1; done

# 스키마 적용·시드는 review_app 시작 시 자동 실행됨
(sleep 2 && open "http://127.0.0.1:8766") &
exec python3 review_app.py "$@"
