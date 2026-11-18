"""Поиск компаний-клиентов: источники (OpenStreetMap, Яндекс, 2ГИС), ниши, приоритет и рекомендация по цене.

Источники:
- OpenStreetMap — бесплатно, без ключей, всегда включён. Данные заполняют добровольцы, бывают устаревшими.
- Яндекс, «API поиска по организациям» — нужен ключ (YANDEX_ORG_API_KEY или keys.json).
- 2ГИС, «Catalog API» — нужен ключ (DGIS_API_KEY или keys.json). Даёт рейтинг и число отзывов.
Условия и цены Яндекса и 2ГИС проверяйте на их сайтах: оба API, насколько известно, платные.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

import prospector as p

HERE = Path(__file__).resolve().parent
UA = "site-prospector-bot/1.2 (+https://github.com/gdima7249-cpu/site-prospector)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
YANDEX_ORG_URL = "https://search-maps.yandex.ru/v1/"
DGIS_URL = "https://catalog.api.2gis.com/3.0/items"


class MapError(RuntimeError):
    """Источник не ответил. Текст — для пользователя: какой сервис и что вернул."""


def _describe(r: requests.Response) -> str:
    hint = {401: "неверный ключ", 403: "доступ запрещён или ключ не подходит", 429: "слишком много запросов",
            400: "не понял запрос", 502: "сервер перегружен", 503: "сервер перегружен",
            504: "сервер не успел ответить"}.get(r.status_code, "")
    return f"HTTP {r.status_code}" + (f" — {hint}" if hint else "")


# ───────────────────────────── ниши ─────────────────────────────
NICHES_FILE = HERE / "niches.json"          # ниши по умолчанию (обновляются вместе с ботом)
CUSTOM_NICHES_FILE = HERE / "niches_custom.json"  # ваши ниши (бот их не трогает при обновлении)


def load_niches() -> dict[str, dict]:
    niches = json.loads(NICHES_FILE.read_text(encoding="utf-8-sig"))
    niches.pop("_comment", None)
    if CUSTOM_NICHES_FILE.exists():
        niches.update(json.loads(CUSTOM_NICHES_FILE.read_text(encoding="utf-8-sig")))
    return niches


def add_custom_niche(name: str, keywords: list[str], tier: str = "mid") -> dict:
    custom = {}
    if CUSTOM_NICHES_FILE.exists():
        custom = json.loads(CUSTOM_NICHES_FILE.read_text(encoding="utf-8-sig"))
    entry = {"osm": [], "keywords": keywords, "tier": tier if tier in ("low", "mid", "high") else "mid"}
    custom[name.lower()] = entry
    CUSTOM_NICHES_FILE.write_text(json.dumps(custom, ensure_ascii=False, indent=1), encoding="utf-8")
    return entry


def parse_request(text: str, niches: dict[str, dict]) -> tuple[str, str] | None:
    """«Стоматология  Казань» → ("стоматология", "Казань"). Ниша — самое длинное совпадение в начале."""
    t = " ".join(text.split())
    low = t.lower()
    names = {n: n for n in niches}
    for n, d in niches.items():
        for a in d.get("aliases", []):
            names[a.lower()] = n
    for alias in sorted(names, key=len, reverse=True):
        if low.startswith(alias + " "):
            city = t[len(alias):].strip()
            return names[alias], city
    return None


CITIES_FILE = HERE / "cities.json"  # города России от 100 тыс. жителей (обновляются вместе с ботом)


def load_cities() -> list[dict]:
    """[{"name", "region", "tz", "pop_k"}, …] — от крупных к небольшим."""
    try:
        data = json.loads(CITIES_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return sorted(data.get("cities", []), key=lambda c: -(c.get("pop_k") or 0))


def other_cities(city: str, limit: int = 8) -> list[str]:
    """Где ещё поискать ту же нишу, если в городе мало подходящих компаний: сначала города того же региона,
    потом остальные той же страны: сначала с близким часовым поясом, среди них — близкие по размеру. Координат в
    cities.json нет, поэтому «соседние» определяются по региону и часовому поясу, а не по расстоянию."""
    cities = load_cities()
    key = city.strip().lower().replace("ё", "е")
    me = next((c for c in cities if c["name"].lower().replace("ё", "е") == key), None)
    if me is None:  # города нет в списке: берём крупные города, начиная с самых больших
        return [c["name"] for c in cities if c.get("country", "RU") == city_country(city)][:limit]
    rank = {c["name"]: i for i, c in enumerate(cities)}
    others = [c for c in cities if c["name"] != me["name"] and c.get("country", "RU") == me.get("country", "RU")]
    region = [c["name"] for c in others if c.get("region") == me.get("region")]
    my_off = _utc_offset(me.get("tz", ""))
    rest = sorted((c for c in others if c["name"] not in region),
                  key=lambda c: (abs(_utc_offset(c.get("tz", "")) - my_off), abs(rank[c["name"]] - rank[me["name"]])))
    return (region + [c["name"] for c in rest])[:limit]


def _utc_offset(tz: str) -> float:
    """Сдвиг часового пояса от UTC в часах (Омск +6, Москва +3): чем ближе сдвиг, тем ближе город на восток-запад."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(tz)).utcoffset().total_seconds() / 3600
    except Exception:  # noqa: BLE001 — неизвестный пояс
        return 3.0


