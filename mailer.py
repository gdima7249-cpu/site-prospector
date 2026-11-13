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


def fetch_new(cfg: dict, seen: set[str], days: int = 3) -> list[tuple[str, dict]]:
    """Новые письма за последние дни, которых нет в seen. cfg — раздел imap (+ логин из smtp)."""
    box = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port", 993)), ssl_context=ssl.create_default_context())
    out = []
    try:
        box.login(cfg["user"], cfg["password"])
        box.select("INBOX", readonly=True)
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
