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

# Файл подкачки (swap): браузеру для PDF нужно несколько сотен МБ памяти. На сервере с 1–2 ГБ и без подкачки
# система в нехватке памяти убивает браузер — отсюда «PDF не собрался» и «браузер не отвечает».
# Создаём 1 ГБ подкачки, только если её нет совсем, памяти меньше 2 ГБ и на диске свободно больше 3 ГБ.
if [ "${NO_SWAP:-0}" != "1" ] && [ -d "$APP_DIR/.browsers" ]; then
  MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
  FREE_MB=$(df -Pm / | awk 'NR==2 {print $4}')
  if [ "$SWAP_MB" -eq 0 ] && [ "$MEM_MB" -lt 2000 ] && [ "$FREE_MB" -gt 3000 ] && [ ! -e /swapfile ]; then
    echo "→ Памяти ${MEM_MB} МБ и нет подкачки — создаю файл подкачки 1 ГБ (/swapfile), чтобы браузер для PDF не падал"
    if (fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none) \
       && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
      grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
      echo "   Готово: подкачка включена и останется после перезагрузки"
    else
      echo "⚠ Подкачку создать не получилось (на некоторых VPS это запрещено) — бот работает и без неё"
      swapoff /swapfile 2>/dev/null || true; rm -f /swapfile
    fi
  fi
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
