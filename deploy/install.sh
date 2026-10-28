#!/usr/bin/env bash
# Установка tgsave на сервер. Запускать от root.
#
#   API_ID=12345 API_HASH=abcdef PHONE=+79990000000 bash deploy/install.sh
#
# Переменные:
#   API_ID, API_HASH, PHONE — обязательны при первой установке
#   TARGET                 — куда складывать копии (по умолчанию me)
#   TZ                     — часовой пояс для шапок сообщений
#
set -euo pipefail

APP_DIR=/opt/tgsave
SERVICE=tgsave
SERVICE_USER=tgsave
STATE_DIR=/var/lib/tgsave
ENV_FILE=/etc/tgsave.env
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ${EUID} -ne 0 ]]; then
    echo "нужен root: sudo API_ID=... API_HASH=... PHONE=... bash $0" >&2
    exit 1
fi

command -v python3 >/dev/null || { echo "python3 не найден" >&2; exit 1; }

if ! python3 -c "import venv" 2>/dev/null; then
    echo "ставлю python3-venv"
    apt-get update -qq && apt-get install -y -qq python3-venv
fi

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 755 "${APP_DIR}"
install -m 644 "${SRC}/userbot.py" "${SRC}/login.py" "${SRC}/menu.py" \
    "${SRC}/requirements.txt" "${APP_DIR}/"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 700 "${STATE_DIR}"

if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
    python3 -m venv "${APP_DIR}/venv"
fi
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

if [[ ! -f "${ENV_FILE}" ]]; then
    if [[ -z "${API_ID:-}" || -z "${API_HASH:-}" || -z "${PHONE:-}" ]]; then
        echo "нет ${ENV_FILE}, а API_ID/API_HASH/PHONE не заданы" >&2
        exit 1
    fi
    umask 077
    cat > "${ENV_FILE}" <<EOF
API_ID=${API_ID}
API_HASH=${API_HASH}
PHONE=${PHONE}
SESSION=${STATE_DIR}/session
DATA_DIR=${STATE_DIR}
TARGET=${TARGET:-me}
TZ=${TZ:-UTC}
BOT_TOKEN=${BOT_TOKEN:-}
OWNER_ID=${OWNER_ID:-}
EOF
    chmod 600 "${ENV_FILE}"
    echo "создан ${ENV_FILE}"
fi

install -m 644 "${SRC}/deploy/tgsave.service" "/etc/systemd/system/${SERVICE}.service"
install -m 644 "${SRC}/deploy/tgsave-menu.service" "/etc/systemd/system/${SERVICE}-menu.service"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}" "${APP_DIR}"
systemctl daemon-reload
systemctl enable "${SERVICE}.service" >/dev/null
if grep -q '^BOT_TOKEN=.' "${ENV_FILE}"; then
    systemctl enable --now "${SERVICE}-menu.service" >/dev/null
fi

cat <<EOF

Код и venv готовы, unit включён, но не запущен — сначала вход в аккаунт.

  set -a; . ${ENV_FILE}; set +a

  runuser -u ${SERVICE_USER} -- env API_ID=\$API_ID API_HASH=\$API_HASH \\
    SESSION=\$SESSION DATA_DIR=\$DATA_DIR HOME=${STATE_DIR} \\
    ${APP_DIR}/venv/bin/python ${APP_DIR}/login.py send --phone "\$PHONE"

Код придёт в Telegram (чат «Telegram»). Затем:

  runuser -u ${SERVICE_USER} -- env API_ID=\$API_ID API_HASH=\$API_HASH \\
    SESSION=\$SESSION DATA_DIR=\$DATA_DIR HOME=${STATE_DIR} \\
    ${APP_DIR}/venv/bin/python ${APP_DIR}/login.py code --code 12345
  # если включена 2FA — добавь --password 'пароль'

После AUTHORIZED:

  systemctl start ${SERVICE}
  journalctl -u ${SERVICE} -f
EOF
