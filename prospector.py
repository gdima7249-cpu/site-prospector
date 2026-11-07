#!/usr/bin/env python3
"""Поиск сайтов с явными проблемами и подготовка черновиков писем владельцам.

Три команды:
  search — ищет сайты через API Яндекс Поиска и дописывает их в список;
  audit  — проверяет сайты из списка, пишет отчёт и черновики писем (ничего не отправляет);
  send   — отправляет только те черновики, которые вы просмотрели и переложили в папку approved/.

Проверки пассивные: скрипт открывает главную, страницу контактов и несколько внутренних ссылок,
как обычный посетитель, и уважает robots.txt. Уязвимости он не ищет и ничего не взламывает.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import os
import random
import re
import smtplib
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

DEFAULT_UA = "Mozilla/5.0 (compatible; SiteAudit/1.0)"
TIMEOUT = 15
YEAR = dt.date.today().year

# ───────────────────────────── проблемы ─────────────────────────────
# вес — насколько проблема заметна владельцу и влияет на заявки; текст — как она звучит в письме.
ISSUES = {
    "no_https": (25, "сайт открывается без HTTPS — браузеры помечают его как «Не защищён», часть посетителей уходит сразу"),
    "bad_cert": (25, "у сайта недействительный SSL-сертификат — браузер показывает предупреждение вместо страницы"),
    "cert_expiring": (10, "SSL-сертификат истекает через {days} дн. — после этого браузеры начнут показывать предупреждение"),
    "no_viewport": (20, "на телефоне страница не подстраивается под экран (нет мобильной вёрстки), а с телефонов сейчас заходит большая часть посетителей"),
    "slow": (15, "главная страница отвечала {seconds} с — это заметно долго для посетителя"),
    "heavy": (5, "HTML главной весит {kb} КБ — страница грузится тяжело, особенно с мобильного интернета"),
    "psi_low": (20, "Google PageSpeed оценивает скорость мобильной версии в {score} из 100"),
    "broken_links": (10, "на главной есть неработающие ссылки ({count} шт.), например: {example}"),
    "old_copyright": (10, "в подвале сайта указан {year} год — посетители могут решить, что компания не работает"),
    "no_title": (8, "у главной страницы нет заголовка (title) — в поиске и на вкладке браузера она выглядит пусто"),
    "no_description": (5, "нет описания страницы для поисковиков (meta description) — Яндекс и Google подставляют случайный кусок текста"),
    "mixed_content": (8, "страница по HTTPS подгружает файлы по HTTP ({count} шт.) — часть из них браузер блокирует"),
    "flash": (20, "часть сайта сделана на Flash — браузеры перестали его показывать в 2021 году, посетители видят пустое место"),
    "old_markup": (5, "сайт свёрстан устаревшими средствами ({what}) — так делали в 2000-х, под телефоны такой код почти не адаптируется"),
}

GENERIC_MAILBOXES = {"info", "office", "mail", "sales", "hello", "contact", "contacts", "admin", "support",
                     "zakaz", "order", "manager", "reception", "secretary", "post", "pochta", "biz", "team"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
BAD_EMAIL_DOMAINS = ("example.com", "example.ru", "sentry.io", "wixpress.com", "domain.com", "email.com", "mail.example")
# признаки заглушки: домен продаётся, сайт не опубликован, пустая страница хостинга
PARKED_HINTS = ("домен продается", "домен продаётся", "домен выставлен на продажу", "купить домен", "купить этот домен",
                "domain is for sale", "this domain is for sale", "buy this domain", "domain for sale", "parked domain",
                "domain parking", "сайт не опубликован", "сайт заблокирован", "срок регистрации домена истек",
                "срок регистрации домена истёк", "аккаунт заблокирован", "account suspended", "index of /")
CONTACT_HINTS = ("contact", "kontakt", "контакт", "about", "o-nas", "o-kompanii", "о-нас", "о компании", "связ")


@dataclass
class Audit:
    input: str
    domain: str
    url: str = ""
    ok: bool = False
    error: str = ""
    https: bool = False
    status: int = 0
    seconds: float = 0.0
    html_kb: int = 0
    title: str = ""
    generator: str = ""
    cert_days_left: int | None = None
    copyright_year: int | None = None
    psi_score: int | None = None
    broken: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    issues: dict[str, dict] = field(default_factory=dict)
    score: int = 0

    def add(self, key: str, **facts):
        self.issues[key] = facts


# ───────────────────────────── сеть ─────────────────────────────
class Fetcher:
    def __init__(self, ua: str, respect_robots: bool = True):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": ua, "Accept-Language": "ru,en;q=0.8"})
        self.ua = ua
        self.respect_robots = respect_robots
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=TIMEOUT)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[base] = rp
        return self._robots[base].can_fetch(self.ua, url)

    def get(self, url: str, **kw) -> requests.Response:
        return self.s.get(url, timeout=TIMEOUT, allow_redirects=True, **kw)

    def head_status(self, url: str) -> int:
        try:
            r = self.s.head(url, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code in (403, 405, 501):  # многие серверы не любят HEAD
                r = self.s.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
                r.close()
            return r.status_code
        except requests.RequestException:
            return 0


def cert_days_left(host: str, port: int = 443) -> int | None:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                not_after = tls.getpeercert()["notAfter"]
        expires = dt.datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), dt.timezone.utc)
        return (expires - dt.datetime.now(dt.timezone.utc)).days
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


def pagespeed(url: str, api_key: str | None) -> int | None:
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if api_key:
        params["key"] = api_key
    try:
        r = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=120)
        r.raise_for_status()
        score = r.json()["lighthouseResult"]["categories"]["performance"]["score"]
        return round(score * 100) if score is not None else None
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


# ───────────────────────────── разбор страницы ─────────────────────────────
def normalize(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line:
        line = "http://" + line
    host = urlparse(line).hostname
    return host.lower().removeprefix("www.") if host else None


def find_copyright_year(soup: BeautifulSoup) -> int | None:
    text = soup.get_text(" ", strip=True)
    years = []
    for m in re.finditer(r"(?:©|&copy;|\(c\)|copyright|все права защищены)", text, re.I):
        chunk = text[max(0, m.start() - 40): m.end() + 40]
        years += [int(y) for y in re.findall(r"\b(19[89]\d|20\d\d)\b", chunk)]
    years = [y for y in years if y <= YEAR]
    return max(years) if years else None


def extract_emails(html: str, soup: BeautifulSoup, domain: str) -> list[str]:
    found = set()
    for a in soup.select('a[href^="mailto:"]'):
        found.add(a["href"][7:].split("?")[0].strip())
    found.update(EMAIL_RE.findall(soup.get_text(" ")))
    found.update(EMAIL_RE.findall(html))
    clean = []
    for e in found:
        e = e.strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(e) or e.endswith(BAD_EMAIL_SUFFIXES) or e.split("@")[1] in BAD_EMAIL_DOMAINS:
            continue
        clean.append(e)
    return sorted(set(clean), key=lambda e: email_rank(e, domain))


def email_rank(email: str, domain: str) -> tuple:
    """Чем меньше, тем лучше. Сначала адреса на домене сайта, потом ящики с названием сайта (zyb-ok@mail.ru),
    потом общие (info@), в конце — похожие на личные (lerner1966@mail.ru): это персональные данные человека."""
    local, _, host = email.partition("@")
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    on_domain = host == domain or host.endswith("." + domain)
    named = bool(stem) and len(stem) >= 3 and stem in re.sub(r"[^a-z0-9]", "", local)
    return (not on_domain, not named, not is_generic(email), looks_personal(email), email)


def looks_personal(email: str) -> bool:
    local = email.split("@")[0]
    return bool(re.search(r"(19[4-9]\d|20[0-2]\d)", local) or re.fullmatch(r"[a-z]+[._][a-z]+", local))


def is_generic(email: str) -> bool:
    return email.split("@")[0] in GENERIC_MAILBOXES


def same_site(url: str, domain: str) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return host == domain


def audit_site(raw: str, fetcher: Fetcher, check_links: int, psi: bool, psi_key: str | None) -> Audit:
    domain = normalize(raw) or raw
    a = Audit(input=raw.strip(), domain=domain)
    explicit = raw.strip() if "://" in raw else None
    if explicit and explicit.startswith("https://"):
        candidates = [explicit, "http://" + explicit[8:]]
    elif explicit:
        candidates = [explicit]
    else:
        candidates = [f"https://{domain}", f"https://www.{domain}", f"http://{domain}"]

    resp = None
    for url in candidates:
        if not fetcher.allowed(url):
            a.error = "robots.txt запрещает обход"
            return a
        try:
            t0 = time.monotonic()
            resp = fetcher.get(url)
            a.seconds = round(time.monotonic() - t0, 2)
            break
        except requests.exceptions.SSLError:
            a.add("bad_cert")
        except requests.RequestException as e:
            a.error = type(e).__name__
    if resp is None:
        return finish(a)

    a.url, a.status = resp.url, resp.status_code
    if resp.status_code >= 400:
        a.error = f"сайт отвечает ошибкой {resp.status_code}"
        return finish(a)
    page_text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True).lower()
    if any(h in page_text[:3000] for h in PARKED_HINTS):
        a.error = "заглушка: домен продаётся или сайт не работает"
        return finish(a)
    a.ok, a.error = True, ""
    a.https = resp.url.startswith("https://")
    html = resp.text
    a.html_kb = len(resp.content) // 1024
    soup = BeautifulSoup(html, "html.parser")

    if not a.https:
        # если HTTPS есть, но с плохим сертификатом, это одна проблема, а не две
        if "bad_cert" not in a.issues:
            a.add("no_https")
    else:
        a.issues.pop("bad_cert", None)
        host = urlparse(a.url).hostname
        port = urlparse(a.url).port or 443
        a.cert_days_left = cert_days_left(host, port)
        # бесплатные сертификаты (Let's Encrypt) живут 90 дней и продлеваются сами незадолго до конца,
        # поэтому «осталось 20 дней» — норма. Тревожно, только если осталось совсем мало.
        if a.cert_days_left is not None and a.cert_days_left < 7:
            a.add("cert_expiring", days=a.cert_days_left)
        insecure = [t for t in soup.select("script[src], img[src], link[href], iframe[src]")
                    if (t.get("src") or t.get("href") or "").startswith("http://")]
        if insecure:
            a.add("mixed_content", count=len(insecure))

    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        a.add("no_viewport")
    if a.seconds > 3:
        # один медленный ответ может быть случайностью: перемеряем и берём лучший результат
        try:
            t0 = time.monotonic()
            fetcher.get(a.url)
            a.seconds = round(min(a.seconds, time.monotonic() - t0), 2)
        except requests.RequestException:
            pass
    if a.seconds > 3:
        a.add("slow", seconds=a.seconds)
    if a.html_kb > 500:
        a.add("heavy", kb=a.html_kb)

    a.title = (soup.title.string or "").strip()[:150] if soup.title and soup.title.string else ""
    if not a.title:
        a.add("no_title")
    desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if not desc or not (desc.get("content") or "").strip():
        a.add("no_description")
    gen = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    a.generator = (gen.get("content") or "").strip() if gen else ""

    if soup.find("embed", src=re.compile(r"\.swf", re.I)) or soup.find("object", data=re.compile(r"\.swf", re.I)) \
            or soup.find("param", value=re.compile(r"\.swf", re.I)):
        a.add("flash")
    old = [t for t in ("font", "center", "marquee", "blink", "frameset") if soup.find(t)]
    if old:
        a.add("old_markup", what=", ".join(f"<{t}>" for t in old))

    a.copyright_year = find_copyright_year(soup)
    if a.copyright_year and a.copyright_year <= YEAR - 2:
        a.add("old_copyright", year=a.copyright_year)

    # контакты: главная + до двух страниц «Контакты/О нас»
    emails = extract_emails(html, soup, domain)
    links = [urljoin(a.url, x["href"]) for x in soup.find_all("a", href=True)]
    internal = list(dict.fromkeys(u.split("#")[0] for u in links if same_site(u, domain) and u.startswith("http")))
    contact_pages = [u for u in internal if any(h in u.lower() for h in CONTACT_HINTS)]
    for x in soup.find_all("a", href=True):
        if any(h in x.get_text(" ", strip=True).lower() for h in CONTACT_HINTS):
            u = urljoin(a.url, x["href"]).split("#")[0]
            if same_site(u, domain) and u not in contact_pages:
                contact_pages.append(u)
    for u in contact_pages[:2]:
        if not fetcher.allowed(u):
            continue
        try:
            r = fetcher.get(u)
            emails += extract_emails(r.text, BeautifulSoup(r.text, "html.parser"), domain)
        except requests.RequestException:
            pass
    a.emails = list(dict.fromkeys(extract_emails(" ".join(emails), BeautifulSoup("", "html.parser"), domain)))

    # битые внутренние ссылки
    to_check = [u for u in internal if u.rstrip("/") != a.url.rstrip("/")][:check_links]
    for u in to_check:
        if fetcher.allowed(u):
            code = fetcher.head_status(u)
            if code >= 400:
                a.broken.append(f"{u} ({code})")
    if a.broken:
        a.add("broken_links", count=len(a.broken), example=a.broken[0])

    if psi:
        a.psi_score = pagespeed(a.url, psi_key)
        if a.psi_score is not None and a.psi_score < 50:
            a.add("psi_low", score=a.psi_score)

    return finish(a)


def finish(a: Audit) -> Audit:
    a.score = min(100, sum(ISSUES[k][0] for k in a.issues))
    return a


def issue_text(key: str, facts: dict) -> str:
    return ISSUES[key][1].format(**facts)


def top_issues(a: Audit, n: int = 3) -> list[str]:
    keys = sorted(a.issues, key=lambda k: -ISSUES[k][0])[:n]
    return [issue_text(k, a.issues[k]) for k in keys]


# ───────────────────────────── черновики писем ─────────────────────────────
def template_draft(a: Audit, me: dict) -> tuple[str, str]:
    site = urlparse(a.url).netloc or a.domain
    subject = f"Пара замечаний по сайту {site}"
    points = "\n".join(f"— {t};" for t in top_issues(a))
    points = points[:-1] + "."
    body = f"""Здравствуйте!

