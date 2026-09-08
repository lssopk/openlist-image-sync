#!/usr/bin/env bash
set -Eeuo pipefail

# One-click Debian/Ubuntu installer.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install.sh | sudo bash

PROJECT_NAME="openlist-image-sync"
DEFAULT_PROJECT_DIR="/opt/${PROJECT_NAME}"
REPO_URL="${REPO_URL:-https://github.com/lssopk/openlist-image-sync.git}"
BRANCH="${BRANCH:-main}"
PROJECT_DIR="${PROJECT_DIR:-${DEFAULT_PROJECT_DIR}}"
GENERATED_PASSWORD=""

if [[ "${EUID}" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "请使用 root 运行，或先安装 sudo。" >&2
    exit 1
  fi
  SUDO=(sudo)
else
  SUDO=()
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "debian" && "${ID_LIKE:-}" != *debian* && "${ID:-}" != "ubuntu" ]]; then
    echo "此一键脚本面向 Debian/Ubuntu，检测到：${PRETTY_NAME:-未知系统}" >&2
    exit 1
  fi
fi

echo "[1/5] 安装基础依赖..."
"${SUDO[@]}" apt-get update
"${SUDO[@]}" apt-get install -y ca-certificates curl git openssl

echo "[2/5] 检查 Docker..."
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "未检测到可用的 Docker Compose，开始安装 Docker 官方版本..."
  curl -fsSL https://get.docker.com | "${SUDO[@]}" sh
fi
"${SUDO[@]}" systemctl enable --now docker >/dev/null 2>&1 || true
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose 插件安装失败，请执行 docker compose version 检查。" >&2
  exit 1
fi

SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

echo "[3/5] 准备项目目录：${PROJECT_DIR}"
if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/docker-compose.yml" ]]; then
  PROJECT_DIR="${SCRIPT_DIR}"
elif [[ -d "${PROJECT_DIR}/.git" ]]; then
  git -C "${PROJECT_DIR}" fetch origin "${BRANCH}"
  git -C "${PROJECT_DIR}" checkout "${BRANCH}"
  git -C "${PROJECT_DIR}" pull --ff-only origin "${BRANCH}"
elif [[ -e "${PROJECT_DIR}" ]]; then
  echo "目标目录已存在但不是本项目 Git 仓库：${PROJECT_DIR}" >&2
  echo "如要使用该目录，请设置 PROJECT_DIR=/正确目录 后重新运行。" >&2
  exit 1
else
  "${SUDO[@]}" mkdir -p "$(dirname "${PROJECT_DIR}")"
  "${SUDO[@]}" git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${PROJECT_DIR}"
fi

cd "${PROJECT_DIR}"

echo "[4/5] 创建安全配置..."
if [[ ! -f .env ]]; then
  "${SUDO[@]}" cp .env.example .env
  GENERATED_PASSWORD="${APP_PASSWORD:-$(openssl rand -hex 16)}"
  GENERATED_SECRET="${APP_SECRET:-$(openssl rand -hex 32)}"
  # Values generated above are hexadecimal and therefore safe for this
  # replacement. Users may edit .env later for a custom password/port.
  "${SUDO[@]}" sed -i "s|^APP_PASSWORD=.*|APP_PASSWORD=${GENERATED_PASSWORD}|" .env
  "${SUDO[@]}" sed -i "s|^APP_SECRET=.*|APP_SECRET=${GENERATED_SECRET}|" .env
  "${SUDO[@]}" chmod 600 .env
fi
"${SUDO[@]}" mkdir -p data

echo "[5/5] 构建并启动服务..."
"${SUDO[@]}" docker compose up -d --build
"${SUDO[@]}" docker compose ps

echo
echo "安装完成。"
echo "访问地址：http://服务器IP:$(grep '^PORT=' .env | cut -d= -f2- || echo 8080)"
echo "管理账号：$(grep '^APP_USERNAME=' .env | cut -d= -f2- || echo admin)"
if [[ -n "${GENERATED_PASSWORD}" ]]; then
  echo "首次生成的管理密码：${GENERATED_PASSWORD}"
  echo "请立即保存该密码；以后可在 .env 中修改。"
fi
echo "查看日志：cd ${PROJECT_DIR} && docker compose logs -f --tail=100"
