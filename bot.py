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
import sys
import threading
import time
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

HELP = """Напишите нишу и город, например:
кузовной ремонт Омск
стоматология Казань

Бот пришлёт карточки лучших кандидатов. Под каждой кнопки:
✅ Сделать демо — бот соберёт демо-сайт по эталону и подготовит письмо (только автомобильные ниши)
❌ Пропустить — больше не показывать
Потом: 📨 Отправить / ✏️ Изменить текст / ❌ Отмена.

Команды:
/status — сколько писем отправлено сегодня, пауза, расписание
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
                "sent_today": {}, "bounces_today": {}, "paused": False, "pending_edit": None, "last_auto": "",
                "mail_seen": [], "last_send_ts": 0}
    for k, v in defaults.items():
        st.setdefault(k, v)
    if isinstance(st["last"], dict) or (st["last"] and isinstance(st["last"][0], dict)):  # формат старых версий
        st["last"] = []
    return st


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


def draft_text(L: dict, demo_url: str | None = None) -> str:
    d = make_draft(L, demo_url)
    b = L["b"]
    to = d["to"] or (f"нет email — напишите в WhatsApp/Telegram или позвоните: {b['phone']}" if b["phone"] else "контактов нет")
    return f"Кому: {to}\nТема: {d['subject']}\n\n{d['body']}"


def spawn(target, *args):
    """Фоновая задача (в тестах подменяется на синхронный вызов)."""
    threading.Thread(target=target, args=args, daemon=True).start()


def kb(*rows: list[tuple[str, str]]) -> str:
    return json.dumps({"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in row] for row in rows]},
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
    def __init__(self, token: str, config: dict | None = None):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.st = load_state()
        self.cfg = config if config is not None else load_config()
        self.busy = threading.Lock()
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        demo = self.cfg.get("demo", {})
        self.demo_root = HERE / demo.get("dir", "public_demos")
        self.demo_url = demo.get("public_url", "").rstrip("/")

    # — Telegram API —
    def call(self, method: str, timeout: int = 60, **params):
        r = requests.post(self.api + method, data=params, timeout=timeout)
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
        if self.cfg.get("schedule", {}).get("searches"):
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
        if self.st["pending_edit"] and not text.startswith("/"):
            return self.apply_edit(chat, text)
        if cmd in ("/start", "/help"):
            return self.say(chat, HELP)
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
                mark = " 🖼" if d.get("demo") else ""
                by_tier.get(d.get("tier", "mid"), by_tier["mid"]).append(name + mark)
            return self.say(chat, "🖼 — для ниши есть эталон, бот умеет делать демо.\n\nВысокий чек: "
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
        if not self.busy.acquire(blocking=False):
            return self.say(chat, "Ещё ищу по прошлому запросу, подождите.")
        spawn(self.search, chat, *req)

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
                f"Демо-страницы: {self.demo_url or 'не настроены (config.json → demo)'}\n"
                f"Расписание: {(sched.get('time') + ', ' + '; '.join(sched['searches'])) if sched.get('searches') else 'нет'}\n"
                f"Кандидаты: отправлено {statuses.count('sent')}, ответили {statuses.count('replied')}, "
                f"отказались {statuses.count('refused')}")

    def today(self) -> str:
        tz = ZoneInfo(self.cfg.get("schedule", {}).get("tz", "Europe/Moscow"))
        return datetime.now(tz).strftime("%Y-%m-%d")

    # — поиск —
    def search(self, chat: int, niche_name: str, city: str, limit: int = SHOW_TOP) -> int:
        """Ищет и присылает карточки. Возвращает число присланных карточек."""
        try:
            niche = leads.load_niches()[niche_name]
            self.say(chat, f"Ищу «{niche_name}» в городе {city}…")
            try:
                found, notes = leads.find_businesses(niche_name, niche, city)
            except LookupError as e:
                self.say(chat, str(e))
                return 0
            except leads.MapError as e:
                keys = leads.api_keys()
                tip = "" if keys["2gis"] or keys["yandex"] else (
                    "\n\nБесплатные серверы OpenStreetMap иногда перегружены. Надёжнее подключить ключ 2ГИС или Яндекса "
                    "(см. /sources) — тогда бот будет работать и без OpenStreetMap.")
                self.say(chat, f"Источники не ответили.\n{e}\n\nПопробуйте через 5–10 минут.{tip}")
                return 0
            skip = set(self.st["skip"]) | set(self.st["seen"]) | p.load_suppression(HERE / "do_not_contact.txt")
            fresh = [b for b in found if leads.keep(b, skip)][:MAX_LEADS]
            if not fresh:
                self.say(chat, f"Новых компаний не нашёл ({'; '.join(notes)}). Попробуйте другую нишу или город.")
                return 0
            n_site = sum(1 for b in fresh if b["site"])
            self.say(chat, f"Нашёл {len(fresh)} новых компаний ({'; '.join(notes)}): {n_site} с сайтом, "
                           f"{len(fresh) - n_site} без. Проверяю сайты, это пара минут.")
            audits = audit_sites(fresh)
            rows = build_leads(fresh, audits, niche.get("tier", "mid"), city)
            ids = []
            with self.lock:
                for L in rows:
                    i = lead_id(L["b"])
                    L.update(id=i, niche=niche_name, city=city, status="new")
                    self.st["leads"][i] = L
                    ids.append(i)
                self.st["seen"] += [leads.business_key(L["b"]) for L in rows]
                self.st["last"] = ids
                if len(self.st["leads"]) > 1000:  # не раздувать файл состояния
                    for old in list(self.st["leads"])[:-1000]:
                        self.st["leads"].pop(old)
            self.save()
            top = [L for L in rows if L["score"] >= MIN_SCORE][:limit]
            if not top:
                self.say(chat, f"Оценил {len(rows)} компаний — подходящих мало. Полный список в файле.")
            for n, L in enumerate(top, 1):
                buttons = [("✅ Сделать демо", f"demo:{L['id']}")] if niche.get("demo") else \
                          [("✉️ Письмо без демо", f"demo:{L['id']}")]
                self.say(chat, format_lead(n, L), kb(buttons + [("❌ Пропустить", f"skip:{L['id']}")]))
            self.send_file(chat, f"{niche_name}-{city}.csv".replace(" ", "_"), csv_bytes(rows),
                           "Все компании с приоритетом и рекомендацией (открывается в Excel)")
            return len(top)
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка поиска: {e!r}")
            self.say(chat, f"Что-то сломалось: {type(e).__name__}. Подробности — в окне, где запущен бот.")
            return 0
        finally:
            if self.busy.locked():
                self.busy.release()

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
            self.st["skip"].append(leads.business_key(L["b"]))
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
        if niche.get("demo"):
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
        to = d["to"] or "нет email — скопируйте текст и отправьте в WhatsApp/Telegram: " + (L["b"]["phone"] or "контактов нет")
        text = f"{head}Кому: {to}\nТема: {d['subject']}\n\n{d['body']}"
        buttons = [("✏️ Изменить", f"edit:{L['id']}"), ("❌ Отмена", f"cancel:{L['id']}")]
        if d["to"] and self.cfg.get("smtp"):
            buttons.insert(0, ("📨 Отправить", f"send:{L['id']}"))
        elif d["to"]:
            text += "\n\n(Почта для отправки не настроена — config.json → smtp. Пока отправьте вручную.)"
        self.say(chat, text, kb(buttons))

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
            if ev.get("from") == me:
                continue  # собственные письма (например, /testmail) — не ответы клиентов
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

    def run_schedule_if_due(self, now: datetime | None = None) -> bool:
        sched = self.cfg.get("schedule", {})
        tz = ZoneInfo(sched.get("tz", "Europe/Moscow"))
        now = now or datetime.now(tz)
        day = now.strftime("%Y-%m-%d")
        if self.st["last_auto"] == day or now.strftime("%H:%M") < sched.get("time", "10:00") or not self.st["owner_chat"]:
            return False
        self.st["last_auto"] = day
        self.save()
        chat = self.st["owner_chat"]
        left = int(sched.get("max_cards", SHOW_TOP))
        self.say(chat, "⏰ Поиск по расписанию.")
        niches = leads.load_niches()
        for q in sched["searches"]:
            if left <= 0:
                break
            req = leads.parse_request(q, niches)
            if not req:
                self.say(chat, f"В расписании непонятный запрос: «{q}» — пропускаю.")
                continue
            self.busy.acquire()
            left -= self.search(chat, *req, limit=left)
        return True


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