Меня зовут {me['name']}{', ' + me['company'] if me.get('company') else ''}, я занимаюсь сайтами. На {site} есть несколько вещей, которые, скорее всего, стоят вам части заявок:

{points}

{me['offer']}

Если не актуально — просто ответьте «нет», больше писать не буду.

{me['signature']}"""
    return subject, body


SYSTEM_PROMPT = """Ты пишешь короткое деловое письмо владельцу сайта от имени веб-разработчика.
Правила:
- Пиши по-русски, на «вы», без восклицательных знаков и рекламных штампов («уникальное предложение», «выведем в топ»).
- Упоминай ТОЛЬКО проблемы из переданного JSON. Не придумывай новых фактов о сайте, компании, цифр, цен, сроков и гарантий.
- Род отправителя неизвестен: не используй глаголы прошедшего времени от первого лица («зашёл», «заметила»).
- Не пугай и не оскорбляй: никаких «ваш сайт ужасен», «вас взломают».
- 80–150 слов. Структура: приветствие, кто пишет (из данных отправителя), 2–3 конкретные проблемы с пояснением, чем они мешают, предложение отправителя, строка «Если не актуально — просто ответьте «нет», больше писать не буду.», подпись.
- Первая строка ответа — тема письма в формате «Тема: ...», затем пустая строка, затем текст письма. Больше ничего не выводи."""


class LLMRefused(RuntimeError):
    pass


def llm_draft(a: Audit, me: dict, model: str) -> tuple[str, str]:
    import anthropic

    facts = {
        "сайт": urlparse(a.url).netloc or a.domain,
        "заголовок_страницы": a.title,
        "проблемы": top_issues(a, 4),
        "отправитель": {k: me.get(k, "") for k in ("name", "company", "offer", "signature")},
    }
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False, indent=2)}],
    )
    if response.stop_reason == "refusal":
        raise LLMRefused(a.domain)
    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    first, _, rest = text.partition("\n")
    if first.lower().startswith("тема:"):
        return first.split(":", 1)[1].strip(), rest.strip()
    return f"Пара замечаний по сайту {facts['сайт']}", text


def write_draft(path: Path, to: str, subject: str, body: str, site: str):
    path.write_text(f"To: {to}\nSubject: {subject}\nX-Site: {site}\n\n{body}\n", encoding="utf-8")


def read_draft(path: Path) -> tuple[dict, str]:
    head, _, body = path.read_text(encoding="utf-8").partition("\n\n")
    headers = {}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return headers, body.strip() + "\n"


# ───────────────────────────── списки «не писать» и «уже писали» ─────────────────────────────
def load_suppression(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {ln.strip().lower() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def suppressed(email_or_domain: str, block: set[str]) -> bool:
    v = email_or_domain.lower()
    dom = v.split("@")[-1].removeprefix("www.")
    return v in block or dom in block


def load_sent(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {r["domain"] for r in rows} | {r["to"].lower() for r in rows}


# ───────────────────────────── поиск через Яндекс ─────────────────────────────
YANDEX_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
# каталоги, соцсети и агрегаторы: их сайты не про «владельца бизнеса», писать им бессмысленно
SKIP_DOMAINS = {
    "yandex.ru", "ya.ru", "dzen.ru", "google.com", "vk.com", "ok.ru", "t.me", "youtube.com", "rutube.ru",
    "avito.ru", "2gis.ru", "zoon.ru", "yell.ru", "flamp.ru", "otzovik.com", "irecommend.ru", "prodoctorov.ru",
    "napopravku.ru", "sberhealth.ru", "docdoc.ru", "profi.ru", "youdo.com", "hh.ru", "superjob.ru",
    "wikipedia.org", "ozon.ru", "wildberries.ru", "market.yandex.ru", "rusprofile.ru", "list-org.com",
    "spravker.ru", "orgpage.ru", "tripadvisor.ru", "restoclub.ru", "cian.ru", "domclick.ru", "drom.ru", "auto.ru",
    "pikabu.ru", "vc.ru", "habr.com", "mail.ru", "rbc.ru", "gosuslugi.ru", "nalog.gov.ru",
}


class YandexError(RuntimeError):
    pass


def root_domain_skipped(domain: str, skip: set[str]) -> bool:
    parts = domain.split(".")
    return any(".".join(parts[i:]) in skip for i in range(len(parts) - 1)) or domain.endswith(".gov.ru")


def yandex_search(query: str, page: int, api_key: str, folder_id: str, region: str | None) -> list[str]:
    """Одна страница выдачи → список URL. Синхронный режим API v2, ответ — XML в Base64."""
    body = {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query, "page": str(page)},
        "folderId": folder_id,
        "responseFormat": "FORMAT_XML",
    }
    if region:
        body["region"] = region
    r = requests.post(YANDEX_URL, json=body, headers={"Authorization": f"Api-Key {api_key}"}, timeout=60)
    if r.status_code != 200:
        raise YandexError(f"HTTP {r.status_code}: {r.text[:300]}")
    raw = r.json().get("rawData")
    if not raw:
        raise YandexError(f"в ответе нет rawData: {r.text[:300]}")
    root = ET.fromstring(base64.b64decode(raw))
    err = root.find(".//error")
    if err is not None:
        if err.get("code") == "15":  # «Искомая комбинация слов нигде не встречается» — просто пустая выдача
            return []
        raise YandexError(f"ошибка Яндекса {err.get('code')}: {(err.text or '').strip()}")
    return [u.text.strip() for u in root.iterfind(".//doc/url") if u.text]


def cmd_search(args):
    api_key, folder = os.environ.get("YANDEX_SEARCH_API_KEY"), os.environ.get("YANDEX_FOLDER_ID")
    if not api_key or not folder:
        sys.exit("Нужны переменные YANDEX_SEARCH_API_KEY и YANDEX_FOLDER_ID (см. README).")
    queries = list(args.query)
    if args.queries:
        queries += [q.strip() for q in Path(args.queries).read_text(encoding="utf-8").splitlines()
                    if q.strip() and not q.startswith("#")]
    if not queries:
        sys.exit("Укажите запрос или файл с запросами (--queries).")

    out = Path(args.output)
    known = set()
    if out.exists():
        known = {d for d in map(normalize, out.read_text(encoding="utf-8").splitlines()) if d}
    block = load_suppression(Path(args.suppress)) | SKIP_DOMAINS

    added = []
    for q in queries:
        for page in range(args.pages):
            try:
                urls = yandex_search(q, page, api_key, folder, args.region)
            except (YandexError, requests.RequestException, ET.ParseError, ValueError) as e:
                print(f"«{q}», стр. {page + 1}: {e}", file=sys.stderr)
                break
            for u in urls:
                d = normalize(u)
                if d and d not in known and not root_domain_skipped(d, block):
                    known.add(d)
                    added.append(d)
            print(f"«{q}», стр. {page + 1}: результатов {len(urls)}, новых сайтов всего {len(added)}", file=sys.stderr)
            if not urls:
                break
            time.sleep(args.delay)
    with out.open("a", encoding="utf-8") as f:
        for d in added:
            f.write(d + "\n")
    print(f"Добавлено {len(added)} сайтов в {out}", file=sys.stderr)


# ───────────────────────────── команды ─────────────────────────────
def cmd_audit(args):
    out = Path(args.out)
    drafts = out / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (out / "approved").mkdir(exist_ok=True)
    block = load_suppression(Path(args.suppress))
    already = load_sent(out / "sent.csv")

    seen, sites = set(), []
    for line in Path(args.urls).read_text(encoding="utf-8").splitlines():
        d = normalize(line)
        if d and d not in seen and not suppressed(d, block) and d not in already:
            seen.add(d)
            sites.append(line.strip())
    print(f"Сайтов к проверке: {len(sites)}", file=sys.stderr)

    me = json.loads(Path(args.sender).read_text(encoding="utf-8-sig")) if Path(args.sender).exists() else None
    fetcher = Fetcher(args.user_agent, respect_robots=not args.ignore_robots)
    psi_key = os.environ.get("PSI_API_KEY")

    def job(s):
        try:
            return audit_site(s, fetcher, args.check_links, args.pagespeed, psi_key)
        except Exception as e:  # один кривой сайт не должен ронять весь прогон
            return Audit(input=s, domain=normalize(s) or s, error=f"{type(e).__name__}: {e}")

    results: list[Audit] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, a in enumerate(pool.map(job, sites), 1):
            results.append(a)
            state = f"оценка {a.score}" if a.ok else f"ошибка: {a.error}"
            print(f"[{i}/{len(sites)}] {a.domain}: {state}", file=sys.stderr)

    results.sort(key=lambda a: -a.score)
    (out / "report.json").write_text(json.dumps([asdict(a) for a in results], ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    with (out / "report.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["домен", "оценка", "url", "email", "проблемы", "ошибка", "сек", "PageSpeed", "CMS"])
        for a in results:
            w.writerow([a.domain, a.score, a.url, ", ".join(a.emails[:3]),
                        " | ".join(issue_text(k, v) for k, v in a.issues.items()), a.error,
                        a.seconds or "", a.psi_score if a.psi_score is not None else "", a.generator])

    made = 0
    if me is None:
        print(f"Нет файла {args.sender} — черновики писем не созданы (см. sender.example.json).", file=sys.stderr)
    else:
        for a in results:
            if not a.ok or a.score < args.min_score or not a.emails:
                continue
            to = a.emails[0]
            if suppressed(to, block) or to in already:
                continue
            try:
                subject, body = llm_draft(a, me, args.model) if args.llm else template_draft(a, me)
            except Exception as e:
                print(f"{a.domain}: нейросеть не написала письмо ({type(e).__name__}), беру шаблон", file=sys.stderr)
                subject, body = template_draft(a, me)
            write_draft(drafts / f"{a.domain}.txt", to, subject, body, a.url)
            made += 1

    ok = sum(a.ok for a in results)
    print(f"\nГотово: проверено {ok} из {len(results)}, черновиков {made}.\n"
          f"Отчёт: {out / 'report.csv'}\nЧерновики: {drafts}/ — прочитайте, поправьте и переложите нужные в {out / 'approved'}/",
          file=sys.stderr)


def smtp_connect():
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    if port == 465:
        srv = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
    else:
        srv = smtplib.SMTP(host, port, timeout=30)
        srv.starttls(context=ssl.create_default_context())
    srv.login(user, password)
    return srv


def cmd_send(args):
    out = Path(args.out)
    approved, sent_dir = out / "approved", out / "sent"
    sent_dir.mkdir(parents=True, exist_ok=True)
    log = out / "sent.csv"
    block = load_suppression(Path(args.suppress))
    already = load_sent(log)
    files = sorted(approved.glob("*.txt"))
    if not files:
        print(f"В {approved}/ нет одобренных черновиков.", file=sys.stderr)
        return

    sender = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER", "")
    from_name = os.environ.get("SMTP_FROM_NAME", "")
    srv = None if args.dry_run else smtp_connect()
    new_log = not log.exists()
    sent = 0
    with log.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["date", "domain", "to", "subject"])
        for path in files:
            if sent >= args.limit:
                print(f"Достигнут лимит {args.limit} писем за запуск.", file=sys.stderr)
                break
            h, body = read_draft(path)
            to, domain = h.get("to", ""), path.stem
            if not to or suppressed(to, block) or to in already or domain in already:
                print(f"пропуск {domain}: в списке «не писать» или уже отправляли", file=sys.stderr)
                continue
            msg = EmailMessage()
            msg["From"] = formataddr((from_name, sender)) if from_name else sender
            msg["To"] = to
            msg["Subject"] = h.get("subject", "")
            msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
            msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
            msg.set_content(body)
            if args.dry_run:
                print(f"[проверка] → {to}: {msg['Subject']}", file=sys.stderr)
            else:
                srv.send_message(msg)
                w.writerow([dt.datetime.now().isoformat(timespec="seconds"), domain, to, msg["Subject"]])
                f.flush()
                path.rename(sent_dir / path.name)
                print(f"отправлено → {to}", file=sys.stderr)
            sent += 1
            if sent < args.limit and path != files[-1] and not args.dry_run:
                time.sleep(args.delay + random.uniform(0, args.delay / 2))
    if srv:
        srv.quit()
    print(f"Итого: {sent}{' (проверка, ничего не отправлено)' if args.dry_run else ''}.", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("search", help="найти сайты через API Яндекс Поиска")
    q.add_argument("query", nargs="*", help="поисковые запросы, например \"стоматология Казань\"")
    q.add_argument("--queries", help="файл с запросами, по одному в строке")
    q.add_argument("--pages", type=int, default=3, help="сколько страниц выдачи брать на запрос (по умолчанию 3)")
    q.add_argument("--region", help="код региона Яндекса, например 213 — Москва, 2 — Санкт-Петербург")
    q.add_argument("--output", default="urls.txt", help="куда дописывать сайты (по умолчанию urls.txt)")
    q.add_argument("--delay", type=float, default=1.0, help="пауза между запросами, секунд")
    q.add_argument("--suppress", default="do_not_contact.txt")
    q.set_defaults(func=cmd_search)

    a = sub.add_parser("audit", help="проверить сайты и подготовить черновики")
    a.add_argument("urls", help="файл со списком сайтов, по одному в строке")
    a.add_argument("-o", "--out", default="out", help="папка для результатов (по умолчанию out)")
    a.add_argument("--sender", default="sender.json", help="данные отправителя для писем")
    a.add_argument("--min-score", type=int, default=30, help="писать только сайтам с оценкой не ниже этой (0–100)")
    a.add_argument("--check-links", type=int, default=15, help="сколько внутренних ссылок проверять на битость")
    a.add_argument("--pagespeed", action="store_true", help="спросить оценку Google PageSpeed (медленно; ключ — в PSI_API_KEY)")
    a.add_argument("--llm", action="store_true", help="писать письма нейросетью Claude вместо шаблона (нужен ANTHROPIC_API_KEY)")
    a.add_argument("--model", default="claude-opus-5-5", help="модель Claude для --llm")
    a.add_argument("--workers", type=int, default=5, help="сколько сайтов проверять параллельно")
    a.add_argument("--user-agent", default=DEFAULT_UA)
    a.add_argument("--ignore-robots", action="store_true", help="не смотреть robots.txt (не рекомендуется)")
    a.add_argument("--suppress", default="do_not_contact.txt", help="файл с адресами и доменами, которым не писать")
    a.set_defaults(func=cmd_audit)

    s = sub.add_parser("send", help="отправить одобренные черновики из OUT/approved/")
    s.add_argument("-o", "--out", default="out")
    s.add_argument("--limit", type=int, default=20, help="максимум писем за запуск (по умолчанию 20)")
    s.add_argument("--delay", type=float, default=90, help="пауза между письмами, секунд (плюс случайные 0–50%%)")
    s.add_argument("--dry-run", action="store_true", help="показать, что ушло бы, ничего не отправляя")
    s.add_argument("--suppress", default="do_not_contact.txt")
    s.set_defaults(func=cmd_send)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