def scheduled_niches(niches: dict[str, dict] | None = None) -> list[str]:
    """Ниши для ежедневного перебора: все, кроме помеченных "schedule": false (сети, розница)."""
    niches = niches if niches is not None else load_niches()
    return [n for n, d in niches.items() if d.get("schedule", True)]


# ───────────────────────────── фильтры ─────────────────────────────
STATE_RE = re.compile(r"\b(ГБУЗ|ГАУЗ|ГБУ|ГАУ|МБУ|МАУ|МБОУ|МБДОУ|МАДОУ|ФГБУ|ФГБОУ|ФГУП|ГУП|МУП|ГКБ|ГУЗ|МУЗ|ОГБУЗ|КГБУЗ)\b"
                      r"|поликлиник|больниц|государствен|муниципальн|республиканск|министерств|управлени[ея] здравоохран",
                      re.I)
STATE_OPERATORS = {"public", "government", "community"}
ROOT_PATHS = {"", "index.html", "index.htm", "index.php", "ru", "main", "home"}


def is_state(name: str, extra: str = "", operator_type: str = "") -> bool:
    return operator_type in STATE_OPERATORS or bool(STATE_RE.search(f"{name} {extra}"))


def is_portal_page(site: str) -> bool:
    """Сайт указан как страница внутри чужого портала (zdrav.tatar.ru/kzn/dsp6), а не свой домен."""
    path = urlparse(site if "://" in site else "http://" + site).path.strip("/").lower()
    return path not in ROOT_PATHS


def phone_key(phone: str) -> str:
    digits = re.sub(r"\D", "", phone.split(";")[0])
    return digits[-10:] if len(digits) >= 10 else ""


def phone_keys(phone: str) -> list[str]:
    """Все номера компании (источники пишут их через «;» или «,» и в разном порядке)."""
    return list(dict.fromkeys(k for k in map(phone_key, re.split(r"[;,]", phone or "")) if k))


def business_key(b: dict) -> str:
    return b.get("domain") or (("tel:" + phone_key(b["phone"])) if phone_key(b.get("phone", "")) else "") \
        or "name:" + b.get("name", "").strip().lower()


def business_keys(b: dict) -> list[str]:
    """Все признаки, по которым компанию можно узнать снова: домен, каждый телефон, почта.
    Первый — business_key. Нужны все: в другой день компания может прийти из другого источника —
    уже с сайтом, с телефонами в другом порядке — и её главный признак изменится."""
    keys = [business_key(b)]
    if b.get("domain"):
        keys.append(b["domain"])
    keys += ["tel:" + k for k in phone_keys(b.get("phone", ""))]
    if b.get("email"):
        keys.append("email:" + b["email"].strip().lower())
    return list(dict.fromkeys(keys))


def new_business(**kw) -> dict:
    b = {"name": "", "site": "", "domain": "", "email": "", "phone": "", "address": "", "hours": "",
         "rating": None, "reviews": None, "sources": [], "osm_date": "", "closed": ""}
    b.update(kw)
    b["domain"] = p.normalize(b["site"]) if b["site"] else ""
    return b


def merge(groups: list[list[dict]]) -> list[dict]:
    """Объединяет одну и ту же компанию из разных источников (по домену, телефону или названию)."""
    out: dict[str, dict] = {}
    by_phone: dict[str, str] = {}
    for group in groups:
        for b in group:
            key = business_key(b)
            pks = phone_keys(b.get("phone", ""))
            if key not in out:
                key = next((by_phone[pk] for pk in pks if pk in by_phone), key)
            if key not in out:
                out[key] = dict(b, sources=list(b["sources"]))
            else:
                cur = out[key]
                for f in ("name", "site", "domain", "email", "phone", "address", "hours", "osm_date", "closed"):
                    if not cur.get(f) and b.get(f):
                        cur[f] = b[f]
                for f in ("rating", "reviews"):
                    if b.get(f) is not None and (cur.get(f) is None or b[f] > cur[f]):
                        cur[f] = b[f]
                cur["sources"] = sorted(set(cur["sources"]) | set(b["sources"]))
            for pk in pks:
                by_phone.setdefault(pk, key)
    return list(out.values())


def keep(b: dict, skip: set[str]) -> bool:
    if b["domain"] and (p.root_domain_skipped(b["domain"], p.SKIP_DOMAINS) or b["domain"] in skip
                        or b["domain"].startswith(("zdrav.", "minzdrav.")) or ".gov." in b["domain"]):
        return False
    if b["site"] and is_portal_page(b["site"]):
        return False
    if any(k in skip for k in business_keys(b)) or (b.get("email") and b["email"].lower() in skip):
        return False  # уже показывали, пропустили или в do_not_contact.txt — по любому из признаков
    if not b["site"] and not b["phone"] and not b["email"]:
        return False  # без сайта и без контактов предложить нечего
    return True


# ───────────────────────────── OpenStreetMap ─────────────────────────────
COUNTRY_NAMES = {"RU": "Россия", "KZ": "Казахстан", "BY": "Беларусь"}
CITY_CACHE_FILE = HERE / "city_cache.json"  # границы городов: Nominatim просит не спрашивать одно и то же повторно


