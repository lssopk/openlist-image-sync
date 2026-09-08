#!/usr/bin/env bash
set -Eeuo pipefail

# One-click deployment for hosts that already have Docker + Compose.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install-docker-existing.sh | sudo bash

export EXISTING_DOCKER_ONLY=true
INSTALL_URL="https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install.sh"

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "${SCRIPT_DIR}/install.sh" ]]; then
    exec bash "${SCRIPT_DIR}/install.sh"
  fi
fi

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "${INSTALL_URL}" | bash
elif command -v wget >/dev/null 2>&1; then
  wget -qO- "${INSTALL_URL}" | bash
else
  echo "需要 curl 或 wget 来下载安装脚本。" >&2
  exit 1
fi
