#!/usr/bin/env python3
"""Telegram-бот: поиск компаний без сайта или со слабым сайтом → карточка с кнопками → демо и письмо только по кнопке.

Порядок работы с кандидатом:
  1. карточка: кто, почему, сколько брать — кнопки «✅ Сделать демо» / «❌ Пропустить»;
  2. после «Сделать демо» бот собирает демо по эталону и присылает ссылку и текст письма —
     кнопки «📨 Отправить» / «✏️ Изменить» / «❌ Отмена»;
  3. «Отправить» — письмо уходит с вашего ящика (лимит в день, пауза между письмами);
  4. ответы клиентов приходят вам в Telegram; «нет» → компания в списке «не писать»;
     много недоставленных писем → рассылка на паузе.

Источники компаний — leads.py, демо — demo_builder.py, почта — mailer.py. Настройки сервера — config.json.
Запуск: токен от @BotFather — в token.txt (или TELEGRAM_BOT_TOKEN), затем `python bot.py` (на Windows — start.bat).
"""
from __future__ import annotations

import csv
import functools
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import demo_builder
import demo_pdf
import leads
import mailer
import prospector as p

HERE = Path(__file__).resolve().parent
STATE = HERE / "bot_state.json"
CONFIG = HERE / "config.json"
MAX_SITES = 40      # сколько сайтов проверять за один поиск
MAX_LEADS = 150     # сколько компаний рассматривать за один поиск
SHOW_TOP = 10       # сколько карточек присылать за один поиск
MIN_SCORE = 35      # ниже этого приоритета компания не показывается (но есть в отчёте)
KEEP_LEADS = 3000   # сколько карточек помнить (кнопки старых карточек после этого не работают)
ALL = ("все", "all", "*")  # schedule → cities / niches: "все" — все города из cities.json / все ниши

HELP = """Напишите нишу и город, например:
кузовной ремонт Омск
стоматология Казань

Бот пришлёт карточки лучших кандидатов. Под каждой кнопки:
✅ Сделать демо — бот соберёт демо-сайт по эталону и подготовит письмо
❌ Пропустить — больше не показывать
Потом: 📨 Отправить / ✏️ Изменить текст / ❌ Отмена.

Команды:
/status — сколько писем отправлено сегодня, пауза, расписание
/scan — запустить обход ниш и городов сейчас, не дожидаясь расписания (те же лимиты)
/pause и /resume — остановить и возобновить отправку
/testmail — пробное письмо на ваш ящик (проверка почты)
/draft 3 — черновик письма для кандидата №3 из последнего поиска
/niches — список ниш · /addniche натяжные потолки: потолк, натяжн | high — своя ниша
/sources — какие источники подключены
/help — эта справка"""


# ───────────────────────────── настройки и состояние ─────────────────────────────
def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    return {}


def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    defaults = {"owner": None, "owner_chat": None, "offset": 0, "seen": [], "skip": [], "last": [], "leads": {},
                "sent_today": {}, "bounces_today": {}, "paused": False, "pending_edit": None, "pending_mail": None, "last_auto": "",
                "mail_seen": [], "last_send_ts": 0}
    for k, v in defaults.items():
        st.setdefault(k, v)
    if isinstance(st["last"], dict) or (st["last"] and isinstance(st["last"][0], dict)):  # формат старых версий
        st["last"] = []
    return st


def seen_path() -> Path:
    return HERE / "seen.txt"


def load_seen(st: dict) -> set[str]:
    """Признаки уже показанных компаний. Хранятся отдельным файлом, по строке на признак: за месяцы работы их
    становятся сотни тысяч, а bot_state.json перезаписывается после каждого сообщения. Старый формат (список
    в bot_state.json) переносится в файл."""
    path = seen_path()
    seen = set()
    if path.exists():
        seen = {x for x in path.read_text(encoding="utf-8").splitlines() if x}
    old = [k for k in st.get("seen") or [] if k not in seen]
    if old:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(k + "\n" for k in old))
        seen.update(old)
    st["seen"] = []
    return seen


def save_state(st: dict):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def lead_id(b: dict) -> str:
    return hashlib.sha1(leads.business_key(b).encode()).hexdigest()[:8]


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
        if b.get("domain") and a and not a.ok and b.get("site_found") == "по телефону на сайте":
            continue  # угаданный домен уже не открывается — не считаем это сайтом компании
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
    if L["kind"] == "no_site" and not leads.no_site_confirmed(b):
        head += " · 🚫 сайта нет в OpenStreetMap"
    elif L["kind"] != "site":
        head += f" · {KIND_ICON[L['kind']]}"
    lines = [head, f"   приоритет {L['score']}/100: {', '.join(L['reasons']) or 'мелкие недочёты'}",
             f"   💰 {L['price']}"]
    if b.get("site_found"):
        lines.append(f"   🔗 сайта не было в OpenStreetMap, нашёл: {b['site_found']}")
    contacts = []
    if b["email"]:
        contacts.append(b["email"])
    ph = leads.phones(b["phone"])
    if ph:
        contacts.append("тел. " + ph[0]["text"] + (" (мобильный)" if ph[0]["mobile"] else ""))
    lines.append("   ✉ " + (", ".join(contacts) or "контактов не нашёл"))
    if b.get("rating") is not None:
        lines.append(f"   ★ {b['rating']} · {b.get('reviews') or 0} отзывов ({', '.join(b['sources'])})")
    if L["kind"] == "no_site" and not leads.no_site_confirmed(b):
        lines.append("   🔎 проверьте, нет ли сайта: " + search_link(b, L.get("city", "")))
    return "\n".join(lines)


def search_link(b: dict, city: str) -> str:
    return "https://yandex.ru/search/?text=" + urllib.parse.quote(f"{b['name']} {city}".strip())


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