def _city_cache() -> dict:
    try:
        return json.loads(CITY_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find_city(city: str) -> tuple[float, float, float, float] | None:
    """Границы города (юг, запад, север, восток): из city_cache.json или через Nominatim.
    Для городов из cities.json в запрос добавляется регион — чтобы не перепутать одноимённые города."""
    key = city.strip().lower()
    cache = _city_cache()
    if key in cache:
        return tuple(cache[key]) if cache[key] else None
    known = next((c for c in load_cities() if c["name"].lower() == key), {})
    country = COUNTRY_NAMES.get(known.get("country", ""), "")
    region = known.get("region", "")
    exact = ", ".join(x for x in (city, region if region.lower() != key else "", country) if x)
    box = None
    for q in ([exact] if known else []) + [city]:
        box = _nominatim(q)
        if box:
            break
    if box:  # «не нашёл» не запоминаем: могла быть опечатка или временный сбой
        cache[key] = list(box)
        try:
            tmp = CITY_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CITY_CACHE_FILE)
        except OSError:
            pass
    return box


def _nominatim(q: str) -> tuple[float, float, float, float] | None:
    params = {"q": q, "format": "json", "limit": 1, "accept-language": "ru", "countrycodes": "ru,by,kz,ua"}
    r, err = None, ""
    for attempt in range(2):
        try:
            r = requests.get(NOMINATIM, params=params, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as e:
            err = type(e).__name__
        else:
            if r.status_code == 200:
                break
            err = _describe(r)
            print(f"Nominatim: {err}: {r.text[:300]!r}")
        if attempt == 0:
            time.sleep(2)
    else:
        raise MapError(f"поиск города (nominatim.openstreetmap.org): {err}")
    data = r.json()
    if not data:
        return None
    s, n, w, e = map(float, data[0]["boundingbox"])
    return s, w, n, e


def _ci(word: str) -> str:
    """Регулярка Overpass без учёта регистра первой буквы: «стомат» → «[Сс]томат» (для кириллицы ,i ненадёжен)."""
    word = re.sub(r'[\\"^$.|?*+()\[\]{}]', "", word.strip().lower())  # без спецсимволов: строка уходит в Overpass как есть
    return f"[{word[0].upper()}{word[0]}]{word[1:]}" if word and word[0].isalpha() else word


def overpass_query(niche: dict, bbox: tuple[float, float, float, float], limit: int = 400, light: bool = False) -> str:
    """Запрос к Overpass. Поиск по словам в названии — самая тяжёлая часть, поэтому только в shop/office/craft.
    light=True — облегчённый запрос на случай перегрузки: только теги ниши (или слова только в shop/craft)."""
    box = ",".join(f"{x:.5f}" for x in bbox)
    parts = []
    for tag in niche.get("osm", []):
        k, _, v = tag.partition("=")
        parts.append(f'  nwr["{k}"="{v}"]({box});')
    kw = niche.get("keywords") or []
    if kw and (not light or not parts):
        rx = "|".join(_ci(w) for w in kw)
        for k in (("shop", "craft") if light else ("shop", "office", "craft")):
            parts.append(f'  nwr["{k}"]["name"~"{rx}"]({box});')
    timeout = 40 if light else 60
    # out meta — вместе с тегами дата последней правки: по ней видно, давно ли кто-то подтверждал, что компания на месте
    return f"[out:json][timeout:{timeout}];\n(\n" + "\n".join(parts) + f"\n);\nout meta {200 if light else limit};"


_OSM_CACHE: dict[tuple, tuple[float, list]] = {}
OSM_CACHE_TTL = 6 * 3600  # повторный поиск того же в течение 6 часов не нагружает серверы


def _overpass(query: str, errors: list[str]) -> list | None:
    for url in OVERPASS_URLS:
        host = urlparse(url).hostname
        try:
            r = requests.post(url, data={"data": query}, headers={"User-Agent": UA}, timeout=75)
        except requests.RequestException as e:
            errors.append(f"{host}: {type(e).__name__}")
            continue
        if r.status_code == 200:
            try:
                return r.json().get("elements", [])
            except ValueError:
                errors.append(f"{host}: ответ не в JSON")
                continue
        errors.append(f"{host}: {_describe(r)}")
        print(f"Overpass {host}: {_describe(r)}: {r.text[:300]!r}")
    return None


def osm_search(niche: dict, city: str) -> list[dict]:
    bbox = find_city(city)
    if bbox is None:
        raise LookupError(f"Не нашёл город «{city}»")
    cache_key = (json.dumps(niche, sort_keys=True, ensure_ascii=False), city.strip().lower())
    hit = _OSM_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < OSM_CACHE_TTL:
        return [dict(b, sources=list(b["sources"])) for b in hit[1]]
    # сначала полный запрос по всем серверам, потом, после паузы, облегчённый
    errors: list[str] = []
    elements = None
    for i, light in enumerate((False, True)):
        if i:
            time.sleep(5)
        elements = _overpass(overpass_query(niche, bbox, light=light), errors)
        if elements is not None:
            break
    if elements is None:
        raise MapError("OpenStreetMap (Overpass) перегружен: " + "; ".join(dict.fromkeys(errors)))

    def site_of(t):
        return t.get("website") or t.get("contact:website") or t.get("url") or ""

    # домен, указанный у нескольких разных организаций, — это портал или сеть, а не сайт одного владельца
    owners: dict[str, set] = {}
    for el in elements:
        t = el.get("tags", {})
        d = p.normalize(site_of(t)) if site_of(t) else ""
        if d:
            owners.setdefault(d, set()).add(t.get("name", "").strip().lower())

    found = []
    for el in elements:
        t = el.get("tags", {})
        name = t.get("name", "").strip()
        if not name or is_state(name, f"{t.get('official_name', '')} {t.get('operator', '')}", t.get("operator:type", "")):
            continue
        site = site_of(t)
        if site and len(owners.get(p.normalize(site), ())) > 1:
            continue
        street = " ".join(x for x in (t.get("addr:street", ""), t.get("addr:housenumber", "")) if x)
        hours = t.get("opening_hours", "")
        found.append(new_business(
            name=name, site=site, email=(t.get("email") or t.get("contact:email") or "").lower(),
            phone=t.get("phone") or t.get("contact:phone") or "", address=street,
            hours="" if hours.strip().lower() in ("closed", "off") else hours, sources=["OSM"],
            osm_date=osm_date(el), closed=osm_closed(t, name)))
    _OSM_CACHE[cache_key] = (time.time(), [dict(b, sources=list(b["sources"])) for b in found])
    return found


# ───────────────────────────── работает ли компания ─────────────────────────────
# Бесплатного реестра «открыта / закрыта» для всех компаний нет. Бот смотрит на то, что есть у источников:
# - OpenStreetMap: пометки о закрытии (disused:*, was:*, «закрыто» в названии, часы «closed», дата окончания)
#   и дата последней правки или проверки на месте (check_date). Давно не правили — данные могли устареть;
# - справочники 2ГИС и Яндекс: компания там есть (с ключами) — данные свежие;
# - отзывы и работающий сайт — признаки живого бизнеса.
LIFECYCLE = ("disused:", "abandoned:", "was:", "closed:", "removed:", "demolished:", "razed:")
CLOSED_NAME = re.compile(r"закрыт|ликвидир|не работает|переехал", re.I)
STALE_YEARS = 5  # данные OpenStreetMap старше этого без других подтверждений — «могла закрыться»


def osm_date(el: dict) -> str:
    """Дата последней правки объекта в OpenStreetMap или проверки на месте (check_date), что новее: ГГГГ-ММ-ДД."""
    t = el.get("tags", {})
    dates = [str(el.get("timestamp", ""))[:10]] + [str(t.get(k, ""))[:10] for k in ("check_date", "survey:date")]
    dates = [d for d in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)]
    return max(dates) if dates else ""


