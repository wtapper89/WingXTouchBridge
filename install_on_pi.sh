#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/wing-xtouch-bridge"
CONFIG_DIR="/etc/wing-xtouch-bridge"
APP_USER="${SUDO_USER:-pi}"
APP_GROUP="$(id -gn "${APP_USER}")"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo: sudo ./install_on_pi.sh"
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip libasound2-dev libjack-jackd2-dev build-essential

install -d -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" "${CONFIG_DIR}"
cp -R app.py requirements.txt "${APP_DIR}/"

if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
  cp config.example.json "${CONFIG_DIR}/config.json"
fi
chown "${APP_USER}:${APP_GROUP}" "${CONFIG_DIR}/config.json"

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

sed \
  -e "s/^User=.*/User=${APP_USER}/" \
  -e "s/^Group=.*/Group=${APP_GROUP}/" \
  systemd/wing-xtouch-bridge.service > /etc/systemd/system/wing-xtouch-bridge.service
systemctl daemon-reload
systemctl enable wing-xtouch-bridge.service
systemctl restart wing-xtouch-bridge.service

echo "Installed. Open http://$(hostname -I | awk '{print $1}'):8088/"
