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
STUDIO = HERE / "template" / "studio"

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


def build_demo(b: dict, niche_name: str, niche: dict, city: str, me: dict, out_root: Path,
               template: str = "studio") -> str:
    """Собирает демо в out_root/<токен>/ и возвращает токен (часть ссылки, которую нельзя угадать).
    template: "studio" — дизайн из эталона kuzovomsk-site (по умолчанию), "simple" — простой светлый шаблон."""
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
    if template == "studio":
        page = render_studio(b, cfg, city, me, full_addr, q, phone_href, phone_text, hours, hours_text)
        assets = "../_assets/studio/"
    else:
        page = Template(TPL.read_text(encoding="utf-8")).substitute(values)
        assets = None
    token = secrets.token_urlsafe(9)
    d = out_root / token
    d.mkdir(parents=True)
    (d / "index.html").write_text(page, encoding="utf-8")
    if assets:
        # общие файлы дизайна (стили, шрифты, фото) — одна копия на все демо
        shutil.copytree(STUDIO, out_root / "_assets" / "studio", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.tpl.html"))
    else:
        shutil.copytree(IMG, d / "img")
    robots = out_root / "robots.txt"
    if not robots.exists():
        robots.write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    return token


# Фото для «студийного» дизайна — только со свободными лицензиями (снимки цеха kuzovomsk.ru сюда не входят).
# Для CC BY указание автора обязательно: оно попадает в подвал демо.
STUDIO_PHOTOS = {
    "hero": 'Первый экран: <a href="https://www.flickr.com/photos/24470821@N04/29315296362" target="_blank" rel="noopener">«Win Luxury Cars, DOH, Qatar»</a>, автор Andrew Milligan sumo, <a href="https://creativecommons.org/licenses/by/2.0/" target="_blank" rel="noopener">CC BY 2.0</a>; обработано (ч/б, кадрирование)',
    "svc-body-800": '<a href="https://www.flickr.com/photos/51009184@N06/8057354287" target="_blank" rel="noopener">«Welder at SRS»</a>, автор Savannah River Site, <a href="https://creativecommons.org/licenses/by/2.0/" target="_blank" rel="noopener">CC BY 2.0</a>; обработано (ч/б, кадрирование)',
    "svc-paint-800": '<a href="https://www.flickr.com/photos/123973741@N03/16375842816" target="_blank" rel="noopener">«merecedes 190 cosworth respray»</a>, автор blastpaintrestore, <a href="https://creativecommons.org/licenses/by/2.0/" target="_blank" rel="noopener">CC BY 2.0</a>; обработано (ч/б, кадрирование)',
    "svc-polish-800": '<a href="https://www.flickr.com/photos/7941044@N06/4837522625" target="_blank" rel="noopener">«out with the old.....»</a>, автор jenny downing, <a href="https://creativecommons.org/licenses/by/2.0/" target="_blank" rel="noopener">CC BY 2.0</a>; обработано (ч/б, кадрирование)',
    "svc-lights-800": '<a href="https://www.flickr.com/photos/87664525@N04/23884144009" target="_blank" rel="noopener">«Black car»</a>, автор McraftPix, <a href="https://creativecommons.org/publicdomain/zero/1.0/" target="_blank" rel="noopener">CC0</a>; обработано (ч/б, кадрирование)',
    "tech-1000": '<a href="https://www.flickr.com/photos/44124401501@N01/99245480" target="_blank" rel="noopener">«My car at the garage»</a>, автор sylvar, <a href="https://creativecommons.org/licenses/by/2.0/" target="_blank" rel="noopener">CC BY 2.0</a>; обработано (ч/б, кадрирование)',
}
SERVICE_PHOTOS = ["svc-body-800", "svc-paint-800", "svc-polish-800", "svc-lights-800", "tech-1000"]


def hours_one_line(hours: dict | None) -> str:
    return hours_human(hours).replace("<br>", " · ") if hours else "Режим уточняйте по телефону"


def render_studio(b, cfg, city, me, full_addr, q, phone_href, phone_text, hours, hours_text) -> str:
    e = html.escape
    A = "../_assets/studio/"
    name = b["name"].strip()
    if cfg.get("hero"):
        head, rest = cfg["hero"][0], cfg["hero"][1] if len(cfg["hero"]) > 1 else ""
    else:
        words = cfg["what"].split()
        head, rest = " ".join(words[:2]), " ".join(words[2:])
    hero_title = e(head) + (f'<br><span class="hero__title-muted">{e(rest)}</span>' if rest else "")
    # название в шапке — в две строки, как в эталоне («Центр кузовного<br>ремонта»)
    nw = name.split()
    cut = max(1, (len(nw) + 1) // 2) if len(name) > 14 and len(nw) > 1 else len(nw)
    brand = e(" ".join(nw[:cut])) + ("<br>" + e(" ".join(nw[cut:])) if nw[cut:] else "")

    facts = []
    has_rating = b.get("rating") is not None and b.get("reviews")
    if has_rating:
        n = int(b["reviews"])
        r = f"{float(b['rating']):.1f}".replace(".", ",")
        src = "2ГИС" if "2ГИС" in b.get("sources", []) else "картах"
        facts.append((f"{r} ★", f"{n} {plural(n, 'отзыв', 'отзыва', 'отзывов')} в&nbsp;{src}"))
    if hours:
        first = hours_human(hours).split("<br>")[0]
        facts.append((e(first.split(" ", 1)[1]) if " " in first else e(first), e(first.split(" ", 1)[0]) if " " in first else "режим работы"))
    if b.get("address"):
        facts.append((e(b["address"]), e(city)))
    if phone_text:
        facts.append((e(phone_text), "телефон"))
    facts_html = "\n".join(f"      <li><b>{x}</b><span>{y}</span></li>" for x, y in facts[:4])

    marquee = "".join(f'<span{" class=\"o\"" if i % 2 else ""}>{e(s)}</span><i></i>' for i, s in enumerate(cfg["services"]))
    used = []
    rows = []
    for i, s in enumerate(cfg["services"], 1):
        img = SERVICE_PHOTOS[(i - 1) % len(SERVICE_PHOTOS)]
        used.append(img)
        rows.append(f'      <li class="svc__row" data-reveal data-preview="{A}img/{img}.webp">\n'
                    f'        <a class="svc__link" href="{phone_href}">\n'
                    f'          <span class="svc__num">{i:02d}</span>\n'
                    f'          <span class="svc__title">{e(s)}</span>\n'
                    f'          <span class="svc__price svc__price--ask">уточняйте по&nbsp;телефону</span>\n'
                    f'        </a>\n'
                    f'        <img class="svc__thumb" src="{A}img/{img}.webp" alt="" loading="lazy" width="800" height="533">\n'
                    f'      </li>')

    rating_section = ""
    if has_rating:
        stars = "★" * round(float(b["rating"])) + "☆" * (5 - round(float(b["rating"])))
        rating_section = f"""
<section class="section reviews" id="rating">
  <div class="wrap">
    <div class="head">
      <p class="label" data-reveal><span>03</span>Рейтинг</p>
      <h2 data-reveal>Что пишут клиенты</h2>
    </div>
    <div class="rating-big" data-reveal>
      <span class="rating-big__num">{r}</span>
      <div>
        <span class="rating-big__stars" aria-label="{r} из 5">{stars}</span>
        <p>{n} {plural(n, "отзыв", "отзыва", "отзывов")} в&nbsp;{src} на&nbsp;{date.today().strftime("%d.%m.%Y")}. На&nbsp;сайте здесь будет пересказ ваших настоящих отзывов.</p>
      </div>
    </div>
  </div>
</section>
"""
    credits = [STUDIO_PHOTOS["hero"]] + [f"Услуги: {STUDIO_PHOTOS[x]}" for x in dict.fromkeys(used)]
    values = {
        "assets": A,
        "title": e(f"{name} — {cfg['what']}, {city}"),
        "description": e(f"{cfg['what']}. {full_addr}. Телефон {phone_text}."),
        "tz": CITY_TZ.get(city.strip().lower(), "Europe/Moscow"),
        "hours_json": json.dumps(hours or {}),
        "plaque": ('<span class="demo-bar__full">' + e(f"Демо-концепт сайта для «{name}», подготовил {me.get('name', '')}. "
                   "Это не официальный сайт: данные взяты из открытых источников и могут быть неточны.") + '</span>'
                   '<span class="demo-bar__short">Демо-концепт · не официальный сайт · данные могут быть неточны</span>'),
        "brand": brand,
        "name": e(name), "phone_href": phone_href, "phone_text": e(phone_text or "телефон уточняется"),
        "hours_short": e(hours_one_line(hours)),
        "route_href": f"https://yandex.ru/maps/?text={q}",
        "kicker": e(full_addr),
        "hero_title": hero_title,
        "lead": e(f"{name}. Позвоните — подскажем стоимость и время, когда можно подъехать."),
        "facts": facts_html, "marquee": marquee, "services": "\n".join(rows),
        "address_short": e(f"Адрес: {full_addr}") if b.get("address") else "Адрес уточните по&nbsp;телефону",
        "nav_rating": '<a href="#rating">Рейтинг</a>' if has_rating else "",
        "rating_section": rating_section, "contacts_num": "04" if has_rating else "03",
        "address": e(full_addr), "hours_text": hours_text,
        "map_src": f"https://yandex.ru/map-widget/v1/?mode=search&amp;text={q}&amp;z=16",
        "footer_line": e(full_addr),
        "credits": "\n".join(f"        <li>{c}</li>" for c in credits),
    }
    return Template((STUDIO / "index.tpl.html").read_text(encoding="utf-8")).substitute(values)