def osm_closed(t: dict, name: str) -> str:
    """Почему по данным OpenStreetMap компания закрыта (пустая строка — пометок о закрытии нет)."""
    if any(k.startswith(LIFECYCLE) for k in t) or t.get("disused") == "yes" or t.get("abandoned") == "yes":
        return "в OpenStreetMap помечена как закрытая"
    if CLOSED_NAME.search(name) or CLOSED_NAME.search(t.get("description", "")) or CLOSED_NAME.search(t.get("note", "")):
        return "в OpenStreetMap написано, что закрыта"
    if t.get("opening_hours", "").strip().lower() in ("closed", "off"):
        return "в OpenStreetMap часы работы: «закрыто»"
    end = str(t.get("end_date", ""))[:10]
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", end) and end <= time.strftime("%Y-%m-%d"):
        return f"в OpenStreetMap дата закрытия {end}"
    return ""


def activity(b: dict, a: p.Audit | dict | None = None, today: str | None = None) -> tuple[str, str]:
    """Работает ли компания: ("closed" | "active" | "stale" | "unknown", пояснение для карточки).
    closed — источник прямо говорит, что закрыта (такие не показываем); active — есть свежие признаки жизни;
    stale — только старые данные OpenStreetMap; unknown — подтверждений нет, но и данные не старые."""
    if b.get("closed"):
        return "closed", b["closed"]
    signs = []
    dirs = [s for s in b.get("sources", []) if s in DIRECTORIES]
    if dirs:
        signs.append("есть в " + " и ".join(sorted(dirs)))
    if b.get("reviews"):
        signs.append(f"{b['reviews']} отзывов")
    if (a.get("ok") if isinstance(a, dict) else getattr(a, "ok", False)):  # a — проверка сайта (или её dict из карточки)
        signs.append("сайт открывается")
    if signs:
        return "active", ", ".join(signs)
    d = b.get("osm_date", "")
    today = today or time.strftime("%Y-%m-%d")
    if d and int(today[:4]) - int(d[:4]) - (today[5:] < d[5:]) >= STALE_YEARS:
        return "stale", f"данные о компании не обновлялись с {d[:4]} г."
    if d:
        return "unknown", f"только OpenStreetMap, данные обновлялись в {d[:4]} г."
    return "unknown", "подтверждений нет"


ACTIVITY_ICON = {"active": "🟢 работает", "stale": "🟠 могла закрыться", "unknown": "🟡 не проверить точно"}