LETTER = HERE / "letter.txt"
MOBILE_ISSUES = {"no_viewport", "flash", "old_markup"}
SECURE_ISSUES = {"no_https", "bad_cert", "mixed_content", "cert_expiring"}


def load_letter(path: Path | None = None) -> dict[str, str]:
    """letter.txt → {"письмо": …, "проблема: нет сайта": …, "демо: pdf": …}. Строки с # — пояснения."""
    parts, key, buf = {}, None, []
    for line in (path or LETTER).read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("#"):
            continue
        if line.startswith("===") and line.rstrip().endswith("==="):
            if key:
                parts[key] = "\n".join(buf).strip()
            key, buf = line.strip("= ").strip(), []
        elif key:
            buf.append(line)
    if key:
        parts[key] = "\n".join(buf).strip()
    return parts


def problem_key(L: dict) -> str:
    if L["kind"] == "no_site":
        return "проблема: нет сайта"
    if L["kind"] == "dead_site":
        return "проблема: сайт не работает"
    issues = set((L.get("a") or {}).get("issues") or {})
    if issues & MOBILE_ISSUES:
        return "проблема: неудобно с телефона"
    if issues & SECURE_ISSUES:
        return "проблема: не защищено"
    return "проблема: другое"


def make_draft(L: dict, demo_url: str | None = None, pdf: bool = False) -> dict:
    """Письмо по шаблону letter.txt: {"to", "subject", "body"}. to пустой, если email нет."""
    me = sender_info()
    b = L["b"]
    t = load_letter()
    demo_key = ("демо: pdf и ссылка" if pdf and demo_url else "демо: pdf" if pdf
                else "демо: ссылка" if demo_url else "демо: нет")
    niche = leads.load_niches().get(L.get("niche", ""), {})
    values = {
        "{проблема}": t.get(problem_key(L), t.get("проблема: другое", "")),
        "{демо}": t.get(demo_key, ""),
        "{ссылка}": demo_url or "",
        "{компания}": b["name"],
        "{моё_имя}": me.get("name", ""),
        "{что_ищут}": niche.get("search_phrase", "такие услуги"),
        "{подпись}": me.get("signature", ""),
    }
    text = t["письмо"]
    for _ in range(2):  # второй проход — подстановки внутри {проблема} и {демо}
        for k, v in values.items():
            text = text.replace(k, v)
    first, _, rest = text.partition("\n")
    if first.lower().startswith("тема:"):
        subject, body = first.split(":", 1)[1].strip(), rest.strip()
    else:
        subject, body = f"Сайт для «{b['name']}»", text.strip()
    to = b["email"]
    if not to and L.get("a"):
        to = (L["a"].get("emails") or [""])[0]
    return {"to": to, "subject": subject, "body": body}


LETTER_DEFAULTS = {  # если в letter.txt на сервере ещё нет этих частей (его правили до обновления)
    "whatsapp": "Здравствуйте! Меня зовут {моё_имя}, я делаю сайты. Нашёл «{компания}» на карте и заметил, что "
                "{проблема}. {демо_коротко} Если интересно, расскажу подробнее.",
    "звонок": "Здравствуйте! Меня зовут {моё_имя}, я делаю сайты. Нашёл «{компания}» на карте и заметил, что "
              "{проблема}. Я подготовил, как мог бы выглядеть ваш сайт. Можно прислать вам на почту или в WhatsApp? "
              "Подскажите адрес.",
    "демо коротко: ссылка": "Сделал пример, как может выглядеть ваш сайт: {ссылка}",
    "демо коротко: pdf": "Сделал пример, как может выглядеть ваш сайт, — пришлю следующим сообщением.",
    "демо коротко: нет": "Могу бесплатно сделать пример, как может выглядеть ваш сайт.",
}


def short_text(L: dict, kind: str, demo_url: str | None = None, pdf: bool = False) -> str:
    """Короткий текст для WhatsApp (kind="whatsapp") или подсказка для звонка (kind="звонок")."""
    t = {**LETTER_DEFAULTS, **load_letter()}
    demo = t["демо коротко: ссылка"] if demo_url else t["демо коротко: pdf"] if pdf else t["демо коротко: нет"]
    text = t[kind]
    for k, v in (("{демо_коротко}", demo), ("{проблема}", t.get(problem_key(L), "")), ("{ссылка}", demo_url or ""),
                 ("{компания}", L["b"]["name"]), ("{моё_имя}", sender_info().get("name", ""))):
        text = text.replace(k, v)
    return text.strip()


def no_email_help(L: dict, demo_url: str | None = None, pdf: bool = False) -> tuple[str, list[tuple[str, str]]]:
    """Что делать, если email нет: текст для WhatsApp или для звонка и кнопки (WhatsApp, ввести почту)."""
    ph = leads.phones(L["b"]["phone"])
    buttons = [("✉️ Ввести почту", f"mail:{L['id']}")]
    if not ph:
        return "Email и телефона нет — написать некуда.", buttons
    nums = ", ".join(x["text"] for x in ph[:3])
    mobile = next((x for x in ph if x["mobile"]), None)
    if mobile:
        buttons.insert(0, ("💬 WhatsApp", f"https://wa.me/{mobile['intl']}"))
        head = (f"Email нет. Мобильный {mobile['text']} — нажмите «💬 WhatsApp» и вставьте текст "
                f"(если у них нет WhatsApp, позвоните: {nums}):")
        return f"{head}\n\n{short_text(L, 'whatsapp', demo_url, pdf)}", buttons
    head = f"Email нет, мобильного тоже. Позвоните: {nums}. Что сказать:"
    tail = "\n\nЕсли дадут почту — нажмите «✉️ Ввести почту», и письмо уйдёт туда."
    return f"{head}\n\n{short_text(L, 'звонок', demo_url, pdf)}{tail}", buttons


