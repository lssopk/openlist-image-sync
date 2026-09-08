#!/usr/bin/env bash
set -Eeuo pipefail

# Native Python + systemd installer for Debian/Ubuntu.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install-native.sh | sudo bash

PROJECT_NAME="openlist-image-sync"
DEFAULT_PROJECT_DIR="/opt/${PROJECT_NAME}"
REPO_URL="${REPO_URL:-https://github.com/lssopk/openlist-image-sync.git}"
BRANCH="${BRANCH:-main}"
PROJECT_DIR="${PROJECT_DIR:-${DEFAULT_PROJECT_DIR}}"
SERVICE_USER="${SERVICE_USER:-openlist-sync}"
GENERATED_PASSWORD=""

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 或 sudo 运行此脚本。" >&2
  exit 1
fi

echo "[1/6] 安装 Python 和系统依赖..."
apt-get update
apt-get install -y ca-certificates git openssl python3 python3-venv python3-pip

SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

echo "[2/6] 准备项目目录：${PROJECT_DIR}"
if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/requirements.txt" ]]; then
  PROJECT_DIR="${SCRIPT_DIR}"
elif [[ -d "${PROJECT_DIR}/.git" ]]; then
  git -C "${PROJECT_DIR}" fetch origin "${BRANCH}"
  git -C "${PROJECT_DIR}" checkout "${BRANCH}"
  git -C "${PROJECT_DIR}" pull --ff-only origin "${BRANCH}"
elif [[ -e "${PROJECT_DIR}" ]]; then
  echo "目标目录已存在但不是本项目 Git 仓库：${PROJECT_DIR}" >&2
  exit 1
else
  mkdir -p "$(dirname "${PROJECT_DIR}")"
  git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${PROJECT_DIR}"
fi

cd "${PROJECT_DIR}"

echo "[3/6] 创建运行用户和 Python 虚拟环境..."
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${PROJECT_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "[4/6] 创建安全配置..."
if [[ ! -f .env ]]; then
  cp .env.example .env
  GENERATED_PASSWORD="${APP_PASSWORD:-$(openssl rand -hex 16)}"
  GENERATED_SECRET="${APP_SECRET:-$(openssl rand -hex 32)}"
  sed -i "s|^APP_PASSWORD=.*|APP_PASSWORD=${GENERATED_PASSWORD}|" .env
  sed -i "s|^APP_SECRET=.*|APP_SECRET=${GENERATED_SECRET}|" .env
fi
mkdir -p data
chown -R "${SERVICE_USER}:${SERVICE_USER}" data .env
chmod 640 .env
PORT_VALUE="$(awk -F= '$1 == "PORT" {print $2}' .env | tail -n 1)"
PORT_VALUE="${PORT_VALUE:-8080}"

echo "[5/6] 写入 systemd 服务..."
cat > "/etc/systemd/system/${PROJECT_NAME}.service" <<SERVICE
[Unit]
Description=OpenList Image Sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${PROJECT_DIR}/.venv/bin/uvicorn app:app --host 0.0.0.0 --port ${PORT_VALUE}
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=${PROJECT_DIR}/data

[Install]
WantedBy=multi-user.target
SERVICE

echo "[6/6] 启动服务..."
systemctl daemon-reload
systemctl enable --now "${PROJECT_NAME}.service"
systemctl --no-pager --full status "${PROJECT_NAME}.service" || true

echo
echo "原生安装完成。"
echo "访问地址：http://服务器IP:${PORT_VALUE}"
echo "查看日志：journalctl -u ${PROJECT_NAME} -f"
if [[ -n "${GENERATED_PASSWORD}" ]]; then
  echo "首次生成的管理密码：${GENERATED_PASSWORD}"
  echo "请立即保存该密码；以后可在 ${PROJECT_DIR}/.env 中修改。"
fi
