#!/usr/bin/env bash
set -Eeuo pipefail

# Zero-touch Debian/Ubuntu Docker installer.
# It reuses an existing Docker/Compose installation when available.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install.sh | sudo bash

PROJECT_NAME="${PROJECT_NAME:-openlist-image-sync}"
DEFAULT_PROJECT_DIR="/opt/${PROJECT_NAME}"
REPO_URL="${REPO_URL:-https://github.com/lssopk/openlist-image-sync.git}"
BRANCH="${BRANCH:-main}"
PROJECT_DIR="${PROJECT_DIR:-${DEFAULT_PROJECT_DIR}}"
EXISTING_DOCKER_ONLY="${EXISTING_DOCKER_ONLY:-false}"
GENERATED_PASSWORD=""
APT_TEMP_DIR=""
APT_ARGS=()
COMPOSE=()

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 运行，示例：curl -fsSL .../install.sh | sudo bash" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "无法识别操作系统，此脚本只支持 Debian/Ubuntu。" >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release

if [[ "${ID:-}" != "debian" && "${ID:-}" != "ubuntu" ]]; then
  echo "此脚本只支持 Debian/Ubuntu，检测到：${PRETTY_NAME:-未知系统}" >&2
  exit 1
fi

DEBIAN_MAJOR=""
if [[ "${ID}" == "debian" ]]; then
  DEBIAN_MAJOR="${VERSION_ID%%.*}"
  if [[ ! "${DEBIAN_MAJOR}" =~ ^[0-9]+$ || "${DEBIAN_MAJOR}" -lt 11 ]]; then
    echo "检测到 Debian ${VERSION_ID:-未知版本}。Debian 10/buster 已停止维护，请先升级到 Debian 11 或更高版本。" >&2
    exit 1
  fi
fi

if [[ "${EXISTING_DOCKER_ONLY}" == "1" || "${EXISTING_DOCKER_ONLY,,}" == "true" || "${EXISTING_DOCKER_ONLY,,}" == "yes" ]]; then
  EXISTING_ONLY=1
else
  EXISTING_ONLY=0
fi

cleanup_apt() {
  if [[ -n "${APT_TEMP_DIR}" && -d "${APT_TEMP_DIR}" ]]; then
    rm -rf "${APT_TEMP_DIR}"
  fi
}
trap cleanup_apt EXIT

have_package() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

find_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
    return 0
  fi
  return 1
}

resolve_codename() {
  local codename="${VERSION_CODENAME:-}"
  if [[ -n "${codename}" ]]; then
    printf '%s' "${codename}"
    return 0
  fi

  if [[ "${ID}" == "debian" ]]; then
    case "${DEBIAN_MAJOR}" in
      11) printf 'bullseye' ;;
      12) printf 'bookworm' ;;
      13) printf 'trixie' ;;
      *) return 1 ;;
    esac
    return 0
  fi

  return 1
}