def draft_text(L: dict, demo_url: str | None = None) -> str:
    d = make_draft(L, demo_url)
    to = d["to"] or "нет email (текст для WhatsApp или звонка — ниже)"
    text = f"Кому: {to}\nТема: {d['subject']}\n\n{d['body']}"
    if not d["to"]:
        text += "\n\n" + no_email_help(L, demo_url)[0]
    return text


def spawn(target, *args):
    """Фоновая задача (в тестах подменяется на синхронный вызов)."""
    threading.Thread(target=target, args=args, daemon=True).start()


def kb(*rows: list[tuple[str, str]]) -> str:
    """Кнопки: (текст, действие). Действие, начинающееся с https://, — кнопка-ссылка."""
    return json.dumps({"inline_keyboard": [[{"text": t, "url": c} if c.startswith("https://") else
                                            {"text": t, "callback_data": c} for t, c in row] for row in rows]},
                      ensure_ascii=False)


# ───────────────────────────── сервер демо-страниц ─────────────────────────────
class DemoHandler(SimpleHTTPRequestHandler):
    """Отдаёт демо, запрещает индексацию и не показывает список папок."""

    def end_headers(self):
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, *args):
        pass


def start_demo_server(root: Path, port: int) -> ThreadingHTTPServer:
    root.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", port), functools.partial(DemoHandler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ───────────────────────────── Telegram ─────────────────────────────
