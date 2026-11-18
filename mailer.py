"""Почта: отправка одного письма через SMTP и разбор входящих (ответы, отказы, недоставленные письма).

Входящие читаются без отметки «прочитано» (BODY.PEEK), чтобы вы видели их непрочитанными в своём почтовом клиенте.
"""
from __future__ import annotations

import email
import imaplib
import re
import smtplib
import ssl
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

# явный отказ в любом месте ответа
REFUSAL_RE = re.compile(r"(^|[^а-яё])(не интересно|неинтересно|не актуально|неактуально|не пишите|больше не пишите|"
                        r"отпишите|отписаться|уберите из рассылки|спам)([^а-яё]|$)|unsubscribe", re.I)
# короткое «нет» / «нет, спасибо» — только если ответ с него начинается и он короткий («нет проблем, давайте» — не отказ)
SHORT_NO_RE = re.compile(r"^\s*(нет|no)\b[\s,.!]*(спасибо|не нужно|не надо|не требуется)?[\s.!]*$", re.I)
BOUNCE_FROM = ("mailer-daemon", "postmaster", "mail delivery")
BOUNCE_SUBJ = re.compile(r"undeliver|delivery status|delivery failure|returned mail|failure notice|"
                         r"не доставлен|недоставлен|не удалось доставить", re.I)
# автоматические письма (уведомления сервисов, рассылки) — не ответы клиентов
AUTO_FROM = re.compile(r"(no-?reply|do-?not-?reply|notification|notify|newsletter|accounts\.google\.com|"
                       r"mail-noreply|robot@|info@accounts\.)", re.I)
QUOTE_START = re.compile(r"^(>|-{2,}|_{5,}|.*(написал|написала|wrote|пишет)\s*:?\s*$|.*<[^<>@\s]+@[^<>\s]+>\s*:\s*$|From:|От:)", re.I)


# ───────────────────────────── ящики ─────────────────────────────
# Ящиков может быть несколько: разделы smtp, smtp2, smtp3… в config.json. Письма делятся между ними поровну,
# ответы бот читает во всех. Раздел с незаполненным образцом («пароль приложения», «ваш-домен») не считается.
PLACEHOLDERS = ("пароль приложения", "ваш-домен", "вторая-почта")


def mailboxes(cfg: dict) -> list[dict]:
    """Заполненные ящики из config.json по порядку: [{…раздел smtpN…, "key": "smtp2"}, …]."""
    keys = sorted((k for k in cfg if re.fullmatch(r"smtp\d*", k)), key=lambda k: int(k[4:] or 1))
    out = []
    for k in keys:
        box = cfg[k]
        if not isinstance(box, dict) or not all(box.get(x) for x in ("host", "user", "password")):
            continue
        if any(ph in str(box.get(x, "")).lower() for x in ("user", "password") for ph in PLACEHOLDERS):
            continue
        out.append(dict(box, key=k))
    return out


def address(box: dict) -> str:
    return (box.get("from") or box.get("user") or "").lower()


def imap_settings(cfg: dict, box: dict) -> dict:
    """Чтение ответов для ящика: для первого — раздел imap (если есть), иначе адрес IMAP выводится из SMTP
    (smtp.yandex.ru → imap.yandex.ru, smtp.mail.ru → imap.mail.ru). Логин и пароль — те же, что для отправки."""
    base = dict(cfg.get("imap") or {}) if box.get("key", "smtp") == "smtp" else {}
    out = {"host": box.get("imap_host") or base.get("host") or re.sub(r"^smtp\.", "imap.", box["host"]),
           "port": box.get("imap_port") or base.get("port", 993)}
    out["user"] = base.get("user") or box["user"]
    out["password"] = base.get("password") or box["password"]
    return out


def quotas(total: int, boxes: list[dict]) -> dict[str, int]:
    """Сколько писем в день с каждого ящика. У ящика может быть свой per_day; остальные делят limits.per_day поровну
    (10 писем и два ящика — по 5)."""
    fixed = {b["key"]: int(b["per_day"]) for b in boxes if str(b.get("per_day", "")).strip().isdigit()}
    free = [b["key"] for b in boxes if b["key"] not in fixed]
    left = max(total - sum(fixed.values()), 0) if fixed else total
    out = dict(fixed)
    for i, k in enumerate(free):
        out[k] = left // len(free) + (1 if i < left % len(free) else 0)
    return out