prepare_isolated_apt() {
  local codename
  codename="$(resolve_codename)" || {
    echo "无法确定系统代号，不能安全生成临时软件源。" >&2
    return 1
  }

  if [[ -n "${APT_TEMP_DIR}" && -d "${APT_TEMP_DIR}" ]]; then
    rm -rf "${APT_TEMP_DIR}"
  fi
  APT_TEMP_DIR="$(mktemp -d /tmp/openlist-image-sync-apt.XXXXXX)"
  chmod 755 "${APT_TEMP_DIR}"
  install -d -m 755 \
    "${APT_TEMP_DIR}/lists/partial" \
    "${APT_TEMP_DIR}/cache/archives/partial"

  if [[ "${ID}" == "debian" ]]; then
    if [[ "${DEBIAN_MAJOR}" -le 11 ]]; then
      # Debian 11 is archived now; use only the archived base repository.
      printf 'deb [check-valid-until=no] http://archive.debian.org/debian %s main contrib non-free\n' \
        "${codename}" > "${APT_TEMP_DIR}/sources.list"
    else
      printf '%s\n' \
        "deb http://deb.debian.org/debian ${codename} main contrib non-free" \
        "deb http://deb.debian.org/debian ${codename}-updates main contrib non-free" \
        "deb http://security.debian.org/debian-security ${codename}-security main contrib non-free" \
        > "${APT_TEMP_DIR}/sources.list"
    fi
  else
    local mirror="http://archive.ubuntu.com/ubuntu"
    local security_mirror="http://security.ubuntu.com/ubuntu"
    case "${VERSION_ID:-}" in
      18.*|20.*|21.*) mirror="http://old-releases.ubuntu.com/ubuntu"; security_mirror="${mirror}" ;;
    esac
    printf '%s\n' \
      "deb [check-valid-until=no] ${mirror} ${codename} main restricted universe multiverse" \
      "deb [check-valid-until=no] ${mirror} ${codename}-updates main restricted universe multiverse" \
      "deb [check-valid-until=no] ${security_mirror} ${codename}-security main restricted universe multiverse" \
      > "${APT_TEMP_DIR}/sources.list"
  fi

  chmod 644 "${APT_TEMP_DIR}/sources.list"
  APT_ARGS=(
    -o "Dir::Etc::sourcelist=${APT_TEMP_DIR}/sources.list"
    -o "Dir::Etc::sourceparts=-"
    -o "Dir::State::lists=${APT_TEMP_DIR}/lists"
    -o "Dir::Cache::archives=${APT_TEMP_DIR}/cache/archives"
    -o "Acquire::Check-Valid-Until=false"
  )
}

