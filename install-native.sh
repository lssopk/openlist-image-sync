#!/usr/bin/env bash
set -Eeuo pipefail

# Zero-touch Python + systemd installer for Debian/Ubuntu.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/lssopk/openlist-image-sync/main/install-native.sh | sudo bash

PROJECT_NAME="openlist-image-sync"
DEFAULT_PROJECT_DIR="/opt/${PROJECT_NAME}"
REPO_URL="${REPO_URL:-https://github.com/lssopk/openlist-image-sync.git}"
BRANCH="${BRANCH:-main}"
PROJECT_DIR="${PROJECT_DIR:-${DEFAULT_PROJECT_DIR}}"
SERVICE_USER="${SERVICE_USER:-openlist-sync}"
GENERATED_PASSWORD=""
APT_TEMP_DIR=""
APT_ARGS=()

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 root 或 sudo 运行此脚本。" >&2
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

cleanup_apt() {
  if [[ -n "${APT_TEMP_DIR}" && -d "${APT_TEMP_DIR}" ]]; then
    rm -rf "${APT_TEMP_DIR}"
  fi
}
trap cleanup_apt EXIT

have_package() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
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

ensure_dependencies() {
  local packages=()

  have_package ca-certificates || packages+=(ca-certificates)
  command -v git >/dev/null 2>&1 || packages+=(git)
  command -v openssl >/dev/null 2>&1 || packages+=(openssl)

  if ! command -v python3 >/dev/null 2>&1; then
    packages+=(python3 python3-venv python3-pip)
  else
    python3 -m venv --help >/dev/null 2>&1 || packages+=(python3-venv)
    python3 -m pip --version >/dev/null 2>&1 || packages+=(python3-pip)
  fi

  if ((${#packages[@]})); then
    apt_install "${packages[@]}"
  fi
}

echo "[1/6] 检查系统依赖..."
ensure_dependencies

echo "[2/6] 准备项目目录：${PROJECT_DIR}"
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

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

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "${key}" "${value}" >> .env
  fi
}

echo "[3/6] 创建 Python 虚拟环境并安装依赖..."
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${PROJECT_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo "[4/6] 自动创建安全配置..."
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

mkdir -p data
chown -R "${SERVICE_USER}:${SERVICE_USER}" data .env
chmod 640 .env
PORT_VALUE="$(grep '^PORT=' .env | tail -n 1 | cut -d= -f2- || true)"
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
