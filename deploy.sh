#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker，请先安装 Docker Engine。"
  exit 1
fi

COMPOSE=(docker compose)
if ! "${COMPOSE[@]}" version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    echo "未检测到 docker compose 或 docker-compose，请先安装 Docker Compose。"
    exit 1
  fi
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
fi

GENERATED_PASSWORD=""
APP_PASSWORD_VALUE="$(grep '^APP_PASSWORD=' .env | tail -n 1 | cut -d= -f2- || true)"
if [[ -z "${APP_PASSWORD_VALUE}" || "${APP_PASSWORD_VALUE}" == "please-change-this-password" ]]; then
  GENERATED_PASSWORD="$(openssl rand -hex 16)"
  sed -i "s|^APP_PASSWORD=.*|APP_PASSWORD=${GENERATED_PASSWORD}|" .env
fi

APP_SECRET_VALUE="$(grep '^APP_SECRET=' .env | tail -n 1 | cut -d= -f2- || true)"
if [[ -z "${APP_SECRET_VALUE}" || "${APP_SECRET_VALUE}" == "please-change-this-to-a-long-random-secret" ]]; then
  GENERATED_SECRET="$(openssl rand -hex 32)"
  sed -i "s|^APP_SECRET=.*|APP_SECRET=${GENERATED_SECRET}|" .env
fi

chmod 600 .env
mkdir -p data
"${COMPOSE[@]}" up -d --build
"${COMPOSE[@]}" ps
PORT_VALUE="$(grep '^PORT=' .env | tail -n 1 | cut -d= -f2- || true)"
PORT_VALUE="${PORT_VALUE:-8080}"
echo "部署完成：请访问 http://服务器IP:${PORT_VALUE}"
if [[ -n "${GENERATED_PASSWORD}" ]]; then
  echo "首次生成的管理密码：${GENERATED_PASSWORD}"
  echo "请立即保存该密码。"
fi
