#!/usr/bin/env python3
"""Telegram-бот: «стоматология Казань» → компании без сайта или со слабым сайтом, приоритет, цена, черновик письма.

Источники компаний — в leads.py: OpenStreetMap (бесплатно, всегда), Яндекс и 2ГИС (если есть ключи в keys.json).
Сайты проверяются аудитом из prospector.py. Бот ничего никому не отправляет: письма вы отправляете сами.

Запуск: токен от @BotFather — в token.txt (или TELEGRAM_BOT_TOKEN), затем `python bot.py` (на Windows — start.bat).
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
from dataclasses import asdict
from pathlib import Path

import requests

import leads
import prospector as p

HERE = Path(__file__).resolve().parent
STATE = HERE / "bot_state.json"
MAX_SITES = 40      # сколько сайтов проверять за один поиск
MAX_LEADS = 150     # сколько компаний рассматривать за один поиск
SHOW_TOP = 10       # сколько показывать в сообщении
MIN_SCORE = 35      # ниже этого приоритета компания не показывается (но есть в отчёте)

HELP = """Напишите нишу и город, например:
стоматология Казань
кузовной ремонт Омск

Бот найдёт компании без сайта и со слабым сайтом, расставит приоритет и подскажет, сколько брать.

Команды:
/draft 3 — черновик письма для компании №3 из последнего поиска
/skip 3 — больше не показывать компанию №3
/niches — список ниш
/addniche натяжные потолки: потолк, натяжн — своя ниша (слова — части названий компаний)
/addniche ... | high — то же, с высоким чеком (low / mid / high)
/sources — какие источники подключены
/help — эта справка"""


# ───────────────────────────── состояние ─────────────────────────────
def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    for k, v in {"owner": None, "offset": 0, "seen": [], "skip": [], "last": []}.items():
        st.setdefault(k, v)
    if isinstance(st["last"], dict):  # формат старой версии
        st["last"] = []
    return st


def save_state(st: dict):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────────── проверка и оценка ─────────────────────────────
def sender_info() -> dict:
    path = HERE / "sender.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return {"name": "[ваше имя]", "company": "",
            "offer": "Если интересно, бесплатно пришлю короткий разбор: что именно поправить и во сколько это обойдётся.",
            "signature": "[подпись: имя, телефон, Telegram]"}


def audit_sites(businesses: list[dict]) -> dict[str, p.Audit]:
    """Проверяет сайты (до MAX_SITES), возвращает аудит по домену."""
    with_site = [b for b in businesses if b["site"]][:MAX_SITES]
    fetcher = p.Fetcher(p.DEFAULT_UA)

    def one(b):
        try:
            a = p.audit_site(b["site"], fetcher, check_links=10, psi=False, psi_key=None)
        except Exception as e:  # noqa: BLE001 — один сайт не должен ронять поиск
            a = p.Audit(input=b["site"], domain=b["domain"], error=type(e).__name__)
        return b["domain"], a

    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, with_site))


def build_leads(businesses: list[dict], audits: dict[str, p.Audit], tier: str, city: str) -> list[dict]:
    pricing = leads.load_pricing()
    out = []
    for b in businesses:
        a = audits.get(b["domain"]) if b["domain"] else None
        if b["site"] and a is None:
            continue  # сайт есть, но не проверяли (сверх лимита) — оценивать вслепую не будем
        if a and a.emails and not b["email"]:
            b = dict(b, email=a.emails[0])
        score, reasons, kind = leads.lead_score(b, a, tier)
        if kind == "unknown":
            continue
        out.append({"b": b, "a": asdict(a) if a else None, "score": score, "reasons": reasons, "kind": kind,
                    "price": leads.price_hint(b, a, kind, tier, city, pricing)})
    out.sort(key=lambda x: -x["score"])
    return out


KIND_ICON = {"no_site": "🚫 нет сайта", "dead_site": "💀 сайт не работает", "site": "🌐"}


def format_lead(i: int, L: dict) -> str:
    b = L["b"]
    head = f"{i}. {b['name']}"
    head += f" — {b['domain']}" if b["domain"] else ""
    if L["kind"] != "site":
        head += f" · {KIND_ICON[L['kind']]}"
    lines = [head, f"   приоритет {L['score']}/100: {', '.join(L['reasons']) or 'мелкие недочёты'}",
             f"   💰 {L['price']}"]
    contacts = []
    if b["email"]:
        contacts.append(b["email"])
    if b["phone"]:
        contacts.append("тел. " + b["phone"].split(";")[0].strip())
    lines.append("   ✉ " + (", ".join(contacts) or "контактов не нашёл"))
    if b.get("rating") is not None:
        lines.append(f"   ★ {b['rating']} · {b.get('reviews') or 0} отзывов ({', '.join(b['sources'])})")
    return "\n".join(lines)


def csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "компания", "сайт", "статус", "приоритет", "почему", "рекомендация", "email", "телефон",
                "адрес", "рейтинг", "отзывов", "источники"])
    status = {"no_site": "нет сайта", "dead_site": "не работает", "site": "есть"}
    for i, L in enumerate(rows, 1):
        b = L["b"]
        w.writerow([i, b["name"], b["domain"], status[L["kind"]], L["score"], ", ".join(L["reasons"]), L["price"],
                    b["email"], b["phone"], b["address"], b.get("rating") or "", b.get("reviews") or "",
                    ", ".join(b["sources"])])
    return buf.getvalue().encode("utf-8-sig")


def draft_text(L: dict) -> str:
    me = sender_info()
    b = L["b"]
    if L["kind"] == "site":
        a = p.Audit(**L["a"])
        subject, body = p.template_draft(a, me)
        to = b["email"] or (a.emails[0] if a.emails else "(email не найден — ищите на сайте или звоните)")
        return f"Кому: {to}\nТема: {subject}\n\n{body}"
    who = f"Меня зовут {me['name']}{', ' + me['company'] if me.get('company') else ''}, я делаю сайты."
    if L["kind"] == "no_site":
        problem = (f"Нашёл «{b['name']}» на карте, но сайта у вас нет. Когда человек ищет услугу с телефона, "
                   "он чаще звонит тем, у кого можно сразу посмотреть цены, адрес и отзывы.")
        offer = ("Могу сделать простой сайт: что вы делаете, цены «от», адрес с картой и кнопка «Позвонить». "
                 "Покажу, как это будет выглядеть, бесплатно.")
    else:
        problem = f"У «{b['name']}» указан сайт {b['domain']}, но он сейчас не открывается — клиенты, которые его ищут, видят ошибку."
        offer = "Могу восстановить его или сделать новый простой сайт с ценами, адресом и кнопкой звонка."
    to = b["email"] or (f"нет email — напишите в WhatsApp/Telegram или позвоните: {b['phone']}" if b["phone"] else "контактов нет")
    return (f"Кому: {to}\nТема: Сайт для «{b['name']}»\n\nЗдравствуйте!\n\n{who} {problem}\n\n{offer}\n\n"
            f"Если не актуально — просто ответьте «нет», больше писать не буду.\n\n{me['signature']}")


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

    def lead_by_arg(self, arg: str) -> dict | None:
        arg = arg.strip()
        if arg.isdigit():
            i = int(arg) - 1
            return self.st["last"][i] if 0 <= i < len(self.st["last"]) else None
        d = p.normalize(arg) if arg else None
        return next((L for L in self.st["last"] if d and L["b"]["domain"] == d), None)

    def handle(self, chat: int, user: int, text: str):
        if self.st["owner"] is None:  # первый, кто написал, становится владельцем; остальным бот не отвечает
            self.st["owner"] = user
            save_state(self.st)
        if user != self.st["owner"]:
            return
        cmd, _, arg = text.partition(" ")
        if cmd in ("/start", "/help"):
            return self.say(chat, HELP)
        if cmd == "/niches":
            niches = leads.load_niches()
            by_tier = {"high": [], "mid": [], "low": []}
            for name, d in niches.items():
                by_tier.get(d.get("tier", "mid"), by_tier["mid"]).append(name)
            return self.say(chat, "Высокий чек: " + ", ".join(by_tier["high"]) + "\n\nСредний: " + ", ".join(by_tier["mid"])
                            + "\n\nНебольшой: " + ", ".join(by_tier["low"]) + "\n\nСвоя ниша: /addniche название: слово1, слово2")
        if cmd == "/addniche":
            body, _, tier = arg.partition("|")
            name, _, words = body.partition(":")
            kws = [w.strip() for w in words.split(",") if len(w.strip()) >= 3]
            if not name.strip() or not kws:
                return self.say(chat, "Формат: /addniche натяжные потолки: потолк, натяжн | high\n"
                                      "Слова — части названий компаний (не короче 3 букв). Чек (low/mid/high) — по желанию.")
            e = leads.add_custom_niche(name.strip(), kws, tier.strip() or "mid")
            return self.say(chat, f"Добавил нишу «{name.strip().lower()}»: ищу компании, в названии которых есть "
                                  f"{', '.join(kws)}. Чек: {e['tier']}.\nПример: {name.strip().lower()} Казань")
        if cmd == "/sources":
            keys = leads.api_keys()
            return self.say(chat, "OpenStreetMap — подключён (бесплатно)\n"
                                  f"Яндекс — {'подключён' if keys['yandex'] else 'нет ключа (keys.json → yandex_org_api_key)'}\n"
                                  f"2ГИС — {'подключён' if keys['2gis'] else 'нет ключа (keys.json → dgis_api_key)'}")
        if cmd in ("/draft", "/skip"):
            L = self.lead_by_arg(arg)
            if not L:
                return self.say(chat, f"Укажите номер или домен из последнего поиска, например: {cmd} 3")
            if cmd == "/draft":
                return self.say(chat, draft_text(L))
            self.st["skip"].append(leads.business_key(L["b"]))
            save_state(self.st)
            return self.say(chat, f"«{L['b']['name']}» больше не покажу.")
        req = leads.parse_request(text, leads.load_niches())
        if not req:
            return self.say(chat, "Не понял нишу. Список: /niches, своя: /addniche\n\n" + HELP)
        if not self.busy.acquire(blocking=False):
            return self.say(chat, "Ещё ищу по прошлому запросу, подождите.")
        threading.Thread(target=self.search, args=(chat, *req), daemon=True).start()

    def search(self, chat: int, niche_name: str, city: str):
        try:
            niche = leads.load_niches()[niche_name]
            self.say(chat, f"Ищу «{niche_name}» в городе {city}…")
            try:
                found, notes = leads.find_businesses(niche_name, niche, city)
            except LookupError as e:
                return self.say(chat, str(e))
            except leads.MapError as e:
                return self.say(chat, f"Источники не ответили.\n{e}\n\nПопробуйте через пару минут.")
            skip = set(self.st["skip"]) | set(self.st["seen"]) | p.load_suppression(HERE / "do_not_contact.txt")
            fresh = [b for b in found if leads.keep(b, skip)][:MAX_LEADS]
            if not fresh:
                return self.say(chat, f"Новых компаний не нашёл ({'; '.join(notes)}). Попробуйте другую нишу или город.")
            n_site = sum(1 for b in fresh if b["site"])
            self.say(chat, f"Нашёл {len(fresh)} новых компаний ({'; '.join(notes)}): {n_site} с сайтом, "
                           f"{len(fresh) - n_site} без. Проверяю сайты, это пара минут.")
            audits = audit_sites(fresh)
            rows = build_leads(fresh, audits, niche.get("tier", "mid"), city)
            self.st["seen"] += [leads.business_key(L["b"]) for L in rows]
            top = [L for L in rows if L["score"] >= MIN_SCORE][:SHOW_TOP]
            self.st["last"] = rows
            save_state(self.st)
            if not top:
                self.say(chat, f"Оценил {len(rows)} компаний — подходящих мало. Полный список в файле.")
            else:
                text = f"Лучшие кандидаты ({len(top)} из {len(rows)}), по приоритету:\n\n"
                text += "\n\n".join(format_lead(i, L) for i, L in enumerate(top, 1))
                text += ("\n\nЦены — ориентир по правилам из pricing.json, решение за вами."
                         "\nЧерновик: /draft номер · не показывать: /skip номер")
                self.say(chat, text)
            self.send_file(chat, f"{niche_name}-{city}.csv".replace(" ", "_"), csv_bytes(rows),
                           "Все компании с приоритетом и рекомендацией (открывается в Excel)")
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
