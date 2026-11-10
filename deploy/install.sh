#!/usr/bin/env bash
# Установка бота на сервер (Ubuntu / Debian) как службы: запускается сам и перезапускается после сбоя.
# Запуск из папки проекта:  sudo bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=site-prospector
RUN_USER=prospector

if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo: sudo bash deploy/install.sh"; exit 1; fi

echo "→ Ставлю Python"
apt-get update -qq
apt-get install -y -qq python3 python3-venv >/dev/null

echo "→ Пользователь $RUN_USER (бот работает не от root)"
id -u "$RUN_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"

echo "→ Библиотеки"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

for f in token.txt config.json; do
  if [ ! -s "$APP_DIR/$f" ]; then echo "⚠ Нет файла $f — создайте его (см. README) и перезапустите: sudo systemctl restart $SERVICE"; fi
done
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"
chmod 600 "$APP_DIR"/token.txt "$APP_DIR"/config.json "$APP_DIR"/keys.json 2>/dev/null || true

echo "→ Служба $SERVICE"
cat > /etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=site-prospector Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now $SERVICE

PORT=$(python3 -c "import json;print(json.load(open('$APP_DIR/config.json')).get('demo',{}).get('port',''))" 2>/dev/null || true)
if [ -n "$PORT" ] && command -v ufw >/dev/null && ufw status | grep -q active; then
  echo "→ Открываю порт $PORT для демо-страниц"
  ufw allow "$PORT/tcp" >/dev/null
fi

echo
echo "Готово. Состояние: sudo systemctl status $SERVICE"
echo "Журнал (что бот пишет):  sudo journalctl -u $SERVICE -f"
