#!/usr/bin/env bash
set -euo pipefail
umask 027

PROJECT_DIR="/opt/football-system"
DATA_DIR="/var/lib/football-data"
VENV_DIR="${PROJECT_DIR}/.venv"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 运行安装程序。" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-pip python3.12-venv git curl unzip jq \
  ca-certificates util-linux

mkdir -p "${PROJECT_DIR}" "${DATA_DIR}"/{raw,models,backups} \
  /var/log/football-system
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" config set global.index-url \
  https://mirrors.cloud.tencent.com/pypi/simple
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/pip" install --upgrade "${PROJECT_DIR}"
# Local upgrades can otherwise keep an already-installed wheel of the same
# version. Reinstall the just-extracted project code without touching data.
"${VENV_DIR}/bin/pip" install --force-reinstall --no-deps "${PROJECT_DIR}"
"${VENV_DIR}/bin/python" -c \
  "import football_analysis; print('当前程序版本:', football_analysis.__version__)"
"${VENV_DIR}/bin/playwright" install --with-deps chromium

chmod +x "${PROJECT_DIR}/scripts/install_service.sh"
"${VENV_DIR}/bin/football-analysis" init-db
bash "${PROJECT_DIR}/scripts/install_service.sh"

echo "安装完成：实时采集、历史回填、训练、备份和存储维护均已启用。"
