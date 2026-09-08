#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker，请先安装 Docker Engine。"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "未检测到 Docker Compose 插件，请先安装 docker compose。"
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已创建 .env，请先修改 APP_PASSWORD 和 APP_SECRET，然后再次运行此脚本。"
  exit 0
fi

docker compose up -d --build
docker compose ps
echo "部署完成：请访问 http://服务器IP:8080"
