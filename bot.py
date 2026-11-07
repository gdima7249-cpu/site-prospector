#!/usr/bin/env python3
"""Telegram-бот: «стоматология Казань» → проверенные сайты компаний и черновики писем.

Где берутся сайты: OpenStreetMap (открытая карта, ключи не нужны). Город ищется через Nominatim,
организации нужной ниши с указанным сайтом — через Overpass API. Затем каждый сайт проверяется
тем же аудитом, что и в prospector.py.

Бот ничего никому не отправляет: он присылает вам черновики, а письма вы отправляете сами.

Запуск: положите токен от @BotFather в файл token.txt рядом со скриптом (или в переменную
TELEGRAM_BOT_TOKEN) и запустите `python bot.py` (на Windows — start.bat).
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests
from dataclasses import asdict

import prospector as p

HERE = Path(__file__).resolve().parent
STATE = HERE / "bot_state.json"
UA = "site-prospector-bot/1.0"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
MAX_SITES = 40   # сколько сайтов проверять за один поиск
SHOW_TOP = 10    # сколько худших показывать

# ниша → теги OpenStreetMap
NICHES: dict[str, list[tuple[str, str]]] = {
    "стоматология": [("amenity", "dentist")],
    "клиника": [("amenity", "clinic"), ("amenity", "doctors")],
    "ветеринар": [("amenity", "veterinary")],
    "автосервис": [("shop", "car_repair")],
    "автомойка": [("amenity", "car_wash")],
    "шиномонтаж": [("shop", "tyres")],
    "салон красоты": [("shop", "beauty"), ("shop", "hairdresser")],
    "парикмахерская": [("shop", "hairdresser")],
    "фитнес": [("leisure", "fitness_centre")],
    "юрист": [("office", "lawyer")],
    "нотариус": [("office", "notary")],
    "бухгалтер": [("office", "accountant")],
    "недвижимость": [("office", "estate_agent")],
    "строители": [("craft", "builder")],
    "сантехник": [("craft", "plumber")],
    "электрик": [("craft", "electrician")],
    "окна": [("craft", "window_construction")],
    "мебель": [("shop", "furniture")],
    "цветы": [("shop", "florist")],
    "кафе": [("amenity", "cafe")],
    "ресторан": [("amenity", "restaurant")],
    "гостиница": [("tourism", "hotel"), ("tourism", "guest_house")],
    "турагентство": [("shop", "travel_agency")],
    "автошкола": [("amenity", "driving_school")],
    "языковая школа": [("amenity", "language_school")],
    "оптика": [("shop", "optician")],
    "ювелир": [("shop", "jewelry")],
    "зоомагазин": [("shop", "pet")],
}
ALIASES = {
    "стоматолог": "стоматология", "зубной": "стоматология", "медцентр": "клиника", "медицинский": "клиника",
    "ветклиника": "ветеринар", "сто": "автосервис", "автомастерская": "автосервис", "барбершоп": "парикмахерская",
    "красоты": "салон красоты", "спортзал": "фитнес", "адвокат": "юрист", "ремонт": "строители",
    "отель": "гостиница", "хостел": "гостиница", "кофейня": "кафе", "окна пвх": "окна",
}

HELP = ("Напишите нишу и город, например:\n"
        "стоматология Казань\nавтосервис Нижний Новгород\n\n"
        "Ниши: " + ", ".join(NICHES) + "\n\n"
        "Команды:\n/draft домен — черновик письма для сайта из последнего поиска\n"
        "/skip домен — больше не показывать этот сайт\n/help — эта справка")


# ───────────────────────────── состояние ─────────────────────────────
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"owner": None, "offset": 0, "seen": [], "skip": [], "last": {}}


def save_state(st: dict):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────────── разбор запроса ─────────────────────────────
def parse_request(text: str) -> tuple[str, str] | None:
    """«Стоматология  Казань» → ("стоматология", "Казань"). Ниша — самое длинное совпадение в начале."""
    t = " ".join(text.split())
    low = t.lower()
    names = sorted(list(NICHES) + list(ALIASES), key=len, reverse=True)
    for n in names:
        if low.startswith(n + " "):
            city = t[len(n):].strip()
            return ALIASES.get(n, n), city
    return None


# ───────────────────────────── OpenStreetMap ─────────────────────────────
def find_city(city: str) -> tuple[float, float, float, float] | None:
    """Границы города (юг, запад, север, восток) через Nominatim."""
    r = requests.get(NOMINATIM, params={"q": city, "format": "json", "limit": 1, "accept-language": "ru",
                                        "countrycodes": "ru,by,kz,ua"},
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    s, n, w, e = map(float, data[0]["boundingbox"])
    return s, w, n, e


def overpass_query(tags: list[tuple[str, str]], bbox: tuple[float, float, float, float]) -> str:
    box = ",".join(f"{x:.5f}" for x in bbox)
    parts = []
    for k, v in tags:
        for site_key in ("website", "contact:website", "url"):
            parts.append(f'  nwr["{k}"="{v}"]["{site_key}"]({box});')
    return "[out:json][timeout:90];\n(\n" + "\n".join(parts) + "\n);\nout tags;"


def find_businesses(niche: str, city: str) -> list[dict]:
    bbox = find_city(city)
    if bbox is None:
        raise LookupError(f"Не нашёл город «{city}»")
    r = requests.post(OVERPASS, data={"data": overpass_query(NICHES[niche], bbox)},
                      headers={"User-Agent": UA}, timeout=120)
    r.raise_for_status()
    found, seen = [], set()
    for el in r.json().get("elements", []):
        tags = el.get("tags", {})
        site = tags.get("website") or tags.get("contact:website") or tags.get("url")
        domain = p.normalize(site or "")
        if not domain or domain in seen or p.root_domain_skipped(domain, p.SKIP_DOMAINS):
            continue
        seen.add(domain)
        found.append({"name": tags.get("name", ""), "site": site, "domain": domain,
                      "email": tags.get("email") or tags.get("contact:email") or "",
                      "phone": tags.get("phone") or tags.get("contact:phone") or ""})
    return found


# ───────────────────────────── аудит и вывод ─────────────────────────────
def sender_info() -> dict:
    path = HERE / "sender.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {"name": "[ваше имя]", "company": "",
            "offer": "Если интересно, бесплатно пришлю короткий разбор: что именно поправить и во сколько это обойдётся.",
            "signature": "[подпись: имя, телефон, Telegram]"}


def audit_many(businesses: list[dict]) -> list[tuple[dict, p.Audit]]:
    fetcher = p.Fetcher(p.DEFAULT_UA)

    def one(b):
        try:
            a = p.audit_site(b["site"], fetcher, check_links=10, psi=False, psi_key=None)
        except Exception as e:  # noqa: BLE001 — один сайт не должен ронять поиск
            a = p.Audit(input=b["site"], domain=b["domain"], error=type(e).__name__)
        if b["email"] and b["email"].lower() not in a.emails:
            a.emails.insert(0, b["email"].lower())
        return b, a

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one, businesses))


def format_site(i: int, b: dict, a: p.Audit) -> str:
    lines = [f"{i}. {b['name'] or a.domain} — {a.domain}", f"   оценка {a.score}/100"]
    for t in p.top_issues(a, 3):
        lines.append(f"   • {t}")
    contact = ", ".join(a.emails[:2]) or (f"тел. {b['phone']}" if b["phone"] else "контактов не нашёл")
    lines.append(f"   ✉ {contact}")
    return "\n".join(lines)


def csv_bytes(rows: list[tuple[dict, p.Audit]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["компания", "домен", "оценка", "email", "телефон", "проблемы", "ошибка"])
    for b, a in rows:
        w.writerow([b["name"], a.domain, a.score, ", ".join(a.emails[:3]), b["phone"],
                    " | ".join(p.issue_text(k, v) for k, v in a.issues.items()), a.error])
    return buf.getvalue().encode("utf-8-sig")


def draft_text(a: p.Audit) -> str:
    subject, body = p.template_draft(a, sender_info())
    to = a.emails[0] if a.emails else "(email не найден — ищите на сайте или звоните)"
    return f"Кому: {to}\nТема: {subject}\n\n{body}"


# ───────────────────────────── Telegram ─────────────────────────────
class Bot:
    def __init__(self, token: str):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.st = load_state()
        self.busy = threading.Lock()

    def call(self, method: str, timeout: int = 60, **params):
        r = requests.post(self.api + method, data=params, timeout=timeout)
        r.raise_for_status()
        return r.json().get("result")

    def say(self, chat: int, text: str):
        while text:  # лимит Telegram — 4096 символов
            if len(text) <= 4000:
                cut = text
            else:
                idx = text.rfind("\n\n", 0, 4000)
                cut = text[:idx if idx > 0 else 4000]
            self.call("sendMessage", chat_id=chat, text=cut, disable_web_page_preview="true")
            text = text[len(cut):].lstrip()

    def send_file(self, chat: int, name: str, content: bytes, caption: str = ""):
        requests.post(self.api + "sendDocument", data={"chat_id": chat, "caption": caption},
                      files={"document": (name, content)}, timeout=60).raise_for_status()

    def requests_updates(self):
        r = requests.post(self.api + "getUpdates", data={"offset": self.st["offset"], "timeout": 50}, timeout=70)
        r.raise_for_status()
        return r.json().get("result")

    def run(self):
        print("Бот запущен. Напишите ему в Telegram. Остановить — Ctrl+C.")
        while True:
            try:
                updates = self.requests_updates()
            except requests.RequestException as e:
                print(f"Нет связи с Telegram ({type(e).__name__}), повтор через 5 с")
                time.sleep(5)
                continue
            for u in updates or []:
                self.st["offset"] = u["update_id"] + 1
                save_state(self.st)
                msg = u.get("message") or {}
                if "text" in msg:
                    try:
                        self.handle(msg["chat"]["id"], msg["from"]["id"], msg["text"].strip())
                    except Exception as e:  # noqa: BLE001
                        print(f"Ошибка обработки: {e!r}")

    def handle(self, chat: int, user: int, text: str):
        if self.st["owner"] is None:  # первый, кто написал, становится владельцем; остальным бот не отвечает
            self.st["owner"] = user
            save_state(self.st)
        if user != self.st["owner"]:
            return
        cmd, _, arg = text.partition(" ")
        if cmd in ("/start", "/help"):
            return self.say(chat, HELP)
        if cmd == "/draft":
            d = p.normalize(arg) if arg else None
            a = self.st["last"].get(d) if d else None
            if not a:
                return self.say(chat, "Укажите домен из последнего поиска: /draft romashka.ru")
            return self.say(chat, draft_text(p.Audit(**a)))
        if cmd == "/skip":
            d = p.normalize(arg) if arg else None
            if not d:
                return self.say(chat, "Например: /skip romashka.ru")
            self.st["skip"].append(d)
            save_state(self.st)
            return self.say(chat, f"{d} больше не покажу.")
        req = parse_request(text)
        if not req:
            return self.say(chat, "Не понял нишу.\n\n" + HELP)
        if not self.busy.acquire(blocking=False):
            return self.say(chat, "Ещё ищу по прошлому запросу, подождите.")
        threading.Thread(target=self.search, args=(chat, *req), daemon=True).start()

    def search(self, chat: int, niche: str, city: str):
        try:
            self.say(chat, f"Ищу «{niche}» в городе {city}…")
            try:
                found = find_businesses(niche, city)
            except LookupError as e:
                return self.say(chat, str(e))
            except requests.RequestException as e:
                return self.say(chat, f"Карта OpenStreetMap не ответила ({type(e).__name__}). Попробуйте через минуту.")
            skip = set(self.st["skip"]) | set(self.st["seen"]) | p.load_suppression(HERE / "do_not_contact.txt")
            fresh = [b for b in found if b["domain"] not in skip][:MAX_SITES]
            if not fresh:
                return self.say(chat, f"Сайтов не нашёл (всего в карте с сайтом: {len(found)}, новых: 0). "
                                      "Попробуйте другую нишу или город побольше.")
            self.say(chat, f"Нашёл {len(found)} компаний с сайтом, проверяю {len(fresh)}. Это займёт пару минут.")
            rows = audit_many(fresh)
            ok = sorted([r for r in rows if r[1].ok], key=lambda r: -r[1].score)
            self.st["seen"] += [b["domain"] for b, _ in rows]
            self.st["last"] = {a.domain: asdict(a) for _, a in ok}
            save_state(self.st)
            bad = [r for r in ok if r[1].score >= 30][:SHOW_TOP]
            if not bad:
                self.say(chat, f"Проверил {len(ok)} сайтов — явно слабых нет. Попробуйте другую нишу.")
            else:
                text = f"Самые слабые сайты ({len(bad)} из {len(ok)} проверенных):\n\n"
                text += "\n\n".join(format_site(i, b, a) for i, (b, a) in enumerate(bad, 1))
                text += "\n\nЧерновик письма: /draft домен\nНе показывать больше: /skip домен"
                self.say(chat, text)
            self.send_file(chat, f"{niche}-{city}.csv".replace(" ", "_"), csv_bytes(ok + [r for r in rows if not r[1].ok]),
                           "Полный отчёт (открывается в Excel)")
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка поиска: {e!r}")
            self.say(chat, f"Что-то сломалось: {type(e).__name__}. Подробности — в окне, где запущен бот.")
        finally:
            self.busy.release()


def get_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    f = HERE / "token.txt"
    if not token and f.exists():
        token = f.read_text(encoding="utf-8-sig").strip()
    if not token:
        sys.exit("Нет токена. Создайте бота у @BotFather в Telegram и вставьте токен в файл token.txt рядом с bot.py.")
    return token


if __name__ == "__main__":
    Bot(get_token()).run()
