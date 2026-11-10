"""Сборка демо-сайта по эталону для конкретной компании.

В демо попадают только найденные факты: название, телефон, адрес, часы, рейтинг. Цен нет — «уточняйте по телефону»;
блока «За что хвалят» нет — без чтения отзывов его не заполнить честно. Плашка сверху говорит, что это демо.
"""
from __future__ import annotations

import html
import json
import re
import secrets
import shutil
from datetime import date
from pathlib import Path
from string import Template
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
TPL = HERE / "template" / "demo.tpl.html"
IMG = HERE / "template" / "img"

DAYS = {"mo": 1, "tu": 2, "we": 3, "th": 4, "fr": 5, "sa": 6, "su": 7}
DAY_RU = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Вс"}
CITY_TZ = {  # для бейджа «Открыто до …»; по умолчанию — Москва
    "омск": "Asia/Omsk", "новосибирск": "Asia/Novosibirsk", "барнаул": "Asia/Barnaul", "томск": "Asia/Tomsk",
    "красноярск": "Asia/Krasnoyarsk", "кемерово": "Asia/Novokuznetsk", "новокузнецк": "Asia/Novokuznetsk",
    "иркутск": "Asia/Irkutsk", "екатеринбург": "Asia/Yekaterinburg", "челябинск": "Asia/Yekaterinburg",
    "тюмень": "Asia/Yekaterinburg", "пермь": "Asia/Yekaterinburg", "уфа": "Asia/Yekaterinburg",
    "самара": "Europe/Samara", "ижевск": "Europe/Samara", "саратов": "Europe/Saratov", "ульяновск": "Europe/Ulyanovsk",
    "волгоград": "Europe/Volgograd", "астрахань": "Europe/Astrakhan", "калининград": "Europe/Kaliningrad",
    "владивосток": "Asia/Vladivostok", "хабаровск": "Asia/Vladivostok", "якутск": "Asia/Yakutsk",
}

ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/></svg>')


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def parse_osm_hours(s: str) -> dict[str, list[str]] | None:
    """«Mo-Fr 09:00-18:00; Sa 10:00-15:00» → {"1": ["09:00","18:00"], …}. Сложные форматы — None (бейдж не показываем)."""
    s = (s or "").strip()
    if s == "24/7":
        return {str(d): ["00:00", "24:00"] for d in range(1, 8)}
    out: dict[str, list[str]] = {}
    for part in filter(None, (x.strip() for x in s.split(";"))):
        m = re.fullmatch(r"((?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?"
                         r"(?:,(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?)*)\s+"
                         r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", part, re.I)
        if not m:
            if re.fullmatch(r"(?:PH|(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:[-,](?:Mo|Tu|We|Th|Fr|Sa|Su))*)\s+off", part, re.I):
                continue
            return None
        for rng in m.group(1).split(","):
            a, _, b = rng.lower().partition("-")
            lo, hi = DAYS[a], DAYS[b or a]
            days = range(lo, hi + 1) if lo <= hi else [*range(lo, 8), *range(1, hi + 1)]
            for d in days:
                out[str(d)] = [m.group(2).zfill(5), m.group(3).zfill(5)]
    return out or None


def hours_human(hours: dict[str, list[str]]) -> str:
    """{"1":…,"5":…} → «Пн–Пт 9:00–18:00<br>Сб 10:00–15:00<br>Вс — выходной»."""
    def t(x):
        h, m = x.split(":")
        return f"{int(h)}:{m}"
    lines, d = [], 1
    while d <= 7:
        cur = hours.get(str(d))
        e = d
        while e + 1 <= 7 and hours.get(str(e + 1)) == cur:
            e += 1
        days = DAY_RU[d] if d == e else f"{DAY_RU[d]}–{DAY_RU[e]}"
        lines.append(f"{days} — выходной" if cur is None else
                     f"{days} круглосуточно" if cur == ["00:00", "24:00"] else f"{days} {t(cur[0])}–{t(cur[1])}")
        d = e + 1
    return "<br>".join(lines)


def phone_parts(phone: str) -> tuple[str, str]:
    first = (phone or "").split(";")[0].strip()
    digits = re.sub(r"\D", "", first)
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return (f"tel:+{digits}" if digits else "#"), first


def build_demo(b: dict, niche_name: str, niche: dict, city: str, me: dict, out_root: Path) -> str:
    """Собирает демо в out_root/<токен>/ и возвращает токен (часть ссылки, которую нельзя угадать)."""
    cfg = niche.get("demo")
    if not cfg:
        raise ValueError(f"для ниши «{niche_name}» нет эталона")
    e = html.escape
    name = b["name"].strip()
    phone_href, phone_text = phone_parts(b.get("phone", ""))
    address = b.get("address") or ""
    full_addr = f"{city}, {address}" if address and city.lower() not in address.lower() else (address or city)
    q = quote(f"{name}, {full_addr}")

    hours = parse_osm_hours(b.get("hours", ""))
    if hours:
        tz = CITY_TZ.get(city.strip().lower(), "Europe/Moscow")
        status = (f'<div class="status" id="status" data-tz="{tz}" '
                  f"data-hours='{json.dumps(hours)}'>Часы работы</div>")
        hours_text = hours_human(hours)
    else:
        status = ""
        hours_text = e(b["hours"]) if b.get("hours") else "уточняйте по телефону"

    chips = ""
    rating_section = ""
    if b.get("rating") is not None and b.get("reviews"):
        n = int(b["reviews"])
        r = f"{float(b['rating']):.1f}".replace(".", ",")
        src = "2ГИС" if "2ГИС" in b.get("sources", []) else "картах"
        chips = f'<ul class="chips"><li>★ {r} · {n} {plural(n, "отзыв", "отзыва", "отзывов")} в {src}</li></ul>'
        stars = "★" * round(float(b["rating"])) + "☆" * (5 - round(float(b["rating"])))
        rating_section = f"""    <section id="rating">
      <p class="eyebrow">Рейтинг</p>
      <h2>Что пишут клиенты</h2>
      <div class="card rating">
        <div class="score"><span class="num">{r}</span>
          <div><span class="stars" aria-label="{r} из 5">{stars}</span><small>{n} {plural(n, "отзыв", "отзыва", "отзывов")} в {src}</small></div></div>
        <p>Рейтинг и число отзывов — по данным {src} на {date.today().strftime("%d.%m.%Y")}. На сайте здесь будет пересказ ваших отзывов.</p>
      </div>
    </section>"""

    services = "\n".join(
        f'      <li class="card"><span class="ico">{ICON}</span><span class="name">{e(s)}</span>'
        f'<span class="ask">уточняйте<br>по телефону</span></li>' for s in cfg["services"])

    values = {
        "title": e(f"{name} — {cfg['what']}, {city}"),
        "description": e(f"{cfg['what']}. {full_addr}. Телефон {phone_text}."),
        "plaque": e(f"Демо-концепт сайта для «{name}», подготовил {me.get('name', '')}. Это не официальный сайт: "
                    "данные взяты из открытых источников и могут быть неточны."),
        "status_block": status,
        "name": e(name), "what": e(cfg["what"]), "where": e(full_addr),
        "phone_href": phone_href, "phone_text": e(phone_text or "телефон уточняется"),
        "route_href": f"https://yandex.ru/maps/?text={q}",
        "chips": chips, "services": services,
        "price_note": "На сайте здесь будут ваши реальные цены «от». Пока — уточняйте по телефону.",
        "praise_section": "", "rating_section": rating_section,
        "address": e(full_addr), "hours_text": hours_text,
        "map_src": f"https://yandex.ru/map-widget/v1/?mode=search&amp;text={q}&amp;z=16",
        "final_title": "Позвоните — подскажем цену", "final_text": "Расскажем стоимость и когда можно подъехать.",
        "footer": e(f"{name}, {city}"),
    }
    page = Template(TPL.read_text(encoding="utf-8")).substitute(values)
    token = secrets.token_urlsafe(9)
    d = out_root / token
    d.mkdir(parents=True)
    (d / "index.html").write_text(page, encoding="utf-8")
    shutil.copytree(IMG, d / "img")
    robots = out_root / "robots.txt"
    if not robots.exists():
        robots.write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return token