class Bot:
    last_error = ""

    def __init__(self, token: str, config: dict | None = None):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.st = load_state()
        self.seen = load_seen(self.st)
        self.cfg = config if config is not None else load_config()
        self.busy = threading.Lock()   # один поиск за раз: карты и сайты не любят параллельных запросов
        self.current = ""              # что ищется сейчас: «ниша город»
        self.current_auto = False      # True — это обход по расписанию или /scan, а не ваш запрос
        self.pending = None            # ваш запрос, пришедший во время другого поиска: (chat, ниша, город)
        self.sched_lock = threading.Lock()  # обход по расписанию и /scan не идут одновременно
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        demo = self.cfg.get("demo", {})
        self.demo_root = HERE / demo.get("dir", "public_demos")
        self.demo_url = demo.get("public_url", "").rstrip("/")
        leads.set_api_limits(self.cfg.get("limits", {}).get("api_per_day"))

    # — Telegram API —
    def call(self, method: str, timeout: int = 60, **params):
        """Запрос к Telegram. «Слишком часто» (429) — ждём, сколько просит Telegram; сбой сети или 5xx — повторяем:
        за день бот присылает десятки карточек, и одна заминка не должна обрывать поиск."""
        for attempt in range(5):
            try:
                r = requests.post(self.api + method, data=params, timeout=timeout)
            except requests.RequestException:
                if attempt == 4:
                    raise
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 4:
                    r.raise_for_status()
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 0))
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 3 * (attempt + 1)), 120))
                continue
            r.raise_for_status()
            return r.json().get("result")

    def say(self, chat: int, text: str, markup: str | None = None):
        while text:  # лимит Telegram — 4096 символов
            if len(text) <= 4000:
                cut = text
            else:
                idx = text.rfind("\n\n", 0, 4000)
                cut = text[:idx if idx > 0 else 4000]
            text = text[len(cut):].lstrip()
            extra = {"reply_markup": markup} if markup and not text else {}
            self.call("sendMessage", chat_id=chat, text=cut, disable_web_page_preview="true", **extra)

    def send_file(self, chat: int, name: str, content: bytes, caption: str = ""):
        requests.post(self.api + "sendDocument", data={"chat_id": chat, "caption": caption},
                      files={"document": (name, content)}, timeout=60).raise_for_status()

    def save(self):
        with self.lock:
            save_state(self.st)

    def requests_updates(self):
        r = requests.post(self.api + "getUpdates", data={"offset": self.st["offset"], "timeout": 50}, timeout=70)
        r.raise_for_status()
        return r.json().get("result")

    def run(self):
        if self.cfg.get("demo", {}).get("port") and self.demo_url:  # без public_url демо наружу не открываем
            start_demo_server(self.demo_root, int(self.cfg["demo"]["port"]))
            print(f"Демо-страницы: {self.demo_url or 'порт ' + str(self.cfg['demo']['port'])}")
        if self.cfg.get("imap"):
            threading.Thread(target=self.inbox_loop, daemon=True).start()
        if self.schedule_queries():
            threading.Thread(target=self.schedule_loop, daemon=True).start()
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
                self.save()
                try:
                    if "callback_query" in u:
                        self.handle_callback(u["callback_query"])
                    elif "text" in (u.get("message") or {}):
                        m = u["message"]
                        self.handle(m["chat"]["id"], m["from"]["id"], m["text"].strip())
                except Exception as e:  # noqa: BLE001
                    print(f"Ошибка обработки: {e!r}")

    # — текстовые сообщения —
    def lead_by_arg(self, arg: str) -> dict | None:
        arg = arg.strip()
        ids = self.st["last"]
        if arg.isdigit():
            i = int(arg) - 1
            return self.st["leads"].get(ids[i]) if 0 <= i < len(ids) else None
        d = p.normalize(arg) if arg else None
        return next((self.st["leads"][i] for i in ids if d and self.st["leads"][i]["b"]["domain"] == d), None)

    def handle(self, chat: int, user: int, text: str):
        if self.st["owner"] is None:  # первый, кто написал, становится владельцем; остальным бот не отвечает
            self.st["owner"] = user
        if user != self.st["owner"]:
            return
        self.st["owner_chat"] = chat
        self.save()
        cmd, _, arg = text.partition(" ")
        if self.st["pending_mail"] and not text.startswith("/"):
            return self.apply_mail(chat, text)
        if self.st["pending_edit"] and not text.startswith("/"):
            return self.apply_edit(chat, text)
        if cmd in ("/start", "/help"):
            return self.say(chat, HELP)
        if cmd == "/scan":
            return spawn(self.scan_now, chat)
        if cmd == "/status":
            return self.say(chat, self.status_text())
        if cmd == "/testmail":
            return spawn(self.test_mail, chat)
        if cmd in ("/pause", "/resume"):
            self.st["paused"] = cmd == "/pause"
            self.save()
            return self.say(chat, "Отправка писем на паузе." if self.st["paused"] else "Отправка писем возобновлена.")
        if cmd == "/niches":
            niches = leads.load_niches()
            by_tier = {"high": [], "mid": [], "low": []}
            for name, d in niches.items():
                mark = "" if d.get("schedule", True) else " ⏸"
                by_tier.get(d.get("tier", "mid"), by_tier["mid"]).append(name + mark)
            return self.say(chat, f"Ниш: {len(niches)}. Демо бот делает для любой. ⏸ — ниша не входит в ежедневный "
                            "поиск (её можно искать вручную).\n\nВысокий чек: "
                            + ", ".join(by_tier["high"]) + "\n\nСредний: " + ", ".join(by_tier["mid"])
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
        if cmd == "/draft":
            L = self.lead_by_arg(arg)
            if not L:
                return self.say(chat, "Укажите номер из последнего поиска, например: /draft 3")
            return self.say(chat, draft_text(L, self.demo_link(L)))
        req = leads.parse_request(text, leads.load_niches())
        if not req:
            return self.say(chat, "Не понял нишу. Список: /niches, своя: /addniche\n\n" + HELP)
        with self.lock:
            replaced = self.pending
            free = self.busy.acquire(blocking=False)
            self.pending = None if free else (chat, *req)  # последний запрос заменяет прежний ожидающий
        if free:
            return spawn(self.search, chat, *req)
        what = " ".join(req)
        now = (f"Сейчас идёт обход ниш и городов (ищу «{self.current}»)" if self.current_auto
               else f"Сейчас ищу «{self.current}»" if self.current else "Сейчас идёт другой поиск")
        if self.current_auto:
            tail = (f"Ваш запрос «{what}» начну сразу после этого шага — обход подождёт. Карточки с пометкой 📍 "
                    "приходят из обхода, а не по вашему запросу.")
        else:
            tail = f"Ваш запрос «{what}» начну сразу после него."
        if replaced and tuple(replaced[1:]) != tuple(req):
            tail += f" Прежний ожидавший запрос «{' '.join(replaced[1:])}» отменил."
        self.say(chat, f"{now}. {tail}")

    def test_mail(self, chat: int):
        """Пробное письмо на свой же ящик: проверка логина, пароля и адреса SMTP без писем клиентам."""
        smtp = self.cfg.get("smtp")
        if not smtp:
            return self.say(chat, "Почта не настроена: в config.json нет раздела smtp.")
        to = smtp.get("from") or smtp["user"]
        try:
            mailer.send(smtp, to, "Проверка бота site-prospector", "Если вы видите это письмо, отправка работает.")
        except Exception as e:  # noqa: BLE001
            return self.say(chat, f"❌ Письмо не ушло: {type(e).__name__}: {e}\n\nЧастые причины: обычный пароль вместо "
                                  "«пароля приложения», неверный host/port, в ящике не разрешены почтовые программы.")
        self.say(chat, f"✅ Пробное письмо отправлено на {to}. Проверьте, что оно пришло во «Входящие», а не в «Спам».")

    def status_text(self) -> str:
        today = self.today()
        lim = self.cfg.get("limits", {})
        sched = self.cfg.get("schedule", {})
        statuses = [L.get("status", "new") for L in self.st["leads"].values()]
        return (f"Сегодня отправлено: {self.st['sent_today'].get(today, 0)} из {lim.get('per_day', 20)}\n"
                f"Недоставленных сегодня: {self.st['bounces_today'].get(today, 0)}\n"
                f"Отправка: {'⏸ на паузе (/resume)' if self.st['paused'] else '▶️ работает'}\n"
                f"Почта: {'настроена' if self.cfg.get('smtp') else 'не настроена (config.json → smtp)'}\n"
                f"PDF с демо: {'работает' if demo_pdf.available() else 'нет браузера на сервере'}\n"
                f"Ссылки на демо: {self.demo_url if self.demo_link({'demo': 'x'}) else 'выключены (нужен домен; демо уходят в PDF)'}\n"
                f"{self.schedule_status()}\n"
                f"Кандидаты: отправлено {statuses.count('sent')}, ответили {statuses.count('replied')}, "
                f"отказались {statuses.count('refused')}")

    def schedule_status(self) -> str:
        qs = self.schedule_queries()
        if not qs:
            return "Расписание: нет"
        sched = self.cfg.get("schedule", {})
        pos = self.st.get("sched_pos", 0) % len(qs)
        per_day = int(sched.get("max_searches", 40))
        return (f"Расписание: {sched.get('time', '10:00')}, {len(qs)} запросов, следующий — «{qs[pos]}» "
                f"({pos + 1}-й). {len(self.schedule_niches())} ниш × {len(self.schedule_cities())} городов; "
                f"до {per_day} запросов и {sched.get('max_cards', 20)} карточек в день — полный круг не меньше чем "
                f"за {-(-len(qs) // max(per_day, 1))} дн.\n{leads.api_usage_text()}")

    def today(self) -> str:
        tz = ZoneInfo(self.cfg.get("schedule", {}).get("tz", "Europe/Moscow"))
        return datetime.now(tz).strftime("%Y-%m-%d")

    # — поиск —
    def search(self, chat: int, niche_name: str, city: str, limit: int = SHOW_TOP, quiet: bool = False) -> int:
        """Ищет и присылает карточки. Возвращает число присланных карточек.
        quiet=True — поиск по расписанию: только карточки, без промежуточных сообщений и CSV (их за день десятки).
        Если источники не ответили, self.last_error — текст ошибки (иначе пусто)."""
        self.last_error = ""
        self.current, self.current_auto = f"{niche_name} {city}", quiet
        try:
            niche = leads.load_niches()[niche_name]
            if not quiet:
                self.say(chat, f"Ищу «{niche_name}» в городе {city}…")
            try:
                found, notes = leads.find_businesses(niche_name, niche, city)
            except LookupError as e:
                if not quiet:
                    self.say(chat, str(e))
                return 0
            except leads.MapError as e:
                self.last_error = str(e)
                if quiet:
                    return 0
                keys = leads.api_keys()
                tip = "" if keys["2gis"] or keys["yandex"] else (
                    "\n\nБесплатные серверы OpenStreetMap иногда перегружены. Надёжнее подключить ключ 2ГИС или Яндекса "
                    "(см. /sources) — тогда бот будет работать и без OpenStreetMap.")
                self.say(chat, f"Источники не ответили.\n{e}\n\nПопробуйте через 5–10 минут.{tip}")
                return 0
            skip = set(self.st["skip"]) | self.seen | p.load_suppression(HERE / "do_not_contact.txt")
            fresh = [b for b in found if leads.keep(b, skip)][:MAX_LEADS]
            if not fresh:
                if not quiet:
                    self.say(chat, f"Новых компаний не нашёл ({'; '.join(notes)}). Попробуйте другую нишу или город.")
                return 0
            n_site = sum(1 for b in fresh if b["site"])
            if not quiet:
                self.say(chat, f"Нашёл {len(fresh)} новых компаний ({'; '.join(notes)}): {n_site} с сайтом, "
                               f"{len(fresh) - n_site} без. Перепроверяю тех, у кого нет сайта, и проверяю сайты — "
                               "это пара минут.")
            # «нет сайта» только по OpenStreetMap — часто просто незаполненное поле: ищем сайт в других местах
            fresh = [b for b in leads.verify_no_site(fresh, city) if leads.keep(b, skip)]
            audits = audit_sites(fresh)
            rows = build_leads(fresh, audits, niche.get("tier", "mid"), city)
            for L in rows:
                L.update(id=lead_id(L["b"]), niche=niche_name, city=city, status="new")
            top = [L for L in rows if L["score"] >= MIN_SCORE][:limit]
            # Запоминаем показанные компании и слабых кандидатов (ниже порога) — по всем признакам: домен, телефоны,
            # почта. В другой день компания может прийти из другого источника уже с сайтом или другим первым
            # телефоном — и всё равно не повторится. Хороших кандидатов, которым сегодня не хватило места
            # (дневной лимит карточек), не запоминаем: они придут при следующем поиске этой ниши в этом городе.
            shown = {L["id"] for L in top}
            remember = [L for L in rows if L["id"] in shown or L["score"] < MIN_SCORE]
            with self.lock:
                for L in top:
                    self.st["leads"][L["id"]] = L
                self.st["last"] = [L["id"] for L in top]
                new_keys = [k for k in dict.fromkeys(k for L in remember for k in leads.business_keys(L["b"]))
                            if k not in self.seen]
                if new_keys:
                    with open(seen_path(), "a", encoding="utf-8") as f:
                        f.write("".join(k + "\n" for k in new_keys))
                    self.seen.update(new_keys)
                self.trim_leads()
            self.save()
            if not top and not quiet:
                self.say(chat, f"Оценил {len(rows)} компаний — подходящих мало. Полный список в файле.")
            for n, L in enumerate(top, 1):
                buttons = [("✅ Сделать демо", f"demo:{L['id']}"), ("❌ Пропустить", f"skip:{L['id']}")]
                self.say(chat, format_lead(n, L) + (f"\n   📍 из обхода: {niche_name} · {city}" if quiet else ""), kb(buttons))
            if not quiet:
                self.send_file(chat, f"{niche_name}-{city}.csv".replace(" ", "_"), csv_bytes(rows),
                               "Все компании с приоритетом и рекомендацией (открывается в Excel)")
            return len(top)
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка поиска: {e!r}")
            self.last_error = f"{type(e).__name__}"
            if not quiet:
                self.say(chat, f"Что-то сломалось: {type(e).__name__}. Подробности — в окне, где запущен бот.")
            return 0
        finally:
            self.current, self.current_auto = "", False
            if self.busy.locked():
                self.busy.release()
            self.run_pending()

    def run_pending(self):
        """Запустить запрос, который вы прислали во время другого поиска. Ваши запросы идут раньше обхода."""
        with self.lock:
            req = self.pending
            if not req or not self.busy.acquire(blocking=False):
                return
            self.pending = None
        chat, niche_name, city = req
        spawn(self.search, chat, niche_name, city)

    def acquire_for_batch(self):
        """Обход по расписанию и /scan занимают поиск, только когда нет ваших ожидающих запросов."""
        while True:
            with self.lock:
                if self.pending is None and self.busy.acquire(blocking=False):
                    return
            time.sleep(1)

    def trim_leads(self):
        """Не раздувать bot_state.json: забываем самые старые карточки, которые уже ничего не ждут.
        Отправленные и те, кому ответили, держим дольше — по ним бот узнаёт ответы клиентов."""
        leads_ = self.st["leads"]
        if len(leads_) <= KEEP_LEADS:
            return
        idle = [i for i, L in leads_.items() if L.get("status") in ("new", "skipped", "cancelled", None)]
        for i in idle[:len(leads_) - KEEP_LEADS]:
            leads_.pop(i)
        if len(leads_) > 2 * KEEP_LEADS:
            for i in list(leads_)[:len(leads_) - 2 * KEEP_LEADS]:
                leads_.pop(i)

    # — кнопки —
    def handle_callback(self, cq: dict):
        user = cq["from"]["id"]
        if user != self.st["owner"]:
            return
        chat = cq["message"]["chat"]["id"]
        action, _, i = cq.get("data", "").partition(":")
        try:
            self.call("answerCallbackQuery", callback_query_id=cq["id"])
            self.call("editMessageReplyMarkup", chat_id=chat, message_id=cq["message"]["message_id"],
                      reply_markup=json.dumps({"inline_keyboard": []}))
        except requests.RequestException:
            pass
        L = self.st["leads"].get(i)
        if not L:
            return self.say(chat, "Этого кандидата уже нет в памяти бота. Повторите поиск.")
        if action == "skip":
            self.st["skip"] += [k for k in leads.business_keys(L["b"]) if k not in self.st["skip"]]
            L["status"] = "skipped"
            self.save()
            return self.say(chat, f"«{L['b']['name']}» больше не покажу.")
        if action == "demo":
            return spawn(self.make_demo, chat, L)
        if action == "send":
            return spawn(self.send_lead, chat, L)
        if action == "edit":
            self.st["pending_edit"] = i
            self.save()
            return self.say(chat, "Пришлите новый текст письма одним сообщением. Если первая строка начинается с "
                                  "«Тема:», она станет темой письма.")
        if action == "mail":
            self.st["pending_mail"] = i
            self.st["pending_edit"] = None
            self.save()
            return self.say(chat, f"Пришлите email «{L['b']['name']}» одним сообщением (например, узнали по телефону).")
        if action == "cancel":
            L["status"] = "cancelled"
            self.save()
            return self.say(chat, f"Отменил. «{L['b']['name']}» не трогаю.")

    def demo_link(self, L: dict) -> str | None:
        if not (L.get("demo") and self.demo_url and self.cfg.get("demo", {}).get("link", True)):
            return None
        return f"{self.demo_url}/{L['demo']}/"

    def pdf_path(self, L: dict) -> Path | None:
        path = HERE / "demo_pdfs" / f"{L['demo']}.pdf" if L.get("demo") else None
        return path if path and path.exists() else None

    @staticmethod
    def pdf_name(L: dict) -> str:
        safe = "".join(ch for ch in L["b"]["name"] if ch.isalnum() or ch in " -_")[:40].strip().replace(" ", "-")
        return f"Сайт-{safe or 'демо'}.pdf"

    def make_demo(self, chat: int, L: dict):
        niche = leads.load_niches().get(L.get("niche", ""), {})
        demo_cfg = self.cfg.get("demo", {})
        want_pdf = demo_cfg.get("pdf", True) and demo_pdf.available()
        note = ""
        if demo_cfg.get("pdf", True) and not want_pdf:
            note = "PDF выключен: на сервере не установлен браузер (мало места на диске — см. журнал установки).\n\n"
        # демо собирается для любой ниши (у ниш без раздела demo — нейтральное, из названия)
        if not self.demo_url and not want_pdf:
            note += "Демо не показать: нет ни адреса демо-страниц (demo → public_url), ни PDF. Письмо — без демо.\n\n"
        elif not L.get("demo"):
            self.say(chat, f"Собираю демо для «{L['b']['name']}»" + (" и PDF…" if want_pdf else "…"))
            try:
                L["demo"] = demo_builder.build_demo(L["b"], L["niche"], niche, L["city"], sender_info(), self.demo_root,
                                                  demo_cfg.get("template", "studio"))
            except Exception as e:  # noqa: BLE001
                print(f"Ошибка сборки демо: {e!r}")
                note = f"Демо собрать не удалось ({type(e).__name__}), письмо — без него.\n\n"
        if L.get("demo") and want_pdf and not self.pdf_path(L):
            try:
                demo_pdf.render_pdf(self.demo_root, L["demo"], L["b"]["name"], HERE / "demo_pdfs" / f"{L['demo']}.pdf",
                                    demo_cfg.get("chromium_path"))
            except Exception as e:  # noqa: BLE001
                print(f"Ошибка PDF: {e!r}")
                note += (f"PDF собрать не удалось ({type(e).__name__}). Если это ModuleNotFoundError или ошибка браузера — "
                         "на сервере не установлен Chromium (см. README, раздел про PDF).\n\n")
        pdf = self.pdf_path(L)
        if pdf:
            self.send_file(chat, self.pdf_name(L), pdf.read_bytes(), "PDF, который уйдёт клиенту вместе с письмом")
        L["draft"] = make_draft(L, self.demo_link(L), pdf=bool(pdf))
        L["status"] = "draft"
        self.save()
        if self.cfg.get("one_click") and L["draft"]["to"]:
            return self.send_lead(chat, L)
        self.show_draft(chat, L, note)

    def show_draft(self, chat: int, L: dict, note: str = ""):
        d = L["draft"]
        link = self.demo_link(L)
        head = note + (f"🖼 Демо: {link}\nПроверьте его на телефоне перед отправкой.\n\n" if link else "")
        if self.pdf_path(L):
            head += "📎 К письму будет приложен PDF (он выше).\n\n"
        to = d["to"] or "нет email — ниже текст для WhatsApp или звонка"
        text = f"{head}Кому: {to}\nТема: {d['subject']}\n\n{d['body']}"
        buttons = [("✏️ Изменить", f"edit:{L['id']}"), ("❌ Отмена", f"cancel:{L['id']}")]
        if d["to"] and self.cfg.get("smtp"):
            buttons.insert(0, ("📨 Отправить", f"send:{L['id']}"))
        elif d["to"]:
            text += "\n\n(Почта для отправки не настроена — config.json → smtp. Пока отправьте вручную.)"
        self.say(chat, text, kb(buttons))
        if not d["to"]:
            help_text, extra = no_email_help(L, link, bool(self.pdf_path(L)))
            self.say(chat, help_text, kb(extra))

    def apply_edit(self, chat: int, text: str):
        L = self.st["leads"].get(self.st["pending_edit"])
        self.st["pending_edit"] = None
        if not L or not L.get("draft"):
            self.save()
            return self.say(chat, "Не нашёл, какое письмо править.")
        first, _, rest = text.partition("\n")
        if first.lower().startswith("тема:"):
            L["draft"]["subject"] = first.split(":", 1)[1].strip()
            text = rest.strip()
        L["draft"]["body"] = text
        self.save()
        self.show_draft(chat, L, "Текст обновил.\n\n")

    def apply_mail(self, chat: int, text: str):
        L = self.st["leads"].get(self.st["pending_mail"])
        self.st["pending_mail"] = None
        email = text.strip().lower()
        if not L or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zа-яё]{2,}", email):
            self.save()
            return self.say(chat, "Это не похоже на email. Нажмите «✉️ Ввести почту» ещё раз." if L else
                            "Не нашёл, к какой компании относится почта.")
        L["b"]["email"] = email
        if L.get("draft"):
            L["draft"]["to"] = email
        else:
            L["draft"] = make_draft(L, self.demo_link(L), pdf=bool(self.pdf_path(L)))
        self.save()
        self.show_draft(chat, L, f"Почту {email} записал.\n\n")

    # — отправка —
    def send_lead(self, chat: int, L: dict):
        d = L.get("draft") or {}
        smtp = self.cfg.get("smtp")
        lim = self.cfg.get("limits", {})
        today = self.today()
        problem = None
        if not smtp:
            problem = "почта не настроена (config.json → smtp)"
        elif not d.get("to"):
            problem = "у компании нет email"
        elif self.st["paused"]:
            problem = "отправка на паузе (/resume)"
        elif self.st["sent_today"].get(today, 0) >= lim.get("per_day", 20):
            problem = f"дневной лимит {lim.get('per_day', 20)} писем исчерпан, завтра можно продолжить"
        elif p.suppressed(d["to"], p.load_suppression(HERE / "do_not_contact.txt")) or L.get("status") == "sent":
            problem = "этот адрес в списке «не писать» или письмо уже отправлено"
        if problem:
            return self.say(chat, f"Не отправил «{L['b']['name']}»: {problem}.",
                            kb([("📨 Отправить", f"send:{L['id']}"), ("❌ Отмена", f"cancel:{L['id']}")])
                            if "лимит" in problem or "пауз" in problem else None)
        with self.send_lock:  # пауза между письмами, даже если нажать несколько кнопок подряд
            wait = self.st["last_send_ts"] + lim.get("min_delay_sec", 90) - time.time()
            if wait > 0:
                self.say(chat, f"Отправлю «{L['b']['name']}» через {int(wait)} с (пауза между письмами).")
                time.sleep(wait)
            try:
                pdf = self.pdf_path(L)
                mailer.send(smtp, d["to"], d["subject"], d["body"],
                            [(self.pdf_name(L), pdf.read_bytes())] if pdf else None)
            except Exception as e:  # noqa: BLE001
                print(f"Ошибка отправки: {e!r}")
                return self.say(chat, f"Письмо «{L['b']['name']}» не ушло: {type(e).__name__}: {e}",
                                kb([("📨 Попробовать ещё раз", f"send:{L['id']}")]))
            with self.lock:
                self.st["last_send_ts"] = time.time()
                self.st["sent_today"] = {today: self.st["sent_today"].get(today, 0) + 1}
                L.update(status="sent", sent_to=d["to"], sent_at=datetime.now().isoformat(timespec="minutes"))
            self.save()
        self.say(chat, f"📨 Отправлено: «{L['b']['name']}» → {d['to']}. "
                       f"Сегодня {self.st['sent_today'][today]} из {lim.get('per_day', 20)}.")

    # — входящие —
    def inbox_loop(self):
        while True:
            try:
                self.check_inbox()
            except Exception as e:  # noqa: BLE001
                print(f"Ошибка чтения почты: {e!r}")
            time.sleep(int(self.cfg.get("imap", {}).get("every_sec", 300)))

    def check_inbox(self):
        imap = dict(self.cfg["imap"])
        imap.setdefault("user", self.cfg.get("smtp", {}).get("user"))
        imap.setdefault("password", self.cfg.get("smtp", {}).get("password"))
        seen = set(self.st["mail_seen"])
        events = mailer.fetch_new(imap, seen)
        chat = self.st["owner_chat"]
        if not events:
            return
        today = self.today()
        by_email = {L.get("sent_to"): L for L in self.st["leads"].values() if L.get("sent_to")}
        me = (self.cfg.get("smtp", {}).get("from") or self.cfg.get("smtp", {}).get("user") or "").lower()
        for uid, ev in events:
            seen.add(uid)
            if ev.get("from") == me or ev["kind"] == "auto":
                continue  # свои письма (например, /testmail) и автоматические уведомления — не ответы клиентов
            if ev["kind"] == "bounce":
                with self.lock:
                    self.st["bounces_today"] = {today: self.st["bounces_today"].get(today, 0) + 1}
                L = by_email.get(ev.get("failed"))
                if L:
                    L["status"] = "bounced"
                self.say(chat, f"⚠️ Письмо не доставлено{': ' + ev['failed'] if ev.get('failed') else ''}.")
                max_b = self.cfg.get("limits", {}).get("max_bounces_per_day", 3)
                if self.st["bounces_today"][today] >= max_b and not self.st["paused"]:
                    self.st["paused"] = True
                    self.say(chat, f"⏸ Сегодня уже {self.st['bounces_today'][today]} недоставленных писем — поставил "
                                   "отправку на паузу. Проверьте адреса и настройки почты, потом /resume.")
                continue
            L = by_email.get(ev["from"])
            who = f"«{L['b']['name']}» ({ev['from']})" if L else ev["from"]
            if ev["kind"] == "refusal":
                with open(HERE / "do_not_contact.txt", "a", encoding="utf-8") as f:
                    f.write(ev["from"] + "\n")
                if L:
                    L["status"] = "refused"
                self.say(chat, f"🙅 {who} отказался — добавил в список «не писать».\n\n{ev['text'][:500]}")
            else:
                if L:
                    L["status"] = "replied"
                self.say(chat, f"📩 Ответ от {who}\nТема: {ev['subject']}\n\n{ev['text'][:1500]}\n\n"
                               "Дальше — вы. Ответьте клиенту из своей почты.")
        with self.lock:
            self.st["mail_seen"] = list(seen)[-2000:]
        self.save()

    # — расписание —
    def schedule_loop(self):
        while True:
            try:
                self.run_schedule_if_due()
            except Exception as e:  # noqa: BLE001
                print(f"Ошибка расписания: {e!r}")
            time.sleep(30)

    def schedule_cities(self) -> list[str]:
        """Города расписания: список из config.json или "все" (по умолчанию) — города из cities.json
        (страны — schedule → countries, по умолчанию Россия, Казахстан и Беларусь), от крупных к небольшим."""
        sched = self.cfg.get("schedule", {})
        cities = sched.get("cities", "все")
        if isinstance(cities, str) and cities.strip().lower() in ALL:
            countries = {c.upper() for c in sched.get("countries", ["RU", "KZ", "BY"])}
            return [c["name"] for c in leads.load_cities() if c.get("country", "RU") in countries]
        return [cities] if isinstance(cities, str) else list(cities)

    def schedule_niches(self) -> list[str]:
        """Ниши расписания: список из config.json или "все" (по умолчанию) — все ниши, кроме помеченных ⏸."""
        niches = self.cfg.get("schedule", {}).get("niches", "все")
        if isinstance(niches, str) and niches.strip().lower() in ALL:
            return leads.scheduled_niches()
        return [niches] if isinstance(niches, str) else list(niches)

    def schedule_queries(self) -> list[str]:
        """Запросы расписания: searches + все сочетания ниш и городов (город за городом, от крупных к небольшим)."""
        sched = self.cfg.get("schedule")
        if not sched:
            return []
        qs = list(sched.get("searches", []))
        if not (sched.get("searches") and "cities" not in sched and "niches" not in sched):  # только searches — как раньше
            niches = self.schedule_niches()
            qs += [f"{n} {c}" for c in self.schedule_cities() for n in niches]
        return list(dict.fromkeys(qs))

    def run_schedule_if_due(self, now: datetime | None = None) -> bool:
        sched = self.cfg.get("schedule", {})
        tz = ZoneInfo(sched.get("tz", "Europe/Moscow"))
        now = now or datetime.now(tz)
        day = now.strftime("%Y-%m-%d")
        if (self.st["last_auto"] == day or now.strftime("%H:%M") < sched.get("time", "10:00")
                or not self.st["owner_chat"]):
            return False
        queries = self.schedule_queries()
        if not queries:
            return False
        self.st["last_auto"] = day
        self.save()
        with self.sched_lock:
            self.run_batch(self.st["owner_chat"], queries, "⏰ Поиск по расписанию")
        return True

    def scan_now(self, chat: int):
        """/scan — тот же обход, что по расписанию, но сейчас: с места, где остановился, с теми же лимитами
        (max_cards, max_searches, лимиты API). Ежедневный запуск по расписанию при этом не отменяется."""
        queries = self.schedule_queries()
        if not queries:
            return self.say(chat, "Расписание не настроено: в config.json нет раздела schedule.")
        if not self.sched_lock.acquire(blocking=False):
            return self.say(chat, "Обход уже идёт — дождитесь сводки в конце.")
        try:
            self.run_batch(chat, queries, "🔎 Обход по команде /scan")
        finally:
            self.sched_lock.release()

    def run_batch(self, chat: int, queries: list[str], title: str):
        sched = self.cfg.get("schedule", {})
        leads.set_api_limits(self.cfg.get("limits", {}).get("api_per_day"))
        left = int(sched.get("max_cards", 20))
        max_searches = int(sched.get("max_searches", 40))  # не перебирать за раз тысячи запросов, если карточек мало
        pos = self.st.get("sched_pos", 0) % len(queries)
        self.say(chat, f"{title}: начинаю с «{queries[pos]}» ({pos + 1}-й из {len(queries)} запросов).")
        niches = leads.load_niches()
        done = cards = failed_in_row = 0
        errors = []
        while done < min(max_searches, len(queries)) and left > 0:
            q = queries[pos]
            req = leads.parse_request(q, niches)
            if req:
                if done:
                    time.sleep(3)  # бесплатные серверы карт просят не слать запросы подряд
                self.acquire_for_batch()
                n = self.search(chat, *req, limit=left, quiet=True)
                left -= n
                cards += n
                err = getattr(self, "last_error", "")
            else:
                err = f"непонятный запрос «{q}»"
            if err and req:
                failed_in_row += 1
                errors.append(f"{q}: {err}")
                if failed_in_row >= 3:
                    # источники лежат — не прожигаем очередь: эти запросы повторим в следующий раз
                    pos = (pos - (failed_in_row - 1)) % len(queries)
                    done -= failed_in_row - 1
                    errors.append("три запроса подряд без ответа источников — остальное в следующий раз (с того же места)")
                    break
            else:
                failed_in_row = 0
                if err:
                    errors.append(err)
            done += 1
            pos = (pos + 1) % len(queries)
            self.st["sched_pos"] = pos  # после каждого запроса: перезапуск бота не отбросит назад
            self.save()
        self.st["sched_pos"] = pos
        self.save()
        summary = (f"Готово: {done} запросов, {cards} карточек. Следующий запрос — «{queries[pos]}» "
                   f"({pos + 1}-й из {len(queries)}).")
        if errors:
            summary += "\nНе получилось: " + "; ".join(errors[:5]) + (f" и ещё {len(errors) - 5}" if len(errors) > 5 else "")
        self.say(chat, summary)


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
