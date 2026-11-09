"""Тесты источников, ниш, приоритета и рекомендации по цене. Сеть подменена."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import leads  # noqa: E402
import prospector as p  # noqa: E402


def resp(data, status=200):
    r = mock.Mock(status_code=status, text=json.dumps(data, ensure_ascii=False),
                  headers={"content-type": "application/json"})
    r.json.return_value = data
    return r


NOMINATIM = resp([{"boundingbox": ["54.8", "55.1", "73.1", "73.6"]}])


def audit(**issues):
    a = p.Audit(input="x", domain="x.ru", ok=True, url="https://x.ru/", https=True)
    for k, v in issues.items():
        a.add(k, **v)
    return p.finish(a)


class Niches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(leads, "CUSTOM_NICHES_FILE", Path(self.tmp.name) / "custom.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_default_niches(self):
        n = leads.load_niches()
        self.assertGreaterEqual(len(n), 50)
        for name, d in n.items():
            self.assertIn(d["tier"], ("low", "mid", "high"), name)
            self.assertTrue(d["osm"] or d["keywords"], name)

    def test_parse_request(self):
        n = leads.load_niches()
        self.assertEqual(leads.parse_request("Кузовной ремонт  Омск", n), ("кузовной ремонт", "Омск"))
        self.assertEqual(leads.parse_request("барбершоп Нижний Новгород", n), ("парикмахерская", "Нижний Новгород"))
        self.assertEqual(leads.parse_request("стоматолог Москва", n), ("стоматология", "Москва"))
        self.assertIsNone(leads.parse_request("привет", n))
        self.assertIsNone(leads.parse_request("стоматология", n))

    def test_custom_niche(self):
        leads.add_custom_niche("Натяжные потолки премиум", ["потолк", "натяжн"], "high")
        n = leads.load_niches()
        self.assertEqual(n["натяжные потолки премиум"]["tier"], "high")
        self.assertEqual(leads.parse_request("натяжные потолки премиум Тула", n), ("натяжные потолки премиум", "Тула"))
        leads.add_custom_niche("Что-то", ["что-то"], "огромный")
        self.assertEqual(leads.load_niches()["что-то"]["tier"], "mid")

    def test_overpass_query(self):
        q = leads.overpass_query({"osm": ["amenity=dentist"], "keywords": ["стомат", "дентал"]}, (54.8, 73.1, 55.1, 73.6))
        self.assertIn('nwr["amenity"="dentist"](54.80000,73.10000,55.10000,73.60000);', q)
        self.assertIn('nwr["shop"]["name"~"[Сс]томат|[Дд]ентал"]', q)
        self.assertTrue(q.endswith("out tags 400;"))
        self.assertNotIn("website", q)  # теперь ищем и компании без сайта


class Sources(unittest.TestCase):
    def test_osm_search(self):
        overpass = resp({"elements": [
            {"tags": {"name": "Зубок", "website": "http://zyb-ok.ru/", "email": "Zyb@mail.ru"}},
            {"tags": {"name": "Улыбка", "phone": "+7 (3812) 11-22-33", "opening_hours": "Mo-Fr 09:00-18:00",
                      "addr:street": "улица Ленина", "addr:housenumber": "5"}},
            {"tags": {"name": "ГАУЗ Стоматология № 9", "website": "https://stom9.ru"}},
            {"tags": {"name": "Сеть 1", "website": "https://set.ru"}},
            {"tags": {"name": "Сеть 2", "website": "https://set.ru/"}},
            {"tags": {"website": "https://noname.ru"}},
        ]})
        with mock.patch.object(leads.requests, "get", return_value=NOMINATIM), \
                mock.patch.object(leads.requests, "post", return_value=overpass):
            found = leads.osm_search({"osm": ["amenity=dentist"], "keywords": []}, "Омск")
        self.assertEqual([b["name"] for b in found], ["Зубок", "Улыбка"])
        self.assertEqual(found[0]["domain"], "zyb-ok.ru")
        self.assertEqual(found[0]["email"], "zyb@mail.ru")
        self.assertEqual(found[1]["site"], "")
        self.assertEqual(found[1]["address"], "улица Ленина 5")
        self.assertEqual(found[1]["sources"], ["OSM"])

    def test_yandex_search(self):
        data = {"features": [
            {"properties": {"CompanyMetaData": {"name": "Центр Кузовного Ремонта", "url": "http://kuzovomsk.ru/",
                                                "address": "Омск, Заводская ул., 1/2",
                                                "Phones": [{"formatted": "+7 (913) 977-45-07"}],
                                                "Hours": {"text": "пн-пт 9:00–18:00"}}}},
            {"properties": {"CompanyMetaData": {"name": "Гараж Петровича", "Phones": [{"formatted": "+7 900 1"}]}}},
        ]}
        with mock.patch.object(leads.requests, "get", return_value=resp(data)) as get:
            found = leads.yandex_search("кузовной ремонт Омск", "KEY")
        kw = get.call_args.kwargs["params"]
        self.assertEqual((kw["apikey"], kw["type"], kw["text"]), ("KEY", "biz", "кузовной ремонт Омск"))
        self.assertEqual(found[0]["domain"], "kuzovomsk.ru")
        self.assertEqual(found[0]["hours"], "пн-пт 9:00–18:00")
        self.assertEqual(found[1]["site"], "")
        self.assertEqual(get.call_count, 1)  # меньше 50 результатов — вторую страницу не просим

    def test_dgis_search(self):
        page1 = {"meta": {"code": 200}, "result": {"items": [
            {"name": "Центр кузовного ремонта", "address_name": "Заводская, 1/2",
             "reviews": {"general_rating": 4.6, "general_review_count": 64},
             "contact_groups": [{"contacts": [
                 {"type": "website", "text": "kuzovomsk.ru", "url": "http://link.2gis.ru/abc"},
                 {"type": "phone", "text": "+7 (913) 977-45-07", "value": "+79139774507"},
                 {"type": "email", "value": "Kuzov@mail.ru"}]}]}] * 10}}
        end = {"meta": {"code": 404, "error": {"message": "Results not found"}}}
        with mock.patch.object(leads.requests, "get", side_effect=[resp(page1), resp(end, 404)]):
            found = leads.dgis_search("кузовной ремонт Омск", "KEY")
        self.assertEqual(len(found), 10)
        b = found[0]
        self.assertEqual((b["domain"], b["email"], b["rating"], b["reviews"]), ("kuzovomsk.ru", "kuzov@mail.ru", 4.6, 64))
        self.assertIn("977-45-07", b["phone"])

    def test_dgis_bad_key(self):
        with mock.patch.object(leads.requests, "get", return_value=resp({"meta": {"code": 403}}, 403)), \
                self.assertRaises(leads.MapError) as cm:
            leads.dgis_search("q", "BAD")
        self.assertIn("2ГИС", str(cm.exception))

    def test_merge(self):
        osm = [leads.new_business(name="Центр Кузовного Ремонта", site="http://kuzovomsk.ru/", sources=["OSM"]),
               leads.new_business(name="Гараж", phone="+7 (900) 111-22-33", sources=["OSM"])]
        gis = [leads.new_business(name="Центр кузовного ремонта", site="kuzovomsk.ru", phone="+7 913 977-45-07",
                                  reviews=64, rating=4.6, sources=["2ГИС"]),
               leads.new_business(name="Гараж Петровича", phone="8 900 111 22 33", address="Заводская 3", sources=["2ГИС"])]
        merged = leads.merge([osm, gis])
        self.assertEqual(len(merged), 2)
        kuzov = next(b for b in merged if b["domain"] == "kuzovomsk.ru")
        self.assertEqual((kuzov["reviews"], kuzov["sources"]), (64, ["2ГИС", "OSM"]))
        garage = next(b for b in merged if not b["domain"])
        self.assertEqual(garage["address"], "Заводская 3")

    def test_keep(self):
        nb = leads.new_business
        self.assertTrue(leads.keep(nb(name="А", phone="+7 900 000 00 00"), set()))
        self.assertFalse(leads.keep(nb(name="Без контактов"), set()))
        self.assertFalse(leads.keep(nb(name="Портал", site="https://zdrav.tatar.ru/kzn/dsp6"), set()))
        self.assertFalse(leads.keep(nb(name="Авито", site="https://www.avito.ru/x"), set()))
        self.assertFalse(leads.keep(nb(name="Был", site="zyb-ok.ru"), {"zyb-ok.ru"}))
        self.assertFalse(leads.keep(nb(name="Был", phone="+7 900 000 00 00"), {"tel:9000000000"}))

    def test_find_businesses_combines_and_survives_failures(self):
        with mock.patch.object(leads, "api_keys", return_value={"yandex": "", "2gis": "K"}), \
                mock.patch.object(leads, "osm_search", side_effect=leads.MapError("OSM: HTTP 504")), \
                mock.patch.object(leads, "dgis_search", return_value=[leads.new_business(name="Б", phone="1234567890", sources=["2ГИС"])]) as dg:
            found, notes = leads.find_businesses("автосервис", {"osm": [], "keywords": []}, "Омск")
        self.assertEqual([b["name"] for b in found], ["Б"])
        self.assertEqual(dg.call_args.args[0], "автосервис Омск")
        self.assertTrue(any("OpenStreetMap не ответил" in n for n in notes))
        self.assertIn("2ГИС: 1", notes)
        with mock.patch.object(leads, "api_keys", return_value={"yandex": "", "2gis": ""}), \
                mock.patch.object(leads, "osm_search", side_effect=leads.MapError("OSM: HTTP 504")), \
                self.assertRaises(leads.MapError):
            leads.find_businesses("автосервис", {"osm": [], "keywords": []}, "Омск")

    def test_overpass_mirror_fallback_and_errors(self):
        busy = mock.Mock(status_code=504, text="timeout")
        good = resp({"elements": [{"tags": {"name": "А", "website": "https://a-dent.ru"}}]})
        with mock.patch.object(leads.requests, "get", return_value=NOMINATIM), \
                mock.patch.object(leads.requests, "post", side_effect=[busy, good]) as post:
            found = leads.osm_search({"osm": ["amenity=dentist"], "keywords": []}, "Казань")
        self.assertEqual(found[0]["domain"], "a-dent.ru")
        self.assertEqual(post.call_args_list[1].args[0], leads.OVERPASS_URLS[1])
        denied = mock.Mock(status_code=403, text="blocked")
        with mock.patch.object(leads.requests, "get", return_value=denied), mock.patch.object(leads.time, "sleep"), \
                self.assertRaises(leads.MapError) as cm:
            leads.find_city("Казань")
        self.assertIn("HTTP 403", str(cm.exception))


class Scoring(unittest.TestCase):
    nb = staticmethod(leads.new_business)

    def test_visible_problems_beat_seo_trifles(self):
        b = self.nb(name="А", site="x.ru", email="a@x.ru")
        mobile, _, _ = leads.lead_score(b, audit(no_viewport={}, no_https={}), "mid")
        seo, reasons, _ = leads.lead_score(b, audit(no_description={}, no_title={}), "mid")
        self.assertGreater(mobile, seo + 30)
        self.assertNotIn("нет описания для поиска", reasons)  # мелочь не попадает в причины

    def test_no_site_and_dead_site(self):
        s, reasons, kind = leads.lead_score(self.nb(name="Б", phone="+7 900 000 00 00"), None, "high")
        self.assertEqual((kind, reasons[0]), ("no_site", "нет сайта"))
        self.assertGreaterEqual(s, 55)
        dead = p.Audit(input="x", domain="x.ru", error="заглушка: домен продаётся или сайт не работает")
        s2, r2, k2 = leads.lead_score(self.nb(name="В", site="x.ru", phone="1"), dead, "mid")
        self.assertEqual(k2, "dead_site")
        robots = p.Audit(input="x", domain="x.ru", error="robots.txt запрещает обход")
        self.assertEqual(leads.lead_score(self.nb(name="Г", site="x.ru", phone="1"), robots, "mid")[2], "unknown")

    def test_activity_and_contacts(self):
        quiet = self.nb(name="А", phone="1")
        busy = self.nb(name="А", phone="1", reviews=80, rating=4.7)
        self.assertGreater(leads.lead_score(busy, None, "mid")[0], leads.lead_score(quiet, None, "mid")[0] + 20)
        no_contacts = self.nb(name="А", reviews=200)
        s, reasons, _ = leads.lead_score(no_contacts, None, "high")
        self.assertLessEqual(s, 20)
        self.assertIn("контактов нет", reasons)

    def test_tier_matters(self):
        b = self.nb(name="А", phone="1")
        self.assertGreater(leads.lead_score(b, None, "high")[0], leads.lead_score(b, None, "low")[0])


class Pricing(unittest.TestCase):
    pr = {"new_site": {"low": [12000, 20000], "mid": [15000, 28000], "high": [20000, 40000]},
          "fix_site": {"low": [4000, 8000], "mid": [5000, 10000], "high": [7000, 14000]},
          "support_month": [1000, 3000], "city": {"Москва": 1.4, "million": 1.0, "other": 0.8},
          "million_cities": ["Омск"], "big_business": {"min_reviews": 50, "multiplier": 1.2}}

    def hint(self, b, a, kind, tier, city):
        return leads.price_hint(b, a, kind, tier, city, self.pr)

    def test_no_site(self):
        h = self.hint(leads.new_business(name="А"), None, "no_site", "high", "Москва")
        self.assertIn("сайт-визитка", h)
        self.assertIn("28–56 тыс. ₽", h)  # 20–40 × 1,4
        self.assertIn("ниша с высоким чеком", h)

    def test_city_and_size(self):
        self.assertIn("15–28 тыс. ₽", self.hint(leads.new_business(name="А"), None, "no_site", "mid", "омск"))
        self.assertIn("12–22 тыс. ₽", self.hint(leads.new_business(name="А"), None, "no_site", "mid", "Тара"))
        big = self.hint(leads.new_business(name="А", reviews=120), None, "no_site", "mid", "Омск")
        self.assertIn("18–34 тыс. ₽", big)
        self.assertIn("120 отзывов", big)

    def test_fix_or_new(self):
        b = leads.new_business(name="А", site="x.ru")
        self.assertIn("новый сайт", self.hint(b, audit(no_viewport={}), "site", "mid", "Омск").split(":")[0])
        fix = self.hint(b, audit(no_https={}), "site", "mid", "Омск")
        self.assertTrue(fix.startswith("подлатать"))
        self.assertIn("5–10 тыс. ₽", fix)
        self.assertIn("поддержку", self.hint(b, audit(), "site", "mid", "Омск"))

    def test_default_pricing_file_loads(self):
        pr = leads.load_pricing()
        for kind in ("new_site", "fix_site"):
            for tier in ("low", "mid", "high"):
                lo, hi = pr[kind][tier]
                self.assertLess(lo, hi)


if __name__ == "__main__":
    unittest.main()
