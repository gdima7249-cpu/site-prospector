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


def business_key(b: dict) -> str:
    return b.get("domain") or (("tel:" + phone_key(b["phone"])) if phone_key(b.get("phone", "")) else "") \
        or "name:" + b.get("name", "").strip().lower()


def new_business(**kw) -> dict:
    b = {"name": "", "site": "", "domain": "", "email": "", "phone": "", "address": "", "hours": "",
         "rating": None, "reviews": None, "sources": []}
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
            pk = phone_key(b.get("phone", ""))
            if key not in out and pk and pk in by_phone:
                key = by_phone[pk]
            if key not in out:
                out[key] = dict(b, sources=list(b["sources"]))
            else:
                cur = out[key]
                for f in ("name", "site", "domain", "email", "phone", "address", "hours"):
                    if not cur.get(f) and b.get(f):
                        cur[f] = b[f]
                for f in ("rating", "reviews"):
                    if b.get(f) is not None and (cur.get(f) is None or b[f] > cur[f]):
                        cur[f] = b[f]
                cur["sources"] = sorted(set(cur["sources"]) | set(b["sources"]))
            if pk:
                by_phone.setdefault(pk, key)
    return list(out.values())


def keep(b: dict, skip: set[str]) -> bool:
    if b["domain"] and (p.root_domain_skipped(b["domain"], p.SKIP_DOMAINS) or b["domain"] in skip
                        or b["domain"].startswith(("zdrav.", "minzdrav.")) or ".gov." in b["domain"]):
        return False
    if b["site"] and is_portal_page(b["site"]):
        return False
    if business_key(b) in skip:
        return False
    if not b["site"] and not b["phone"] and not b["email"]:
        return False  # без сайта и без контактов предложить нечего
    return True


# ───────────────────────────── OpenStreetMap ─────────────────────────────
def find_city(city: str) -> tuple[float, float, float, float] | None:
    """Границы города (юг, запад, север, восток) через Nominatim."""
    params = {"q": city, "format": "json", "limit": 1, "accept-language": "ru", "countrycodes": "ru,by,kz,ua"}
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
    return f"[out:json][timeout:{timeout}];\n(\n" + "\n".join(parts) + f"\n);\nout tags {200 if light else limit};"


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
        found.append(new_business(
            name=name, site=site, email=(t.get("email") or t.get("contact:email") or "").lower(),
            phone=t.get("phone") or t.get("contact:phone") or "", address=street,
            hours=t.get("opening_hours", ""), sources=["OSM"]))
    _OSM_CACHE[cache_key] = (time.time(), [dict(b, sources=list(b["sources"])) for b in found])
    return found


# ───────────────────────────── Яндекс ─────────────────────────────
def yandex_search(query: str, api_key: str, pages: int = 2) -> list[dict]:
    """API поиска по организациям Яндекса: до 50 компаний за запрос. Формат ответа — GeoJSON."""
    found = []
    for page in range(pages):
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
def dgis_search(query: str, api_key: str, pages: int = 5) -> list[dict]:
    """Catalog API 2ГИС: по 10 компаний на страницу, с рейтингом и числом отзывов."""
    found = []
    for page in range(1, pages + 1):
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
    for name, func, key in (("Яндекс", yandex_search, keys["yandex"]), ("2ГИС", dgis_search, keys["2gis"])):
        if not key:
            continue
        try:
            got = func(query, key)
            groups.append(got)
            notes.append(f"{name}: {len(got)}")
        except MapError as e:
            notes.append(f"{name} не ответил ({e})")
    if not groups:
        raise MapError("; ".join(notes))
    return merge(groups), notes


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
    if kind == "no_site":
        score += 45
        reasons.append("нет сайта")
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
    if not has_email and not b.get("phone"):
        score = min(score, 20)  # написать некуда — как бы ни был плох сайт
    return min(score, 100), reasons, kind


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
