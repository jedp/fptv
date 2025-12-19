#!/usr/bin/env bash
set -euo pipefail

APP_NAME="toytv"
APP_DIR="/opt/${APP_NAME}"
VENV_DIR="${APP_DIR}/venv"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
USER="${APP_NAME}"

# 1) Create user
if ! id -u "${USER}" >/dev/null 2>&1; then
  sudo useradd --system --create-home --home-dir "/var/lib/${APP_NAME}" --shell /usr/sbin/nologin "${USER}"
fi

# 2) Ensure groups (gpio is common on Pi OS; video/input often needed for framebuffer/DRM)
sudo usermod -aG gpio,video,input "${USER}" || true

# 3) Install OS deps (adjust as needed)
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip mpv

# 4) Copy app to /opt (assumes you run install.sh from repo root)
sudo mkdir -p "${APP_DIR}"
sudo rsync -a --delete ./ "${APP_DIR}/"
sudo chown -R root:root "${APP_DIR}"
sudo chmod -R go-w "${APP_DIR}"

# 5) Create venv
sudo -u "${USER}" python3 -m venv "${VENV_DIR}"

# 6) Install package + deps into venv
sudo -u "${USER}" "${VENV_DIR}/bin/python" -m pip install --upgrade pip
sudo -u "${USER}" "${VENV_DIR}/bin/python" -m pip install -e "${APP_DIR}"

# 7) Install systemd unit
sudo cp "${APP_DIR}/systemd/${APP_NAME}.service" "${SERVICE_FILE}"
sudo systemctl daemon-reload
sudo systemctl enable "${APP_NAME}.service"
sudo systemctl restart "${APP_NAME}.service"

echo "Installed. Check logs with: journalctl -u ${APP_NAME} -f"