def send(cfg: dict, to: str, subject: str, body: str, pdfs: list[tuple[str, bytes]] | None = None) -> str:
    """Отправляет письмо, возвращает Message-ID. cfg — раздел smtp из config.json; pdfs — вложения (имя, данные)."""
    sender = cfg.get("from") or cfg["user"]
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name", ""), sender)) if cfg.get("from_name") else sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(body)
    for name, data in pdfs or []:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    port = int(cfg.get("port", 465))
    ctx = ssl.create_default_context()
    if port == 465:
        srv = smtplib.SMTP_SSL(cfg["host"], port, context=ctx, timeout=30)
    else:
        srv = smtplib.SMTP(cfg["host"], port, timeout=30)
        srv.starttls(context=ctx)
    try:
        srv.login(cfg["user"], cfg["password"])
        srv.send_message(msg)
    finally:
        srv.quit()
    return msg["Message-ID"]


def _text(msg: email.message.Message) -> str:
    part = msg
    if msg.is_multipart():
        part = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), None) or \
            next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
        if part is None:
            return ""
    raw = part.get_payload(decode=True) or b""
    text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text


def fresh_part(text: str) -> str:
    """Текст ответа без процитированного исходного письма."""
    lines = []
    for line in text.splitlines():
        if QUOTE_START.match(line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def classify(msg: email.message.Message) -> dict:
    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    text = _text(msg)
    if any(x in msg.get("From", "").lower() for x in BOUNCE_FROM) or BOUNCE_SUBJ.search(subject):
        m = re.search(r"Final-Recipient:\s*rfc822;\s*(\S+@\S+)", text, re.I) or \
            re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        return {"kind": "bounce", "from": sender, "subject": subject,
                "failed": (m.group(1) if m.lastindex else m.group(0)).strip("<>;,.").lower() if m else ""}
    auto = (msg.get("Auto-Submitted", "no").lower() not in ("", "no") or msg.get("List-Unsubscribe")
            or msg.get("Precedence", "").lower() in ("bulk", "list", "junk") or AUTO_FROM.search(sender))
    if auto:
        return {"kind": "auto", "from": sender, "subject": subject}
    reply = fresh_part(text)
    refused = subject.lower().startswith("unsubscribe") or REFUSAL_RE.search(reply[:300]) or SHORT_NO_RE.match(reply[:60])
    kind = "refusal" if refused else "reply"
    return {"kind": kind, "from": sender, "subject": subject, "text": reply[:1500]}


SPAM_NAMES = ("spam", "junk", "&bceepwqwbdw-")  # «Спам» в кодировке IMAP (modified UTF-7)


def spam_folder(box: imaplib.IMAP4) -> str | None:
    """Имя папки «Спам»: по пометке \\Junk (её ставят Яндекс, Mail.ru, Gmail) или по названию."""
    _, rows = box.list()
    for row in rows or []:
        line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
        m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip()
        bare = name.strip('"').split("/")[-1].split("|")[-1].lower()
        if "\\junk" in m.group("flags").lower() or "\\spam" in m.group("flags").lower() or bare in SPAM_NAMES:
            return name
    return None


def fetch_new(cfg: dict, seen: set[str], days: int = 3, folder: str = "INBOX") -> list[tuple[str, dict]]:
    """Новые письма за последние дни, которых нет в seen. cfg — настройки IMAP (imap_settings).
    folder="spam" — папка «Спам» (если её нет, пустой список)."""
    box = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port", 993)), ssl_context=ssl.create_default_context())
    out = []
    try:
        box.login(cfg["user"], cfg["password"])
        if folder == "spam":
            folder = spam_folder(box)
            if not folder:
                return []
        status, _ = box.select(folder, readonly=True)
        if status != "OK":
            return []
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = box.uid("search", None, f"SINCE {since}")
        for uid in (data[0] or b"").split():
            u = uid.decode()
            if u in seen:
                continue
            _, parts = box.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append((u, classify(email.message_from_bytes(raw))))
    finally:
        try:
            box.logout()
        except Exception:  # noqa: BLE001
            pass
    return out
