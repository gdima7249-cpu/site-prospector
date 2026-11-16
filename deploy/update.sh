#!/usr/bin/env bash
# Обновление бота на сервере одной командой (из папки проекта):
#   cd /opt/site-prospector && sudo git -c safe.directory=/opt/site-prospector pull && sudo bash deploy/update.sh
# Берёт свежий код, переводит config.json на новый формат (копия старого сохраняется), ставит библиотеки
# и перезапускает службу. Почта, пароли, токен, ключи и история бота не меняются.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE=site-prospector
RUN_USER=prospector
if [ "$(id -u)" -ne 0 ]; then echo "Запустите через sudo: sudo bash deploy/update.sh"; exit 1; fi
cd "$APP_DIR"

if [ -d .git ]; then
  echo "→ Свежий код"
  git -c safe.directory="$APP_DIR" pull --ff-only
fi

PY="$APP_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python3
echo "→ Настройки (config.json)"
"$PY" deploy/upgrade_config.py "$APP_DIR/config.json"

if [ -x "$APP_DIR/.venv/bin/pip" ]; then
  echo "→ Библиотеки"
  "$APP_DIR/.venv/bin/pip" install -q --no-cache-dir -r requirements.txt
fi

if id -u "$RUN_USER" >/dev/null 2>&1; then chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"; fi
chmod 600 "$APP_DIR"/token.txt "$APP_DIR"/config.json "$APP_DIR"/keys.json 2>/dev/null || true

echo "→ Перезапуск"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=0 status "$SERVICE" | head -3 || true
echo
echo "Готово. Напишите боту /status — там видно, сколько ниш и городов в расписании."
echo "Журнал: sudo journalctl -u $SERVICE -f"
