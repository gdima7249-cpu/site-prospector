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

# Боту нужен Python 3.9+. На старых системах (Ubuntu 20.04 — Python 3.8) ставим отдельный Python 3.12
# в папку проекта через uv. Системный Python не трогаем — им могут пользоваться другие программы на сервере.
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; }
PY=python3
if ! py_ok python3; then
  echo "→ Системный Python старый ($(python3 --version 2>&1)). Ставлю отдельный Python 3.12 в $APP_DIR/.python"
  python3 -m venv "$APP_DIR/.tools"
  "$APP_DIR/.tools/bin/pip" install -q uv
  export UV_PYTHON_INSTALL_DIR="$APP_DIR/.python"
  "$APP_DIR/.tools/bin/uv" python install 3.12
  PY="$("$APP_DIR/.tools/bin/uv" python find 3.12)"
fi
echo "   Python для бота: $PY ($("$PY" --version 2>&1))"

echo "→ Пользователь $RUN_USER (бот работает не от root)"
id -u "$RUN_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"

echo "→ Библиотеки"
if [ -x "$APP_DIR/.venv/bin/python" ] && ! py_ok "$APP_DIR/.venv/bin/python"; then
  echo "   Старое окружение на Python < 3.9 — пересоздаю"
  rm -rf -- "$APP_DIR/.venv"
fi
"$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "→ Браузер Chromium для PDF с демо (несколько сотен МБ, один раз)"
export PLAYWRIGHT_BROWSERS_PATH="$APP_DIR/.browsers"
if ! "$APP_DIR/.venv/bin/python" -m playwright install --with-deps chromium; then
  echo "⚠ Браузер не установился — бот будет работать, но без PDF (он напишет об этом в Telegram)."
  echo "  Пришлите текст ошибки выше — разберёмся."
fi

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
Environment=PLAYWRIGHT_BROWSERS_PATH=$APP_DIR/.browsers

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now $SERVICE

# порт для демо-страниц открываем, только если они нужны снаружи (задан public_url)
PORT=$(python3 -c "import json;d=json.load(open('$APP_DIR/config.json')).get('demo',{});print(d.get('port','') if d.get('public_url') else '')" 2>/dev/null || true)
if [ -n "$PORT" ] && command -v ufw >/dev/null && ufw status | grep -q active; then
  echo "→ Открываю порт $PORT для демо-страниц"
  ufw allow "$PORT/tcp" >/dev/null
fi

echo
echo "Готово. Состояние: sudo systemctl status $SERVICE"
echo "Журнал (что бот пишет):  sudo journalctl -u $SERVICE -f"
