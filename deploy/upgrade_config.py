#!/usr/bin/env python3
"""Обновляет config.json на сервере под новую версию бота, не трогая почту, пароли и адрес демо.

- расписание: все города (Россия, Казахстан, Беларусь — из cities.json) и все ниши (из niches.json);
- старые маленькие дневные лимиты расписания (10 карточек, 8 запросов) поднимает до 20 карточек и 40 запросов;
- добавляет дневные лимиты запросов к Яндексу и 2ГИС, если их нет.
Перед изменением сохраняет копию: config.json.bak-ГГГГММДД-ЧЧММСС. Повторный запуск ничего не ломает.
Запуск: python3 deploy/upgrade_config.py [путь к config.json]
"""
import json
import shutil
import sys
import time
from pathlib import Path

DEFAULT_SCHEDULE = {"time": "10:00", "tz": "Europe/Moscow", "niches": "все", "cities": "все",
                    "countries": ["RU", "KZ", "BY"], "max_cards": 20, "max_searches": 40}


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
    if int(sched.get("max_cards", 0)) < 20:
        sched["max_cards"] = 20
        changes.append("schedule.max_cards → 20")
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