apt_install() {
  local packages=("$@")
  local use_fallback=0
  local update_log

  ((${#packages[@]})) || return 0
  command -v apt-get >/dev/null 2>&1 || {
    echo "未找到 apt-get，无法自动安装依赖。" >&2
    return 1
  }

  if [[ "${ID}" == "debian" && "${DEBIAN_MAJOR}" -le 11 ]]; then
    use_fallback=1
  else
    update_log="$(mktemp /tmp/openlist-image-sync-apt-update.XXXXXX)"
    if ! apt-get update >"${update_log}" 2>&1; then
      use_fallback=1
      echo "检测到系统软件源存在旧版本或过期条目，改用临时官方源继续安装；不会修改 /etc/apt/。" >&2
    fi
    rm -f "${update_log}"
  fi

  if [[ "${use_fallback}" -eq 1 ]]; then
    prepare_isolated_apt
    apt-get "${APT_ARGS[@]}" update
  fi

  DEBIAN_FRONTEND=noninteractive apt-get "${APT_ARGS[@]}" install -y --no-install-recommends "${packages[@]}"
}

ensure_base_tools() {
  local packages=()

  have_package ca-certificates || packages+=(ca-certificates)
  command -v curl >/dev/null 2>&1 || packages+=(curl)
  command -v git >/dev/null 2>&1 || packages+=(git)
  command -v openssl >/dev/null 2>&1 || packages+=(openssl)

  if ((${#packages[@]} == 0)); then
    return 0
  fi

  if [[ "${EXISTING_ONLY}" -eq 1 ]]; then
    echo "已安装 Docker 模式不会修改系统软件源，但缺少基础命令：${packages[*]}" >&2
    echo "请先安装这些命令后重新执行，或使用普通 install.sh 让脚本自动补齐。" >&2
    return 1
  fi

  apt_install "${packages[@]}"
}

install_docker() {
  if [[ "${EXISTING_ONLY}" -eq 1 ]]; then
    echo "未检测到 Docker；当前是“仅使用已有 Docker”模式，已停止。" >&2
    echo "请安装 Docker Engine 和 docker compose 后重新执行。" >&2
    return 1
  fi

  echo "未检测到 Docker，开始自动安装..."
  ensure_base_tools

  if ! command -v docker >/dev/null 2>&1; then
    # Debian/Ubuntu packages work even when the host's normal source list is stale.
    if ! apt_install docker.io; then
      echo "系统 Docker 软件包不可用，尝试 Docker 官方安装脚本..."
      curl -fsSL https://get.docker.com | bash
    fi
  fi

  systemctl enable --now docker >/dev/null 2>&1 || true

  if ! find_compose; then
    echo "未检测到 Docker Compose，开始自动安装..."
    if ! apt_install docker-compose-plugin; then
      if ! apt_install docker-compose-v2; then
        apt_install docker-compose
      fi
    fi
  fi
}

echo "[1/5] 检查 Docker 和 Compose..."
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now docker >/dev/null 2>&1 || true
fi
if ! find_compose; then
  install_docker
else
  echo "已检测到可用 Docker/Compose，跳过 Docker 和 apt 安装。"
fi

ensure_base_tools

if ! find_compose; then
  echo "仍未找到 docker compose 或 docker-compose，请先确认 Docker Compose 已安装。" >&2
  exit 1
fi

echo "[2/5] 准备项目目录：${PROJECT_DIR}"
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/docker-compose.yml" ]]; then
  PROJECT_DIR="${SCRIPT_DIR}"
elif [[ -d "${PROJECT_DIR}/.git" ]]; then
  git -C "${PROJECT_DIR}" fetch origin "${BRANCH}"
  git -C "${PROJECT_DIR}" checkout "${BRANCH}"
  git -C "${PROJECT_DIR}" pull --ff-only origin "${BRANCH}"
elif [[ -e "${PROJECT_DIR}" ]]; then
  echo "目标目录已存在但不是本项目 Git 仓库：${PROJECT_DIR}" >&2
  echo "如需使用其他目录，请设置 PROJECT_DIR=/正确目录 后重新运行。" >&2
  exit 1
else
  mkdir -p "$(dirname "${PROJECT_DIR}")"
  git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${PROJECT_DIR}"
fi

cd "${PROJECT_DIR}"

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "${key}" "${value}" >> .env
  fi
}

echo "[3/5] 自动创建安全配置..."
if [[ ! -f .env ]]; then
  cp .env.example .env
fi

APP_PASSWORD_VALUE="$(grep '^APP_PASSWORD=' .env | tail -n 1 | cut -d= -f2- || true)"
if [[ -z "${APP_PASSWORD_VALUE}" || "${APP_PASSWORD_VALUE}" == "please-change-this-password" ]]; then
  GENERATED_PASSWORD="$(openssl rand -hex 16)"
  set_env_value APP_PASSWORD "${GENERATED_PASSWORD}"
fi

APP_SECRET_VALUE="$(grep '^APP_SECRET=' .env | tail -n 1 | cut -d= -f2- || true)"
if [[ -z "${APP_SECRET_VALUE}" || "${APP_SECRET_VALUE}" == "please-change-this-to-a-long-random-secret" ]]; then
  GENERATED_SECRET="$(openssl rand -hex 32)"
  set_env_value APP_SECRET "${GENERATED_SECRET}"
fi

chmod 600 .env
mkdir -p data

echo "[4/5] 构建并启动服务..."
"${COMPOSE[@]}" up -d --build
"${COMPOSE[@]}" ps

PORT_VALUE="$(grep '^PORT=' .env | tail -n 1 | cut -d= -f2- || true)"
PORT_VALUE="${PORT_VALUE:-8080}"
APP_USERNAME_VALUE="$(grep '^APP_USERNAME=' .env | tail -n 1 | cut -d= -f2- || true)"
APP_USERNAME_VALUE="${APP_USERNAME_VALUE:-admin}"

echo
echo "[5/5] 安装完成。"
echo "访问地址：http://服务器IP:${PORT_VALUE}"
echo "管理账号：${APP_USERNAME_VALUE}"
if [[ -n "${GENERATED_PASSWORD}" ]]; then
  echo "首次生成的管理密码：${GENERATED_PASSWORD}"
  echo "请立即保存该密码；以后可在 .env 中修改。"
fi
echo "项目目录：${PROJECT_DIR}"
echo "查看日志：cd ${PROJECT_DIR} && docker compose logs -f --tail=100"
