#!/usr/bin/env python3
"""Обновляет config.json на сервере под новую версию бота, не трогая почту, пароли и адрес демо.

- расписание: все города (Россия, Казахстан, Беларусь — из cities.json) и все ниши (из niches.json);
- дневной лимит запросов расписания — не меньше 40;
- добавляет дневные лимиты запросов к Яндексу и 2ГИС, если их нет;
- версия 2 (один раз): 12 карточек на запрос и в обходе по расписанию, образец второй почты (раздел smtp2,
  пока не заполнен — бот его не трогает) и 10 писем в день на все ящики (по 5 с каждого, когда заполните вторую).
Перед изменением сохраняет копию: config.json.bak-ГГГГММДД-ЧЧММСС. Повторный запуск ничего не ломает.
Запуск: python3 deploy/upgrade_config.py [путь к config.json]
"""
import json
import shutil
import sys
import time
from pathlib import Path

DEFAULT_SCHEDULE = {"time": "10:00", "tz": "Europe/Moscow", "niches": "все", "cities": "все",
                    "countries": ["RU", "KZ", "BY"], "max_cards": 12, "max_searches": 40}
VERSION = 2
SMTP2_COMMENT = ("Вторая почта (по желанию). Заполните адрес и пароль приложения — письма будут уходить с двух ящиков "
                 "по очереди, по limits.per_day / 2 с каждого, а ответы бот будет читать в обоих. Пока здесь образец, "
                 "бот этот раздел не использует. Ответы читаются по IMAP: адрес выводится из host (smtp.yandex.ru → "
                 "imap.yandex.ru); другой — впишите \"imap_host\".")
SEARCH = {"_comment": "cards — сколько подходящих компаний присылать на ваш запрос: если в городе меньше, бот добирает ту "
                      "же нишу в других городах (сначала того же региона), но не больше чем в extra_cities городах.",
          "cards": 12, "extra_cities": 8}


def upgrade(cfg: dict) -> list[str]:
    changes = []
    sched = cfg.setdefault("schedule", {})
    for k, v in DEFAULT_SCHEDULE.items():
        if k not in sched:
            sched[k] = v
            changes.append(f"schedule.{k} = {v}")
    for k in ("cities", "niches"):
        if sched.get(k) != "все":
            sched[k] = "все"
            changes.append(f"schedule.{k}: свой список → все")
    if int(cfg.get("config_version", 1)) < 2:
        if sched.get("max_cards") != 12:
            sched["max_cards"] = 12
            changes.append("schedule.max_cards → 12 (12 компаний на выбор)")
        if "search" not in cfg:
            cfg["search"] = dict(SEARCH)
            changes.append("search: 12 компаний на запрос, добор в других городах")
        if "smtp2" not in cfg:
            first = cfg.get("smtp") or {}
            cfg["smtp2"] = {"_comment": SMTP2_COMMENT, "host": first.get("host", "smtp.yandex.ru"),
                            "port": first.get("port", 465), "user": "вторая-почта@ваш-домен.ru",
                            "password": "пароль приложения", "from_name": first.get("from_name", "")}
            changes.append("smtp2: образец второй почты — заполните user и password")
        lim0 = cfg.setdefault("limits", {})
        if lim0.get("per_day") != 10:
            changes.append(f"limits.per_day: {lim0.get('per_day', 20)} → 10 (на все ящики вместе)")
            lim0["per_day"] = 10
        cfg["config_version"] = VERSION
    if int(sched.get("max_searches", 0)) < 40:
        sched["max_searches"] = 40
        changes.append("schedule.max_searches → 40")
    lim = cfg.setdefault("limits", {})
    if "api_per_day" not in lim:
        lim["api_per_day"] = {"yandex": 200, "2gis": 500}
        changes.append("limits.api_per_day = Яндекс 200, 2ГИС 500")
    sched["_comment"] = ("Раз в день бот перебирает сочетания ниш и городов, продолжая с места, где остановился. "
                         "\"все\" — все ниши из niches.json (кроме помеченных schedule: false) и все города из cities.json "
                         "стран из countries; можно вместо \"все\" дать свой список. max_cards — карточек в день, "
                         "max_searches — запросов в день.")
    return changes


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "config.json"
    if not path.exists():
        print(f"Нет {path} — пропускаю (создайте его по образцу config.example.json).")
        return
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    changes = upgrade(cfg)
    if not changes:
        print("config.json уже в новом формате.")
        return
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copymode(path, tmp)
    tmp.replace(path)
    print(f"config.json обновлён (копия старого — {backup.name}):")
    for c in changes:
        print("  " + c)


if __name__ == "__main__":
    main()