# ───────────────────────────── Яндекс ─────────────────────────────
def yandex_search(query: str, api_key: str, pages: int = 2, _count: list | None = None) -> list[dict]:
    """API поиска по организациям Яндекса: до 50 компаний за запрос. Формат ответа — GeoJSON."""
    found = []
    for page in range(pages):
        if _count is not None:
            _count[0] += 1
        params = {"apikey": api_key, "text": query, "type": "biz", "lang": "ru_RU", "results": 50, "skip": page * 50}
        try:
            r = requests.get(YANDEX_ORG_URL, params=params, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as e:
            raise MapError(f"Яндекс (поиск по организациям): {type(e).__name__}")
        if r.status_code != 200:
            print(f"Яндекс: {_describe(r)}: {r.text[:300]!r}")
            raise MapError(f"Яндекс (поиск по организациям): {_describe(r)}")
        features = r.json().get("features", [])
        for f in features:
            m = f.get("properties", {}).get("CompanyMetaData", {})
            if not m.get("name"):
                continue
            phones = [ph.get("formatted", "") for ph in m.get("Phones", []) if ph.get("formatted")]
            found.append(new_business(name=m["name"], site=m.get("url", ""), phone="; ".join(phones),
                                      address=m.get("address", ""), hours=(m.get("Hours") or {}).get("text", ""),
                                      sources=["Яндекс"]))
        if len(features) < 50:
            break
    return found


# ───────────────────────────── 2ГИС ─────────────────────────────
def dgis_search(query: str, api_key: str, pages: int = 5, _count: list | None = None) -> list[dict]:
    """Catalog API 2ГИС: по 10 компаний на страницу, с рейтингом и числом отзывов."""
    found = []
    for page in range(1, pages + 1):
        if _count is not None:
            _count[0] += 1
        params = {"q": query, "key": api_key, "page": page, "page_size": 10, "type": "branch",
                  "fields": "items.contact_groups,items.reviews,items.address,items.schedule"}
        try:
            r = requests.get(DGIS_URL, params=params, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as e:
            raise MapError(f"2ГИС: {type(e).__name__}")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") or r.text.startswith("{") else {}
        code = (data.get("meta") or {}).get("code", r.status_code)
        if code == 404:  # 2ГИС отвечает 404, когда страницы закончились
            break
        if r.status_code != 200 or code != 200:
            print(f"2ГИС: {_describe(r)}: {r.text[:300]!r}")
            raise MapError(f"2ГИС: {_describe(r) if r.status_code != 200 else 'код ' + str(code)}")
        items = (data.get("result") or {}).get("items", [])
        for it in items:
            site = email = ""
            phones = []
            for g in it.get("contact_groups", []):
                for c in g.get("contacts", []):
                    if c.get("type") == "website" and not site:
                        site = c.get("text") or c.get("value") or ""
                    elif c.get("type") == "email" and not email:
                        email = (c.get("value") or "").lower()
                    elif c.get("type") == "phone":
                        phones.append(c.get("text") or c.get("value") or "")
            rev = it.get("reviews") or {}
            found.append(new_business(
                name=it.get("name", ""), site=site, email=email, phone="; ".join(x for x in phones if x),
                address=it.get("address_name", ""), rating=rev.get("general_rating"),
                reviews=rev.get("general_review_count"), sources=["2ГИС"]))
        if len(items) < 10:
            break
    return found


# ───────────────────────────── дневные лимиты платных API ─────────────────────────────
# Сколько запросов в сутки бот делает к Яндексу и 2ГИС. Значения по умолчанию — осторожные, а не тарифы этих сервисов:
# поставьте свои в config.json → limits → api_per_day ({"yandex": …, "2gis": …}); 0 — не обращаться совсем.
API_PER_DAY = {"yandex": 200, "2gis": 500}
USAGE_FILE = HERE / "api_usage.json"
_api_limits: dict[str, int] = dict(API_PER_DAY)


def set_api_limits(limits: dict | None):
    _api_limits.clear()
    _api_limits.update(API_PER_DAY)
    for k, v in (limits or {}).items():
        try:
            _api_limits[k] = int(v)
        except (TypeError, ValueError):
            pass


def _usage() -> dict:
    today = time.strftime("%Y-%m-%d")
    try:
        u = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        u = {}
    return u if u.get("day") == today else {"day": today, "used": {}, "blocked": {}}


def _save_usage(u: dict):
    try:
        USAGE_FILE.write_text(json.dumps(u, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def api_left(source: str) -> int:
    """Сколько запросов к источнику осталось сегодня. 0 — лимит исчерпан или источник сегодня отказал (ключ, 429)."""
    u = _usage()
    if u["blocked"].get(source):
        return 0
    return max(0, _api_limits.get(source, 0) - u["used"].get(source, 0))


def api_spend(source: str, n: int = 1):
    u = _usage()
    u["used"][source] = u["used"].get(source, 0) + n
    _save_usage(u)


def api_block(source: str, why: str):
    """Источник ответил «ключ не подходит» или «слишком много запросов» — до завтра его не трогаем,
    чтобы не долбить сервис и не тратить деньги на заведомо неудачные запросы."""
    u = _usage()
    u["blocked"][source] = why
    _save_usage(u)


def api_usage_text() -> str:
    u = _usage()
    parts = []
    for src, title in (("yandex", "Яндекс"), ("2gis", "2ГИС")):
        line = f"{title}: {u['used'].get(src, 0)} из {_api_limits.get(src, 0)}"
        if u["blocked"].get(src):
            line += f" (сегодня отключён: {u['blocked'][src]})"
        parts.append(line)
    return "Запросы к API сегодня — " + ", ".join(parts)


def api_keys() -> dict:
    keys = {}
    f = HERE / "keys.json"
    if f.exists():
        try:
            keys = json.loads(f.read_text(encoding="utf-8-sig"))
        except ValueError:
            pass
    return {"yandex": os.environ.get("YANDEX_ORG_API_KEY") or keys.get("yandex_org_api_key", ""),
            "2gis": os.environ.get("DGIS_API_KEY") or keys.get("dgis_api_key", "")}


def find_businesses(niche_name: str, niche: dict, city: str) -> tuple[list[dict], list[str]]:
    """Все источники сразу. Возвращает (компании, заметки об источниках для пользователя)."""
    keys = api_keys()
    groups, notes = [], []
    query = f"{niche_name} {city}"
    try:
        osm = osm_search(niche, city)
        groups.append(osm)
        notes.append(f"OpenStreetMap: {len(osm)}")
    except MapError as e:
        notes.append(f"OpenStreetMap не ответил ({e})")
    for src, name, func, key in (("yandex", "Яндекс", yandex_search, keys["yandex"]),
                                 ("2gis", "2ГИС", dgis_search, keys["2gis"])):
        if not key:
            continue
        left = api_left(src)
        if left <= 0:
            notes.append(f"{name}: дневной лимит запросов исчерпан")
            continue
        pages = min(left, 2 if src == "yandex" else 5)
        calls = [0]
        try:
            got = func(query, key, pages=pages, _count=calls)
            groups.append(got)
            notes.append(f"{name}: {len(got)}")
        except MapError as e:
            notes.append(f"{name} не ответил ({e})")
            if any(x in str(e) for x in ("401", "403", "429", "ключ")):
                api_block(src, str(e))
        finally:
            api_spend(src, max(calls[0], 1))
    if not groups:
        raise MapError("; ".join(notes))
    return merge(groups), notes


# ───────────────────────────── телефоны ─────────────────────────────
# Мобильные коды: Россия +7 9xx; Казахстан +7 700–708, 747, 771, 775–778; Беларусь +375 25, 29, 33, 44.
KZ_MOBILE = {"700", "701", "702", "705", "706", "707", "708", "747", "771", "775", "776", "777", "778"}
BY_MOBILE = {"25", "29", "33", "44"}


def phone_info(raw: str) -> dict:
    """Один номер → {"text": как показать, "intl": цифры с кодом страны или "", "mobile": bool}.
    Номер без кода города (меньше 10 цифр) не дополняем: код угадать нельзя (в Москве их два — 495 и 499)."""
    raw = (raw or "").strip()
    d = re.sub(r"\D", "", raw)
    intl = ""
    if d.startswith("375") and len(d) == 12:
        intl = d
    elif d.startswith("80") and len(d) == 11:  # Беларусь внутри страны: 8 0xx …; в России кодов на 0 нет
        intl = "375" + d[2:]
    elif len(d) == 11 and d[0] in "78":
        intl = "7" + d[1:]
    elif len(d) == 10 and d[0] != "0":
        intl = "7" + d
    if not intl:
        return {"text": raw + (" (без кода города)" if 5 <= len(d) < 10 else ""), "intl": "", "mobile": False}
    if intl.startswith("375"):
        c, n = intl[3:5], intl[5:]
        return {"text": f"+375 {c} {n[:3]}-{n[3:5]}-{n[5:]}", "intl": intl, "mobile": c in BY_MOBILE}
    c, n = intl[1:4], intl[4:]
    return {"text": f"+7 {c} {n[:3]}-{n[3:5]}-{n[5:]}", "intl": intl,
            "mobile": c.startswith("9") or c in KZ_MOBILE}


def phones(phone: str) -> list[dict]:
    """Все номера компании, мобильные — первыми (на них можно написать в WhatsApp)."""
    out = [phone_info(x) for x in re.split(r"[;,]", phone or "") if x.strip()]
    return sorted(out, key=lambda x: (not x["mobile"], not x["intl"]))


# ───────────────────────────── перепроверка «нет сайта» ─────────────────────────────
# В OpenStreetMap у многих компаний просто не заполнено поле «сайт». Прежде чем сказать «нет сайта»,
# бот ищет компанию в 2ГИС или Яндексе (если есть ключи) и пробует домены из названия — домен засчитывается,
# только если на его странице есть телефон этой компании.
DIRECTORIES = {"2ГИС", "Яндекс"}
GUESS_MAX = 40     # сколько компаний без сайта проверять доменами за один поиск
LOOKUP_MAX = 10    # сколько платных запросов к 2ГИС/Яндексу тратить на перепроверку за один поиск
TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
WORDS = {"плюс": "plus", "центр": "center", "стиль": "style", "сервис": "service", "клиника": "clinic",
         "студия": "studio", "мастер": "master", "групп": "group", "дентал": "dental", "лайн": "line"}
LEGAL = re.compile(r"\b(ооо|оао|зао|пао|ао|ип|тоо|одо|чуп)\b", re.I)
TLDS = {"RU": ("ru", "com"), "KZ": ("kz", "com"), "BY": ("by", "com")}


def city_country(city: str) -> str:
    c = city.strip().lower()
    try:
        return next((x.get("country", "RU") for x in load_cities() if x["name"].lower() == c), "RU")
    except (OSError, ValueError, KeyError):
        return "RU"


def name_slugs(name: str) -> list[str]:
    """«Дентал плюс» → ["dentalplus", "dental-plus"]. Только из букв названия, без выдумок."""
    s = LEGAL.sub(" ", name.lower().replace("ё", "е"))
    s = re.sub(r"[«»\"'`.,!?()&+/\\]", " ", s)
    words = []
    for w in s.split():
        w = WORDS.get(w, w)
        w = "".join(TRANSLIT.get(ch, ch) for ch in w)
        w = re.sub(r"[^a-z0-9-]", "", w).strip("-")
        if w:
            words.append(w)
    if not words or len("".join(words)) < 4:
        return []
    joined = "".join(words)
    return list(dict.fromkeys([joined, "-".join(words)]))[:2] if len(joined) <= 40 else []


PHONE_RE = re.compile(r"\+?\d[\d\s\-‐-―()\xa0]{5,}\d")


def page_has_phone(html: str, b: dict) -> bool:
    """На странице есть хотя бы один номер компании (сравниваем последние 10 цифр, у коротких — 7)."""
    want = set()
    for x in re.split(r"[;,]", b.get("phone", "")):
        d = re.sub(r"\D", "", x)
        if len(d) >= 10:
            want.add(d[-10:])
        elif len(d) >= 7:
            want.add(d[-7:])
    if not want:
        return False
    for m in PHONE_RE.findall(html):
        d = re.sub(r"\D", "", m)
        if d[-10:] in want or d[-7:] in want:
            return True
    return False


def guess_site(b: dict, country: str = "RU", timeout: int = 6) -> str:
    """Пробует домены из названия; возвращает адрес, только если на странице есть телефон компании."""
    for slug in name_slugs(b.get("name", "")):
        for tld in TLDS.get(country, TLDS["RU"]):
            url = f"http://{slug}.{tld}"
            try:
                r = requests.get(url, headers={"User-Agent": p.DEFAULT_UA}, timeout=timeout)
            except requests.RequestException:
                continue
            if r.status_code == 200 and page_has_phone(r.text[:500_000], b):
                return r.url
    return ""


def _same(a: dict, b: dict) -> bool:
    if set(phone_keys(a.get("phone", ""))) & set(phone_keys(b.get("phone", ""))):
        return True
    norm = lambda s: re.sub(r"[^a-zа-я0-9]", "", s.lower().replace("ё", "е"))  # noqa: E731
    return bool(norm(a.get("name", ""))) and norm(a.get("name", "")) == norm(b.get("name", ""))


def _adopt(b: dict, other: dict, via: str) -> dict:
    b = dict(b, sources=sorted(set(b["sources"]) | set(other["sources"])))
    for f in ("site", "email", "phone", "address", "hours"):
        if not b.get(f) and other.get(f):
            b[f] = other[f]
    for f in ("rating", "reviews"):
        if other.get(f) is not None and b.get(f) is None:
            b[f] = other[f]
    b["domain"] = p.normalize(b["site"]) if b["site"] else ""
    if b["site"]:
        b["site_found"] = via
    return b


def verify_no_site(businesses: list[dict], city: str) -> list[dict]:
    """Перепроверяет компании, у которых сайта нет только по данным OpenStreetMap.
    Найденный сайт дописывается в карточку (site_found — где нашли); если справочник нашёл компанию без сайта,
    он добавляется в sources — это подтверждение «нет сайта». Сеть недоступна или лимит кончился — компания остаётся как была."""
    from concurrent.futures import ThreadPoolExecutor
    todo = [i for i, b in enumerate(businesses) if not b["site"] and not (set(b["sources"]) & DIRECTORIES)]
    if not todo:
        return businesses
    out = list(businesses)
    country = city_country(city)
    guess_idx = [i for i in todo if out[i].get("phone")][:GUESS_MAX]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, site in zip(guess_idx, pool.map(lambda i: guess_site(out[i], country), guess_idx)):
            if site:
                out[i] = _adopt(out[i], new_business(site=site, sources=[]), "по телефону на сайте")
    keys = api_keys()
    budget = LOOKUP_MAX
    for i in todo:
        if out[i]["site"] or budget <= 0:
            continue
        for src, name, func in (("2gis", "2ГИС", dgis_search), ("yandex", "Яндекс", yandex_search)):
            if not keys[src] or api_left(src) <= 0:
                continue
            calls = [0]
            try:
                found = func(f"{out[i]['name']} {city}", keys[src], pages=1, _count=calls)
            except MapError as e:
                if any(x in str(e) for x in ("401", "403", "429", "ключ")):
                    api_block(src, str(e))
                continue
            finally:
                api_spend(src, max(calls[0], 1))
                budget -= 1
            match = [x for x in found if _same(out[i], x)]
            with_site = [x for x in match if x["site"]]
            if with_site:
                out[i] = _adopt(out[i], with_site[0], name)
            elif match:
                out[i] = _adopt(out[i], match[0], name)  # справочник подтвердил: сайта нет и там
            break
    return out


def no_site_confirmed(b: dict) -> bool:
    """«Нет сайта» подтверждает справочник (2ГИС или Яндекс), а не только OpenStreetMap."""
    return bool(set(b.get("sources", [])) & DIRECTORIES)


# ───────────────────────────── приоритет ─────────────────────────────
# Что видит сам владелец, весит больше всего; «невидимые» SEO-мелочи почти ничего не весят.
VISIBLE = {
    "no_viewport": (25, "нет мобильной версии"), "flash": (25, "Flash — не показывается"),
    "bad_cert": (20, "браузер ругается на сертификат"), "no_https": (18, "«Не защищено» в браузере"),
    "old_copyright": (10, "© {year} — выглядит заброшенным"), "psi_low": (8, "медленный по PageSpeed ({score})"),
    "slow": (8, "долго грузится ({seconds} с)"), "broken_links": (6, "битые ссылки ({count})"),
    "mixed_content": (4, "часть файлов по HTTP"), "old_markup": (4, "вёрстка 2000-х"),
    "heavy": (3, "тяжёлая страница"), "no_title": (3, "нет заголовка страницы"),
    "no_description": (2, "нет описания для поиска"), "cert_expiring": (3, "сертификат скоро истечёт"),
}
TIER_BONUS = {"high": 10, "mid": 5, "low": 0}
TIER_TEXT = {"high": "ниша с высоким чеком", "mid": "ниша со средним чеком", "low": "ниша с небольшим чеком"}


def site_kind(b: dict, a: p.Audit | None) -> str:
    if not b["site"]:
        return "no_site"
    if a is None:
        return "unchecked"
    if not a.ok:
        if a.error.startswith("robots"):
            return "unknown"
        return "dead_site"
    return "site"


def lead_score(b: dict, a: p.Audit | None, tier: str) -> tuple[int, list[str], str]:
    """Приоритет 0–100 и причины. Чем выше, тем вероятнее, что компании нужен сайт и она готова платить."""
    kind = site_kind(b, a)
    reasons, score = [], 0
    if kind == "no_site" and no_site_confirmed(b):
        score += 45
        reasons.append("нет сайта")
    elif kind == "no_site":
        # только OpenStreetMap: там поле «сайт» часто просто не заполнено — ниже в списке, пока не проверено
        score += 30
        reasons.append("сайта нет в OpenStreetMap (не перепроверено)")
    elif kind == "dead_site":
        if a.error.startswith(("сайт отвечает ошибкой", "заглушка")):
            score += 40
            reasons.append(f"сайт не работает ({a.error})")
        else:
            # сетевая ошибка: сайт мог заблокировать зарубежный адрес сервера — это ещё не значит, что он не работает
            score += 20
            reasons.append(f"сайт не открылся с сервера ({a.error}) — проверьте с телефона")
    elif kind == "site":
        site_pts = 0
        for k in sorted(a.issues, key=lambda k: -VISIBLE.get(k, (0, ""))[0]):
            w, text = VISIBLE.get(k, (0, ""))
            site_pts += w
            if w >= 4 and len(reasons) < 3:
                reasons.append(text.format(**a.issues[k]))
        score += min(site_pts, 45)
    # активность бизнеса: живые компании с отзывами чаще платят за сайт
    rv = b.get("reviews") or 0
    if rv >= 50:
        score += 20
        reasons.append(f"{rv} отзывов — активный бизнес")
    elif rv >= 15:
        score += 12
        reasons.append(f"{rv} отзывов")
    elif rv >= 5:
        score += 6
    if (b.get("rating") or 0) >= 4.5 and rv >= 5:
        score += 5
    if b.get("hours"):
        score += 3
    # контакты
    has_email = bool(b.get("email") or (a and a.emails))
    if has_email:
        score += 10
    elif b.get("phone"):
        score += 5
    else:
        reasons.append("контактов нет")
    score += TIER_BONUS.get(tier, 5)
    if len(b.get("sources", [])) >= 2:
        score += 3  # компания есть в нескольких справочниках — данные свежее
    status, why = activity(b, a)
    if status == "stale":
        score -= 15  # давно никто не подтверждал, что компания на месте: письмо может уйти в пустоту
        reasons.append(f"могла закрыться ({why})")
    if not has_email and not b.get("phone"):
        score = min(score, 20)  # написать некуда — как бы ни был плох сайт
    return max(min(score, 100), 0), reasons, kind


# ───────────────────────────── рекомендация по цене ─────────────────────────────
PRICING_FILE = HERE / "pricing.json"


def load_pricing() -> dict:
    return json.loads(PRICING_FILE.read_text(encoding="utf-8-sig"))


def _rng(r: list[int], k: float) -> str:
    lo, hi = (round(x * k / 1000) * 1000 for x in r)
    return f"{lo // 1000}–{hi // 1000} тыс. ₽"


def city_factor(city: str, pr: dict) -> tuple[float, str]:
    c = city.strip().lower()
    for name, k in pr["city"].items():
        if name not in ("million", "other") and name.lower() == c:
            return k, name
    if c in (x.lower() for x in pr["million_cities"]):
        return pr["city"]["million"], "город-миллионник"
    return pr["city"]["other"], "небольшой город"


def price_hint(b: dict, a: p.Audit | None, kind: str, tier: str, city: str, pr: dict | None = None) -> str:
    """Рекомендация, что предложить и за сколько. Правила и цифры — в pricing.json, их стоит подстроить под себя."""
    pr = pr or load_pricing()
    k, city_txt = city_factor(city, pr)
    why = [TIER_TEXT.get(tier, ""), city_txt]
    big = pr["big_business"]
    if (b.get("reviews") or 0) >= big["min_reviews"]:
        k *= big["multiplier"]
        why.append(f"крупный: {b['reviews']} отзывов")
    new, fix = _rng(pr["new_site"][tier], k), _rng(pr["fix_site"][tier], k)
    support = f"{pr['support_month'][0]}–{pr['support_month'][1]} ₽/мес"
    issues = set(a.issues) if a and a.ok else set()
    if kind == "no_site":
        offer = f"сайт-визитка по эталону ≈ {new}, поддержка {support}"
    elif kind == "dead_site":
        offer = f"новый сайт ≈ {new} (сначала спросите, нужен ли им сайт — могли забросить)"
    elif issues & {"no_viewport", "flash"} or {"old_markup", "old_copyright"} <= issues:
        offer = f"новый сайт ≈ {new}: старый под телефон не переделать дёшево"
    elif issues & {"no_https", "bad_cert", "slow", "broken_links", "mixed_content", "psi_low", "cert_expiring"}:
        offer = f"подлатать (HTTPS, скорость, ссылки) ≈ {fix}; новый сайт ≈ {new}"
    else:
        offer = f"сайт в порядке; можно предложить поддержку {support}"
    return f"{offer}. Почему: {', '.join(x for x in why if x)}"
