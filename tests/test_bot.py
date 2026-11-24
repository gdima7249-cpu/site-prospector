"""Тесты бота: Telegram, источники и почта подменены, сайты — на локальном сервере."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot  # noqa: E402
import leads  # noqa: E402

# служебные файлы источников (кэш городов, счётчик запросов к API) — во временной папке, не в проекте
_TMP = tempfile.mkdtemp()
leads.CITY_CACHE_FILE = Path(_TMP) / "city_cache.json"
leads.USAGE_FILE = Path(_TMP) / "api_usage.json"
from test_prospector import H  # noqa: E402


CONFIG = {"smtp": {"host": "smtp.test", "port": 465, "user": "me@my.ru", "password": "x"},
          "imap": {"host": "imap.test"},
          "limits": {"per_day": 20, "min_delay_sec": 0, "max_bounces_per_day": 3},
          "demo": {"public_url": "http://1.2.3.4:8080/", "port": 8080},
          "schedule": {"time": "09:00", "tz": "Europe/Moscow", "searches": ["кузовной ремонт Омск"], "max_cards": 5}}


class BotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def handler(root):
            class R(H):
                def do_GET(self):
                    if self.path == "/":
                        self.path = root
                    H.do_GET(self)
                do_HEAD = do_GET
            return R
        cls.servers = []
        for root in ("/old/", "/good/"):
            srv = ThreadingHTTPServer(("127.0.0.1", 0), handler(root))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            cls.servers.append(srv)
        cls.old = f"http://127.0.0.1:{cls.servers[0].server_port}/"
        cls.good = f"http://localhost:{cls.servers[1].server_port}/"

    @classmethod
    def tearDownClass(cls):
        for srv in cls.servers:
            srv.shutdown()

    def setUp(self, config=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [mock.patch.object(bot, "STATE", self.dir / "state.json"),
                        mock.patch.object(bot, "HERE", self.dir),
                        mock.patch.object(leads, "CUSTOM_NICHES_FILE", self.dir / "custom.json"),
                        mock.patch.object(bot, "spawn", lambda target, *args: target(*args)),
                        mock.patch.object(bot.time, "sleep"),
                        mock.patch.object(bot.demo_pdf, "render_pdf", side_effect=self.fake_pdf),
                        mock.patch.object(bot.demo_pdf, "available", return_value=True),
                        mock.patch.object(leads, "verify_no_site", side_effect=lambda bs, city: bs)]
        for x in self.patches:
            x.start()
        self.make_bot(json.loads(json.dumps(CONFIG)) if config is None else config)

    @staticmethod
    def fake_pdf(root, token, name, out, chromium_path=None):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"%PDF-1.4 fake " + name.encode())
        return out

    def make_bot(self, config):
        self.b = bot.Bot("TOKEN", config)
        self.out, self.files, self.api = [], [], []
        orig_say = self.b.say

        def call(method, timeout=60, **params):
            self.api.append((method, params))
            if method == "sendMessage":
                self.out.append((params["text"], params.get("reply_markup")))
        self.b.call = call
        self.b.send_file = lambda chat, name, content, caption="": self.files.append((name, content))
        self.say = orig_say

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    @property
    def last(self):
        return self.out[-1][0]

    def buttons(self, i=-1):
        m = self.out[i][1]
        return [btn.get("callback_data") or btn["url"] for row in json.loads(m)["inline_keyboard"] for btn in row] if m else []

    def press(self, data, user=42):
        self.b.handle_callback({"id": "q", "from": {"id": user}, "data": data,
                                "message": {"chat": {"id": 1}, "message_id": 7}})

    def businesses(self):
        nb = leads.new_business
        return [nb(name="Ромашка", site=self.old, email="boss@romashka.ru", sources=["OSM"]),
                nb(name="Хорошая", site=self.good, phone="+7 900 111 11 11", sources=["OSM"]),
                nb(name="Гараж Петровича", phone="+7 (913) 222-33-44", reviews=70, rating=4.8,
                   hours="Mo-Fr 09:00-18:00", address="Заводская улица, 3", sources=["2ГИС"]),
                nb(name="Без контактов", sources=["OSM"])]

    def run_search(self, niche="кузовной ремонт", city="Омск"):
        with mock.patch.object(leads, "find_businesses", return_value=(self.businesses(), ["OpenStreetMap: 3"])):
            self.b.handle(1, 42, f"{niche} {city}")

    def lead(self, name):
        return next(L for L in self.b.st["leads"].values() if L["b"]["name"] == name)

    # ——— автоотправка ———
    def auto_businesses(self):
        nb = leads.new_business
        return [nb(name="Ромашка", site=self.old, email="boss@romashka.ru", sources=["OSM"]),        # сайт открывается
                nb(name="Гараж Петровича", phone="+7 (913) 222-33-44", reviews=70, sources=["2ГИС"]),  # нет email
                nb(name="Личный", phone="+7 900 777 88 99", email="ivan.petrov@mail.ru", sources=["2ГИС"]),
                nb(name="Только OSM", phone="+7 900 666 55 44", email="info@only-osm.ru", sources=["OSM"])]

    def auto_search(self):
        with mock.patch.object(leads, "find_businesses", return_value=(self.auto_businesses(), ["test"])), \
                mock.patch.object(leads, "verify_no_site", side_effect=lambda bs, city: bs), \
                mock.patch.object(bot.mailer, "send") as send:
            self.b.search(1, "кузовной ремонт", "Омск", quiet=True)
        return send

    def test_auto_send_only_confirmed(self):
        self.b.handle(1, 42, "/start")
        self.b.handle(1, 42, "/auto on")
        self.assertIn("Автоотправка включена", self.last)
        send = self.auto_search()
        self.assertEqual([c.args[1] for c in send.call_args_list], ["boss@romashka.ru"])  # только «Ромашка»
        r = self.lead("Ромашка")
        self.assertEqual((r["status"], r.get("auto")), ("sent", True))
        self.assertIn("Прикладываю", send.call_args.args[3])
        self.assertEqual(send.call_args.args[4][0][0], "Сайт-Ромашка.pdf")
        cards = [t for t, m in self.out if m]
        self.assertFalse(any("Ромашка" in t for t in cards))  # отправленной — карточки нет
        for name in ("Гараж Петровича", "Личный", "Только OSM"):
            self.assertTrue(any(name in t for t in cards), name)  # остальные — карточками, как раньше
        self.assertEqual(self.files[-1][0], "Сайт-Ромашка.pdf")  # отчёт с PDF

    def test_auto_report_and_checks(self):
        self.b.handle(1, 42, "/start")
        self.b.handle(1, 42, "/auto on")
        sent_reports = []
        orig = self.b.send_file
        self.b.send_file = lambda chat, name, content, caption="": sent_reports.append(caption) or orig(chat, name, content, caption)
        self.auto_search()
        self.assertIn("🤖 Отправил сам: «Ромашка» → boss@romashka.ru", sent_reports[-1])
        self.assertIn("почему уверен, что работает: сайт открывается", sent_reports[-1])
        L = {"b": leads.new_business(name="X", email="ivan.petrov@mail.ru", sources=["2ГИС"]), "kind": "no_site",
             "score": 80, "a": None}
        self.assertEqual(self.b.auto_check(L), (False, "email похож на личный"))
        L["b"]["email"] = "info@x.ru"
        self.assertEqual(self.b.auto_check(L), (True, "есть в 2ГИС"))
        L["kind"] = "dead_site"
        self.assertFalse(self.b.auto_check(L)[0])
        L.update(kind="no_site", score=30)
        self.assertEqual(self.b.auto_check(L), (False, "приоритет ниже порога"))
        L.update(score=80, b=leads.new_business(name="X", email="info@x.ru", sources=["OSM"]))
        self.assertEqual(self.b.auto_check(L)[1], "нет подтверждения, что компания работает")
        self.assertTrue(bot.is_branch({"domain": "omsk.fitauto.ru"}))
        self.assertFalse(bot.is_branch({"domain": "kuzovomsk.ru"}))
        self.assertFalse(bot.is_branch({"domain": "romashka.com.ru"}))
        self.assertFalse(bot.is_branch({"domain": "127.0.0.1"}))

    def test_auto_off_paused_limit_manual(self):
        self.b.handle(1, 42, "/start")
        self.assertEqual(self.auto_search().call_count, 0)  # по умолчанию выключено
        self.b.handle(1, 42, "/auto on")
        self.b.handle(1, 42, "/pause")
        self.assertEqual(self.auto_search().call_count, 0)  # пауза
        self.b.handle(1, 42, "/resume")
        self.b.cfg["limits"]["per_day"] = 0
        self.assertEqual(self.auto_search().call_count, 0)  # лимит
        self.b.cfg["limits"]["per_day"] = 20
        with mock.patch.object(bot.mailer, "send") as send:  # ручной поиск — всегда карточки
            self.run_search()
        send.assert_not_called()
        self.b.handle(1, 42, "/status")
        self.assertIn("Автоотправка: 🤖 включена", self.last)
        self.b.handle(1, 42, "/auto off")
        self.assertFalse(self.b.auto_on())

    def test_auto_skips_when_pdf_failed(self):
        self.b.handle(1, 42, "/start")
        self.b.handle(1, 42, "/auto on")
        with mock.patch.object(bot.demo_pdf, "render_pdf", side_effect=RuntimeError("crash")):
            send = self.auto_search()
        send.assert_not_called()
        self.assertTrue(any("Ромашка" in t for t, m in self.out if m))  # пришла карточкой

    # ———
    def test_owner_only(self):
        self.b.handle(1, 42, "/start")
        self.assertEqual((self.b.st["owner"], self.b.st["owner_chat"]), (42, 1))
        self.b.handle(1, 99, "/help")
        self.assertEqual(len(self.out), 1)
        self.press("skip:whatever", user=99)
        self.assertEqual(len(self.out), 1)

    def test_cards_with_buttons(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        cards = [(t, m) for t, m in self.out if m]
        self.assertTrue(cards[0][0].startswith("1. Гараж Петровича"))
        g = self.lead("Гараж Петровича")
        self.assertEqual(self.buttons(self.out.index(cards[0])), [f"demo:{g['id']}", f"skip:{g['id']}"])
        self.assertTrue(any("Ромашка" in t for t, _ in cards))
        self.assertFalse(any("Хорошая" in t or "Без контактов" in t for t, _ in cards))
        self.assertEqual(self.files[0][0], "кузовной_ремонт-Омск.csv")
        self.assertEqual(list(self.b.demo_root.glob("*/index.html")), [])  # до кнопки демо не собирается

    def test_demo_then_send(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        self.press(f"demo:{r['id']}")
        demo = self.b.demo_root / r["demo"] / "index.html"
        self.assertTrue(demo.exists())
        html = demo.read_text(encoding="utf-8")
        self.assertIn("Ромашка", html)
        self.assertIn("noindex", html)
        self.assertIn(f"http://1.2.3.4:8080/{r['demo']}/", self.last)  # ссылка в письме и в шапке
        self.assertIn("Кому: boss@romashka.ru", self.last)
        self.assertEqual(self.buttons()[0], f"send:{r['id']}")
        self.assertIn(("editMessageReplyMarkup", {"chat_id": 1, "message_id": 7,
                                                  "reply_markup": '{"inline_keyboard": []}'}), self.api)
        with mock.patch.object(bot.mailer, "send") as send:
            self.press(f"send:{r['id']}")
            to, subject, body, pdfs = send.call_args.args[1:]
        self.assertEqual(to, "boss@romashka.ru")
        self.assertIn(r["demo"], body)
        self.assertIn("Прикладываю, как это может выглядеть", body)
        self.assertEqual(pdfs[0][0], "Сайт-Ромашка.pdf")
        self.assertTrue(pdfs[0][1].startswith(b"%PDF"))
        self.assertIn("Отправлено", self.last)
        self.assertEqual(r["status"], "sent")
        with mock.patch.object(bot.mailer, "send") as send:
            self.press(f"send:{r['id']}")
        send.assert_not_called()
        self.assertIn("уже отправлено", self.last)

    def test_pdf_without_public_url(self):
        self.b.cfg["demo"] = {"pdf": True}  # без домена: только PDF, без ссылки
        self.b.demo_url = ""
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        self.press(f"demo:{r['id']}")
        self.assertTrue(r.get("demo"))
        self.assertEqual(self.files[-1][0], "Сайт-Ромашка.pdf")  # PDF пришёл в Telegram на проверку
        self.assertIn("📎 К письму будет приложен PDF", self.last)
        self.assertNotIn("http://", self.last)
        self.assertIn("Прикладываю, как это может выглядеть", r["draft"]["body"])

    def test_pdf_failure_is_reported(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        with mock.patch.object(bot.demo_pdf, "render_pdf", side_effect=ModuleNotFoundError("playwright")):
            self.press(f"demo:{r['id']}")
        self.assertIn("PDF собрать не удалось", self.last)
        self.assertIn(f"http://1.2.3.4:8080/{r['demo']}/", r["draft"]["body"])  # ссылка остаётся
        self.assertNotIn("PDF", r["draft"]["body"])

    def test_no_browser_on_server(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        with mock.patch.object(bot.demo_pdf, "available", return_value=False), \
                mock.patch.object(bot.demo_pdf, "render_pdf") as render:
            self.press(f"demo:{r['id']}")
            render.assert_not_called()
            self.b.handle(1, 42, "/status")
        self.assertIn("PDF с демо: нет браузера на сервере", self.last)
        self.assertIn(f"http://1.2.3.4:8080/{r['demo']}/", r["draft"]["body"])  # остаётся ссылка
        self.b.demo_url = ""
        g = self.lead("Гараж Петровича")
        with mock.patch.object(bot.demo_pdf, "available", return_value=False):
            self.press(f"demo:{g['id']}")
        self.assertIn("Письмо — без демо", self.out[-2][0])
        self.assertIn("Могу подготовить вариант сайта", g["draft"]["body"])

    def test_letter_template(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        g, r = self.lead("Гараж Петровича"), self.lead("Ромашка")
        self.press(f"demo:{g['id']}")
        d = g["draft"]
        self.assertEqual(d["subject"], "Сайт для «Гараж Петровича»")
        self.assertIn("Нашёл «Гараж Петровича» на карте и заметил, что у вас пока нет сайта.", d["body"])
        self.assertIn("А многие ищут кузовной ремонт именно с телефона.", d["body"])
        self.assertIn("Прикладываю, как это может выглядеть, а с телефона его можно открыть по ссылке: http://1.2.3.4:8080/", d["body"])
        self.assertIn("Хорошего дня!", d["body"])
        self.assertNotIn("Если не актуально", d["body"])
        self.assertNotIn("{", d["body"])
        self.press(f"demo:{r['id']}")
        self.assertIn("ваш сайт неудобно открывать с телефона", r["draft"]["body"])  # у «Ромашки» нет мобильной версии
        # свой текст в letter.txt
        custom = self.dir / "letter.txt"
        custom.write_text("=== письмо ===\nТема: Привет, {компания}\n\n{проблема}. {демо}\n{подпись}\n"
                          "=== проблема: нет сайта ===\nСайта нет\n=== демо: нет ===\nМогу сделать.\n", encoding="utf-8")
        with mock.patch.object(bot, "LETTER", custom):
            d2 = bot.make_draft(g)
        self.assertEqual(d2["subject"], "Привет, Гараж Петровича")
        self.assertTrue(d2["body"].startswith("Сайта нет. Могу сделать."))
        self.assertEqual(bot.problem_key({"kind": "site", "a": {"issues": {"no_https": {}}}}), "проблема: не защищено")
        self.assertEqual(bot.problem_key({"kind": "site", "a": {"issues": {"no_description": {}}}}), "проблема: другое")

    def test_tyre_and_wash_cards_demo_and_letter(self):
        self.b.handle(1, 42, "/start")
        for niche, phrase, title in (("шиномонтаж", "шиномонтаж", "Шиномонтаж"), ("автомойка", "автомойку", "Автомойка")):
            with self.subTest(niche=niche):
                self.b.st["leads"], self.b.seen = {}, set()  # те же компании — заново для второй ниши
                self.run_search(niche=niche)
                g = self.lead("Гараж Петровича")
                self.assertEqual(g["niche"], niche)
                card = next(m for t, m in self.out if m and "Гараж Петровича" in t and f"demo:{g['id']}" in m)
                self.assertIn("✅ Сделать демо", json.dumps(json.loads(card), ensure_ascii=False))
                self.press(f"demo:{g['id']}")
                html = (self.b.demo_root / g["demo"] / "index.html").read_text(encoding="utf-8")
                self.assertIn(f'<h1 class="hero__title" data-reveal>{title}<br>', html)
                for bad in ("Кузовной", "покраск", "вмятин", "что случилось", "svc-body", "svc-paint"):
                    self.assertNotIn(bad, html)
                body = g["draft"]["body"]
                self.assertIn(f"А многие ищут {phrase} именно с телефона.", body)
                self.assertIn("у вас пока нет сайта", body)
                self.assertIn(f"http://1.2.3.4:8080/{g['demo']}/", body)
                self.assertNotIn("кузов", body.lower())
                self.assertNotIn("{", body)
                self.assertEqual(g["draft"]["subject"], "Сайт для «Гараж Петровича»")

    def test_tyre_and_wash_aliases(self):
        niches = leads.load_niches()
        for text, want in (("шиномонтаж Омск", "шиномонтаж"), ("шиномонтажка Омск", "шиномонтаж"),
                           ("шины Омск", "шиномонтаж"), ("автомойка Омск", "автомойка"), ("мойка Омск", "автомойка"),
                           ("автомойки Нижний Новгород", "автомойка")):
            self.assertEqual(leads.parse_request(text, niches)[0], want, text)
        self.assertEqual(leads.parse_request("автомойки Нижний Новгород", niches)[1], "Нижний Новгород")

    def test_no_email_lead(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        g = self.lead("Гараж Петровича")
        self.press(f"demo:{g['id']}")
        html = (self.b.demo_root / g["demo"] / "index.html").read_text(encoding="utf-8")
        self.assertIn("tel:+79132223344", html)
        self.assertIn("70 отзывов", html)
        self.assertTrue((self.b.demo_root / "_assets" / "studio" / "css" / "styles.css").exists())
        self.assertIn("нет email", self.out[-2][0])
        self.assertNotIn(f"send:{g['id']}", self.buttons(-2))
        # под письмом — текст для WhatsApp (номер мобильный) с ссылкой на демо и кнопки
        self.assertIn("Мобильный +7 913 222-33-44", self.last)
        self.assertIn(f"/{g['demo']}/", self.last)
        self.assertEqual(self.buttons(), ["https://wa.me/79132223344", f"mail:{g['id']}"])
        # узнали почту по телефону — вводим, письмо можно отправить
        self.press(f"mail:{g['id']}")
        self.b.handle(1, 42, "не почта")
        self.assertIn("не похоже на email", self.last)
        self.press(f"mail:{g['id']}")
        self.b.handle(1, 42, "Garage@Mail.ru")
        self.assertEqual(g["draft"]["to"], "garage@mail.ru")
        self.assertIn("Почту garage@mail.ru записал", self.last)
        self.assertIn(f"send:{g['id']}", self.buttons())

    def test_landline_gets_call_script(self):
        self.run_search()
        g = self.lead("Гараж Петровича")
        g["b"]["phone"] = "+7 (3812) 22-33-44"
        self.press(f"demo:{g['id']}")
        self.assertIn("мобильного тоже. Позвоните: +7 381 222-33-44", self.last)
        self.assertIn("Можно прислать вам на почту", self.last)
        self.assertEqual(self.buttons(), [f"mail:{g['id']}"])

    def test_osm_only_no_site_is_marked_and_ranked_lower(self):
        nb = leads.new_business
        found = [nb(name="Из OSM", phone="+7 900 111 22 33", sources=["OSM"]),
                 nb(name="Из 2ГИС", phone="+7 900 444 55 66", sources=["2ГИС"])]
        with mock.patch.object(leads, "find_businesses", return_value=(found, ["OpenStreetMap: 2"])):
            self.b.handle(1, 42, "кузовной ремонт Омск")
        cards = [t for t, m in self.out if t.startswith(("1.", "2."))]
        self.assertTrue(cards[0].startswith("1. Из 2ГИС · 🚫 нет сайта"))
        self.assertIn("2. Из OSM · 🚫 сайта нет в OpenStreetMap", cards[1])
        self.assertIn("проверьте, нет ли сайта: https://yandex.ru/search/?text=", cards[1])
        self.assertNotIn("проверьте", cards[0])

    def test_site_found_by_verification_is_audited(self):
        nb = leads.new_business
        found = [nb(name="Ромашка", phone="+7 900 111 22 33", sources=["OSM"])]

        def verify(bs, city):
            return [leads._adopt(bs[0], nb(site=self.old, sources=[]), "по телефону на сайте")]
        with mock.patch.object(leads, "find_businesses", return_value=(found, ["OpenStreetMap: 1"])), \
                mock.patch.object(leads, "verify_no_site", side_effect=verify):
            self.b.handle(1, 42, "кузовной ремонт Омск")
        card = next(t for t, m in self.out if t.startswith("1."))
        self.assertIn("сайта не было в OpenStreetMap, нашёл: по телефону на сайте", card)
        self.assertNotIn("нет сайта", card)

    def test_demo_for_any_niche(self):
        """Демо есть у всех ниш: у стоматологии — свои услуги без машин, у своей ниши (/addniche) — из названия."""
        self.b.handle(1, 42, "/start")
        self.run_search(niche="стоматология")
        r = self.lead("Ромашка")
        self.assertIn("✅ Сделать демо", json.dumps(json.loads(next(m for t, m in self.out if m and "Ромашка" in t)), ensure_ascii=False))
        self.press(f"demo:{r['id']}")
        html = (self.b.demo_root / r["demo"] / "index.html").read_text(encoding="utf-8")
        self.assertIn("Лечение кариеса", html)
        self.assertIn("Регистратура", html)  # свой сайт группы «медицина»
        self.assertTrue((self.b.demo_root / "_assets" / "groups" / "base.css").exists())
        for bad in ("hero-1600", "svc-body", "Фотографии на сайте", "автомобил", "подъехать"):
            self.assertNotIn(bad, html)
        self.assertIn("стоматологию именно с телефона", r["draft"]["body"])
        self.assertIn(f"http://1.2.3.4:8080/{r['demo']}/", self.last)
        self.b.handle(1, 42, "/addniche вышивка: вышив | low")
        self.b.seen = set()
        self.run_search(niche="вышивка")
        v = self.lead("Ромашка")
        self.assertEqual(v["niche"], "вышивка")
        self.press(f"demo:{v['id']}")
        html = (self.b.demo_root / v["demo"] / "index.html").read_text(encoding="utf-8")
        self.assertIn("Вышивка", html)
        self.assertNotIn("hero-1600", html)
        self.assertIn(f"send:{v['id']}", self.buttons())

    def test_limits_pause_and_delay(self):
        self.b.cfg["limits"].update(per_day=1, min_delay_sec=90)
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        self.press(f"demo:{r['id']}")
        self.b.st["last_send_ts"] = bot.time.time() - 30
        with mock.patch.object(bot.mailer, "send"):
            self.press(f"send:{r['id']}")
        waited = bot.time.sleep.call_args.args[0]
        self.assertTrue(55 <= waited <= 61, waited)
        other = dict(r, id="other1", status="draft", draft=dict(r["draft"], to="x@y.ru"))
        self.b.st["leads"]["other1"] = other
        with mock.patch.object(bot.mailer, "send") as send:
            self.press("send:other1")
        send.assert_not_called()
        self.assertIn("дневной лимит", self.last)
        self.b.handle(1, 42, "/pause")
        self.b.st["sent_today"] = {}
        with mock.patch.object(bot.mailer, "send") as send:
            self.press("send:other1")
        send.assert_not_called()
        self.assertIn("на паузе", self.last)

    def test_send_failure_offers_retry(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        self.press(f"demo:{r['id']}")
        with mock.patch.object(bot.mailer, "send", side_effect=OSError("connection refused")):
            self.press(f"send:{r['id']}")
        self.assertIn("не ушло", self.last)
        self.assertEqual(self.buttons(), [f"send:{r['id']}"])
        self.assertNotEqual(r["status"], "sent")

    def test_edit(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        self.press(f"demo:{r['id']}")
        self.press(f"edit:{r['id']}")
        self.b.handle(1, 42, "Тема: Новый сайт для вас\nДобрый день! Короткий текст.")
        self.assertEqual(r["draft"]["subject"], "Новый сайт для вас")
        self.assertEqual(r["draft"]["body"], "Добрый день! Короткий текст.")
        self.assertIn("Текст обновил", self.last)
        self.assertIsNone(self.b.st["pending_edit"])

    def test_one_click(self):
        self.b.cfg["one_click"] = True
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        with mock.patch.object(bot.mailer, "send") as send:
            self.press(f"demo:{r['id']}")
        send.assert_called_once()
        self.assertEqual(r["status"], "sent")

    def test_inbox(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        r = self.lead("Ромашка")
        r.update(status="sent", sent_to="boss@romashka.ru")
        g = self.lead("Гараж Петровича")
        g.update(status="sent", sent_to="garage@mail.ru")
        events = [("1", {"kind": "reply", "from": "boss@romashka.ru", "subject": "Re: сайт", "text": "Да, интересно"}),
                  ("2", {"kind": "refusal", "from": "garage@mail.ru", "subject": "Re", "text": "Нет"}),
                  ("3", {"kind": "bounce", "from": "mailer-daemon@x", "subject": "Undelivered", "failed": "a@b.ru"}),
                  ("4", {"kind": "bounce", "from": "mailer-daemon@x", "subject": "Undelivered", "failed": "c@d.ru"}),
                  ("5", {"kind": "bounce", "from": "mailer-daemon@x", "subject": "Undelivered", "failed": "e@f.ru"})]
        def fetch(cfg, seen, folder="INBOX"):
            return events if folder == "INBOX" else []
        with mock.patch.object(bot.mailer, "fetch_new", side_effect=fetch) as fetch_mock:
            self.b.check_inbox()
        self.assertEqual(fetch_mock.call_args.args[0]["user"], "me@my.ru")  # логин берётся из smtp
        self.assertEqual(fetch_mock.call_args.args[0]["host"], "imap.test")
        texts = [t for t, _ in self.out]
        self.assertTrue(any("📩 Ответ от «Ромашка»" in t and "Да, интересно" in t for t in texts))
        self.assertTrue(any("отказался" in t for t in texts))
        self.assertIn("garage@mail.ru", (self.dir / "do_not_contact.txt").read_text(encoding="utf-8"))
        self.assertEqual((r["status"], g["status"]), ("replied", "refused"))
        self.assertTrue(self.b.st["paused"])
        self.assertIn("поставил отправку на паузу", self.last)
        self.assertEqual(sorted(self.b.st["mail_seen"]), ["1", "2", "3", "4", "5"])

    def test_testmail(self):
        self.b.handle(1, 42, "/start")
        with mock.patch.object(bot.mailer, "send") as send:
            self.b.handle(1, 42, "/testmail")
        self.assertEqual(send.call_args.args[1], "me@my.ru")
        self.assertIn("✅", self.last)
        with mock.patch.object(bot.mailer, "send", side_effect=bot.mailer.smtplib.SMTPAuthenticationError(535, b"bad")):
            self.b.handle(1, 42, "/testmail")
        self.assertIn("пароля приложения", self.last)
        with mock.patch.object(bot.mailer, "fetch_new", return_value=[("9", {"kind": "reply", "from": "me@my.ru",
                                                                            "subject": "Проверка", "text": "x"})]):
            n = len(self.out)
            self.b.check_inbox()
        self.assertEqual(len(self.out), n)  # своё письмо — не «ответ клиента»
        with mock.patch.object(bot.mailer, "fetch_new", return_value=[("10", {"kind": "auto", "from": "no-reply@accounts.google.com",
                                                                             "subject": "Оповещение"})]):
            self.b.check_inbox()
        self.assertEqual(len(self.out), n)  # уведомления Google — тоже не ответы

    def test_schedule(self):
        self.b.handle(1, 42, "/start")
        tz = ZoneInfo("Europe/Moscow")
        def fake_search(*args, **kw):  # настоящий поиск в конце освобождает блокировку — повторяем это
            self.b.busy.release()
            return 3
        with mock.patch.object(self.b, "search", side_effect=fake_search) as search:
            self.assertFalse(self.b.run_schedule_if_due(datetime(2026, 11, 10, 8, 59, tzinfo=tz)))
            self.assertTrue(self.b.run_schedule_if_due(datetime(2026, 11, 10, 9, 1, tzinfo=tz)))
            self.assertFalse(self.b.run_schedule_if_due(datetime(2026, 11, 10, 15, 0, tzinfo=tz)))
            self.assertTrue(self.b.run_schedule_if_due(datetime(2026, 11, 11, 9, 0, tzinfo=tz)))
        self.assertEqual(search.call_args.args, (1, "кузовной ремонт", "Омск"))
        self.assertEqual(search.call_args.kwargs, {"limit": 5, "quiet": True})

    def test_schedule_cities_rotation(self):
        self.b.cfg["schedule"] = {"time": "09:00", "tz": "Europe/Moscow", "max_cards": 5, "max_searches": 3,
                                  "niches": ["автосервис", "шиномонтаж"], "cities": ["Омск", "Тула", "Казань"]}
        self.b.handle(1, 42, "/start")
        self.assertEqual(self.b.schedule_queries(), ["автосервис Омск", "шиномонтаж Омск", "автосервис Тула",
                                                     "шиномонтаж Тула", "автосервис Казань", "шиномонтаж Казань"])
        tz = ZoneInfo("Europe/Moscow")
        calls = []

        def fake_search(chat, niche, city, limit, quiet=False):
            calls.append(f"{niche} {city}")
            self.b.busy.release()
            return 0  # карточек нет — бот идёт дальше, но не больше max_searches запросов
        with mock.patch.object(self.b, "search", side_effect=fake_search):
            self.b.run_schedule_if_due(datetime(2026, 11, 10, 9, 0, tzinfo=tz))
            self.assertEqual(calls, ["автосервис Омск", "шиномонтаж Омск", "автосервис Тула"])
            calls.clear()
            self.b.run_schedule_if_due(datetime(2026, 11, 11, 9, 0, tzinfo=tz))  # назавтра — с места остановки
            self.assertEqual(calls, ["шиномонтаж Тула", "автосервис Казань", "шиномонтаж Казань"])
            calls.clear()
            self.b.run_schedule_if_due(datetime(2026, 11, 12, 9, 0, tzinfo=tz))  # и по кругу
            self.assertEqual(calls[0], "автосервис Омск")
        self.b.handle(1, 42, "/status")
        self.assertIn("6 запросов, следующий — «шиномонтаж Тула»", self.last)

    def test_status_and_niches(self):
        self.b.handle(1, 42, "/status")
        self.assertIn("Сегодня отправлено: 0 из 20", self.last)
        self.assertIn("Ссылки на демо: http://1.2.3.4:8080", self.last)
        self.b.handle(1, 42, "/niches")
        self.assertIn("Демо бот делает для любой", self.last)
        self.assertIn("кафе ⏸", self.last)  # не входит в ежедневный поиск
        self.assertNotIn("стоматология ⏸", self.last)
        self.b.handle(1, 42, "/addniche Натяжные потолки люкс: потолк, натяжн | high")
        self.assertIn("Добавил нишу", self.last)

    def test_demo_server(self):
        root = self.dir / "srv"
        (root / "abc").mkdir(parents=True)
        (root / "abc" / "index.html").write_text("<h1>демо</h1>", encoding="utf-8")
        (root / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
        srv = bot.ThreadingHTTPServer(("127.0.0.1", 0), bot.functools.partial(bot.DemoHandler, directory=str(root)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            with urllib.request.urlopen(base + "/abc/") as r:
                self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
                self.assertIn("демо", r.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/")
            self.assertEqual(cm.exception.code, 404)  # список демо не показывается
        finally:
            srv.shutdown()

    def card_names(self):
        return [t.split(". ", 1)[1].split("\n")[0].split(" · ")[0] for t, m in self.out if m and t[:1].isdigit()]

    def search_with(self, found, query="кузовной ремонт Омск"):
        self.out.clear()
        with mock.patch.object(leads, "find_businesses", return_value=(found, ["2ГИС: 1"])):
            self.b.handle(1, 42, query)
        return self.card_names()

    def test_no_repeats_when_business_changes_key(self):
        """Компания, уже показанная без сайта, приходит позже из другого источника — с сайтом и телефонами
        в другом порядке. Её главный признак (домен вместо телефона) другой, но показывать её снова нельзя."""
        nb = leads.new_business
        self.b.handle(1, 42, "/start")
        first = [nb(name="Гараж Петровича", phone="+7 (913) 222-33-44", reviews=70, hours="Mo-Fr 09:00-18:00",
                    sources=["OSM"]),
                 nb(name="Кузов-Мастер", phone="+7 900 555 66 77; +7 900 555 66 78", reviews=40, sources=["OSM"])]
        self.assertEqual(sorted(self.search_with(first)), ["Гараж Петровича", "Кузов-Мастер"])
        self.press(f"skip:{self.lead('Кузов-Мастер')['id']}")
        later = [nb(name="Гараж Петровича", site=self.old, phone="+7 913 222 33 44", reviews=70, sources=["2ГИС"]),
                 nb(name="Кузов Мастер", phone="+7 900 555 66 78; +7 900 555 66 77", email="km@mail.ru",
                    reviews=40, sources=["2ГИС"]),
                 nb(name="Новый", phone="+7 900 999 00 00", reviews=40, sources=["2ГИС"])]
        self.assertEqual(self.search_with(later), ["Новый"])
        # после перезапуска (новый процесс читает bot_state.json) — тоже без повторов
        self.make_bot(json.loads(json.dumps(CONFIG)))
        self.assertEqual(self.search_with(later + [nb(name="Ещё", phone="+7 900 999 00 01", sources=["2ГИС"])]),
                         ["Ещё"])

    def test_schedule_full_circle_no_repeats(self):
        """Расписание прошло все запросы и пошло по второму кругу, бот при этом перезапускался —
        те же компании снова не показываются (OpenStreetMap отдаёт их каждый раз)."""
        nb = leads.new_business
        self.b.cfg["schedule"] = {"time": "09:00", "tz": "Europe/Moscow", "max_cards": 10, "max_searches": 2,
                                  "niches": ["автосервис", "шиномонтаж"], "cities": ["Омск"]}
        self.b.handle(1, 42, "/start")
        by_query = {"автосервис": [nb(name="Сервис 1", phone="+7 900 000 00 01", reviews=40, sources=["OSM"]),
                                   nb(name="Общий", phone="+7 900 000 00 03", reviews=40, sources=["OSM"])],
                    "шиномонтаж": [nb(name="Шины 1", phone="+7 900 000 00 02", reviews=40, sources=["OSM"]),
                                   nb(name="Общий", phone="+7 900 000 00 03", reviews=40, sources=["OSM"])]}
        tz = ZoneInfo("Europe/Moscow")

        def day(n):
            self.out.clear()
            with mock.patch.object(leads, "find_businesses",
                                   side_effect=lambda name, niche, city: ([dict(b) for b in by_query[name]], ["OSM"])):
                self.assertTrue(self.b.run_schedule_if_due(datetime(2026, 11, n, 9, 0, tzinfo=tz)))
            return self.card_names()
        self.assertEqual(sorted(day(10)), ["Общий", "Сервис 1", "Шины 1"])
        self.assertEqual(day(11), [])  # второй круг
        self.make_bot(self.b.cfg)  # перезапуск службы
        self.assertEqual(day(12), [])

    # ——— работа месяцами: все города и ниши, без повторов, без потери кандидатов ———
    def test_schedule_all_cities_and_niches(self):
        self.b.cfg["schedule"] = {"time": "09:00", "niches": "все", "cities": "все"}
        cities, niches = self.b.schedule_cities(), self.b.schedule_niches()
        self.assertGreaterEqual(len(cities), 200)
        self.assertEqual(cities[:3], ["Москва", "Санкт-Петербург", "Алматы"])  # от крупных к небольшим
        self.assertIn("Минск", cities)
        self.assertGreaterEqual(len(niches), 55)
        self.assertNotIn("кафе", niches)
        qs = self.b.schedule_queries()
        self.assertEqual(len(qs), len(cities) * len(niches))
        all_niches = leads.load_niches()
        for q in qs:  # каждый запрос расписания понимается ботом ровно так, как задуман
            n, c = leads.parse_request(q, all_niches)
            self.assertEqual(f"{n} {c}", q)
        self.b.cfg["schedule"]["countries"] = ["RU"]
        self.assertEqual(len(self.b.schedule_cities()), 172)
        del self.b.cfg["schedule"]["cities"]  # не задано — тоже все города
        self.assertEqual(len(self.b.schedule_cities()), 172)
        self.b.handle(1, 42, "/status")
        self.assertIn("городов", self.last)
        self.assertIn("Запросы к API сегодня", self.last)

    def test_unshown_good_leads_come_back(self):
        """Хорошие кандидаты, которым не хватило дневного лимита карточек, не теряются: придут в следующий раз."""
        nb = leads.new_business
        self.b.handle(1, 42, "/start")
        found = [nb(name=f"Гараж {i}", phone=f"+7 900 000 00 {i:02d}", reviews=70, sources=["2ГИС"]) for i in range(5)]
        with mock.patch.object(leads, "find_businesses", return_value=([dict(b) for b in found], ["2ГИС: 5"])):
            first = self.b.search(1, "автосервис", "Омск", limit=2, quiet=True)
        self.assertEqual(first, 2)
        with mock.patch.object(leads, "find_businesses", return_value=([dict(b) for b in found], ["2ГИС: 5"])):
            second = self.b.search(1, "автосервис", "Омск", limit=10, quiet=True)
        self.assertEqual(second, 3)
        self.assertEqual(len(self.b.st["leads"]), 5)
        self.assertIn("📍 из обхода: автосервис · Омск", self.out[-1][0])

    def test_seen_moves_out_of_state_file(self):
        bot.STATE.write_text(json.dumps({"owner": 42, "owner_chat": 1, "seen": ["tel:9001112233", "x.ru"]}), encoding="utf-8")
        self.make_bot(json.loads(json.dumps(CONFIG)))
        self.assertEqual(self.b.seen, {"tel:9001112233", "x.ru"})
        self.b.save()
        self.assertEqual(json.loads(bot.STATE.read_text(encoding="utf-8"))["seen"], [])
        self.make_bot(json.loads(json.dumps(CONFIG)))  # после перезапуска — из seen.txt
        self.assertEqual(self.b.seen, {"tel:9001112233", "x.ru"})
        nb = leads.new_business
        names = self.search_with([nb(name="Старый", phone="+7 900 111 22 33", reviews=40, sources=["OSM"]),
                                  nb(name="Новый", phone="+7 900 111 22 44", reviews=40, sources=["OSM"])])
        self.assertEqual(names, ["Новый"])

    def test_schedule_waits_when_sources_are_down(self):
        """Карты не отвечают — бот не «проматывает» очередь: те же запросы повторит завтра."""
        self.b.cfg["schedule"] = {"time": "09:00", "tz": "Europe/Moscow", "max_cards": 5, "max_searches": 10,
                                  "niches": ["автосервис", "шиномонтаж"], "cities": ["Омск", "Тула", "Казань"]}
        self.b.handle(1, 42, "/start")
        tz = ZoneInfo("Europe/Moscow")
        with mock.patch.object(leads, "find_businesses", side_effect=leads.MapError("Overpass перегружен")):
            self.b.run_schedule_if_due(datetime(2026, 11, 10, 9, 0, tzinfo=tz))
        self.assertEqual(self.b.st["sched_pos"], 0)
        self.assertIn("остальное в следующий раз", self.last)
        self.assertEqual(bot.time.sleep.call_count, 2)  # пауза между запросами к картам

    def test_scan_now(self):
        """/scan — обход сейчас, с места остановки и с теми же лимитами; расписание на сегодня не отменяет."""
        self.b.cfg["schedule"] = {"time": "09:00", "tz": "Europe/Moscow", "max_cards": 5, "max_searches": 2,
                                  "niches": ["автосервис", "шиномонтаж"], "cities": ["Омск", "Тула"]}
        calls = []

        def fake_search(chat, niche, city, limit, quiet=False):
            calls.append((f"{niche} {city}", limit, quiet))
            self.b.busy.release()
            return 0
        with mock.patch.object(self.b, "search", side_effect=fake_search):
            self.b.handle(1, 42, "/scan")
            self.assertEqual(calls, [("автосервис Омск", 5, True), ("шиномонтаж Омск", 5, True)])
            self.assertIn("Следующий запрос — «автосервис Тула»", self.last)
            self.assertTrue(self.b.run_schedule_if_due(datetime(2026, 11, 10, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))))
            self.assertEqual(calls[2][0], "автосервис Тула")  # расписание продолжает с того же места
            self.b.sched_lock.acquire()
            self.b.handle(1, 42, "/scan")  # обход уже идёт — второй не запускается
            self.assertIn("Обход уже идёт", self.last)
            self.b.sched_lock.release()
        self.assertEqual(len(calls), 4)
        self.b.handle(1, 99, "/scan")  # чужим бот не отвечает
        self.assertEqual(len(calls), 4)

    def test_request_during_scan_runs_next(self):
        """Ваш запрос во время обхода не теряется: он идёт сразу после текущего шага обхода, раньше следующего."""
        self.b.cfg["schedule"] = {"time": "09:00", "tz": "Europe/Moscow", "max_cards": 5, "max_searches": 2,
                                  "niches": ["автосервис", "шиномонтаж"], "cities": ["Тула"]}
        calls = []

        def fake_search(chat, niche, city, limit=bot.SHOW_TOP, quiet=False):
            calls.append((f"{niche} {city}", quiet))
            if len(calls) == 1:  # пока идёт первый шаг обхода, вы пишете свой запрос
                self.b.current, self.b.current_auto = f"{niche} {city}", True
                self.b.handle(1, 42, "стоматология Омск")
                self.assertIn("обход", self.last)
                self.assertIn("«стоматология Омск» начну сразу после", self.last)
            self.b.current, self.b.current_auto = "", False  # как настоящий search в finally
            self.b.busy.release()
            self.b.run_pending()
            return 0
        with mock.patch.object(self.b, "search", side_effect=fake_search):
            self.b.handle(1, 42, "/start")
            self.b.handle(1, 42, "/scan")
        self.assertEqual(calls, [("автосервис Тула", True), ("стоматология Омск", False), ("шиномонтаж Тула", True)])
        self.assertIsNone(self.b.pending)
        self.assertFalse(self.b.busy.locked())

    def test_request_during_search_replaces_waiting_one(self):
        self.b.handle(1, 42, "/start")
        self.b.busy.acquire()
        self.b.current = "кузовной ремонт Омск"
        self.b.handle(1, 42, "автосервис Омск")
        self.assertIn("Сейчас ищу «кузовной ремонт Омск»", self.last)
        self.b.handle(1, 42, "стоматология Казань")
        self.assertIn("«автосервис Омск» отменил", self.last)
        self.assertEqual(self.b.pending, (1, "стоматология", "Казань"))
        with mock.patch.object(self.b, "search") as search:
            self.b.busy.release()
            self.b.run_pending()
        search.assert_called_once_with(1, "стоматология", "Казань")

    def test_telegram_retry(self):
        b = bot.Bot("TOKEN", json.loads(json.dumps(CONFIG)))
        r429 = mock.Mock(status_code=429)
        r429.json.return_value = {"parameters": {"retry_after": 7}}
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"result": {"message_id": 1}}
        with mock.patch.object(bot.requests, "post", side_effect=[r429, bot.requests.ConnectionError(), ok]) as post:
            self.assertEqual(b.call("sendMessage", chat_id=1, text="x"), {"message_id": 1})
        self.assertEqual(post.call_count, 3)
        self.assertEqual(bot.time.sleep.call_args_list[0].args[0], 7)

    def test_trim_leads_keeps_sent(self):
        with mock.patch.object(bot, "KEEP_LEADS", 3):
            self.b.st["leads"] = {str(i): {"status": "sent" if i < 2 else "new"} for i in range(6)}
            self.b.trim_leads()
        self.assertEqual(sorted(self.b.st["leads"]), ["0", "1", "5"])

    def test_old_state_format(self):
        bot.STATE.write_text('{"owner": 1, "offset": 5, "seen": [], "skip": [], "last": [{"b": {}}]}', encoding="utf-8")
        st = bot.load_state()
        self.assertEqual((st["last"], st["leads"], st["paused"]), ([], {}, False))

    def test_long_message_split_markup_on_last(self):
        self.b.say(1, "\n\n".join(["x" * 1000] * 10), bot.kb([("ok", "a:b")]))
        sent = [(t, m) for t, m in self.out]
        self.assertTrue(len(sent) >= 3 and all(len(t) <= 4096 for t, _ in sent))
        self.assertEqual(sum(t.count("x") for t, _ in sent), 10000)
        self.assertEqual([m is not None for _, m in sent], [False] * (len(sent) - 1) + [True])


    # ——— две почты, ответы, 12 компаний, «работает ли компания» ———
    def two_boxes(self, **lim):
        self.b.cfg["smtp2"] = {"host": "smtp.yandex.ru", "port": 465, "user": "second@my.ru", "password": "y"}
        self.b.cfg["limits"].update(per_day=10, **lim)

    @staticmethod
    def fake_lead(i):
        return {"id": f"x{i}", "b": {"name": f"Компания {i}"}, "status": "draft",
                "draft": {"to": f"c{i}@client.ru", "subject": "s", "body": "b"}}

    def test_two_mailboxes_split_ten_letters(self):
        self.b.handle(1, 42, "/start")
        self.two_boxes()
        self.assertEqual(self.b.box_quotas(), {"smtp": 5, "smtp2": 5})
        froms = []
        with mock.patch.object(bot.mailer, "send", side_effect=lambda box, *a, **k: froms.append(box["user"])):
            for i in range(11):
                self.b.send_lead(1, self.fake_lead(i))
        self.assertEqual(froms.count("me@my.ru"), 5)
        self.assertEqual(froms.count("second@my.ru"), 5)
        self.assertEqual(froms[:2], ["me@my.ru", "second@my.ru"])  # по очереди
        self.assertIn("дневной лимит 10 писем исчерпан (со всех ящиков)", self.last)
        self.b.handle(1, 42, "/status")
        self.assertIn("Сегодня отправлено: 10 из 10", self.last)
        self.assertIn("second@my.ru — отправлено 5 из 5", self.last)

    def test_unfilled_second_mailbox_is_ignored(self):
        self.b.handle(1, 42, "/start")
        self.b.cfg["smtp2"] = {"host": "smtp.yandex.ru", "user": "вторая-почта@ваш-домен.ru", "password": "пароль приложения"}
        self.b.cfg["limits"]["per_day"] = 10
        self.assertEqual([b["key"] for b in self.b.boxes()], ["smtp"])
        self.assertEqual(self.b.box_quotas(), {"smtp": 10})
        with mock.patch.object(bot.mailer, "send") as send:
            self.b.handle(1, 42, "/testmail")
        self.assertEqual(send.call_count, 1)

    def test_testmail_checks_both_boxes(self):
        self.b.handle(1, 42, "/start")
        self.two_boxes()
        def send(box, to, *a, **k):
            if box["key"] == "smtp2":
                raise bot.mailer.smtplib.SMTPAuthenticationError(535, b"bad")
        with mock.patch.object(bot.mailer, "send", side_effect=send):
            self.b.handle(1, 42, "/testmail")
        self.assertIn("✅ Пробное письмо отправлено на me@my.ru", self.last)
        self.assertIn("❌ С second@my.ru письмо не ушло", self.last)

    def test_inbox_second_box_and_spam(self):
        self.b.handle(1, 42, "/start")
        self.two_boxes()
        self.b.st["leads"]["r"] = {"id": "r", "b": {"name": "Ромашка"}, "status": "sent", "sent_to": "boss@romashka.ru"}
        self.b.st["leads"]["k"] = {"id": "k", "b": {"name": "Клён"}, "status": "sent", "sent_to": "klen@mail.ru"}
        def fetch(cfg, seen, folder="INBOX"):
            if cfg["user"] == "second@my.ru" and folder == "INBOX":
                return [("1", {"kind": "reply", "from": "boss@romashka.ru", "subject": "Re", "text": "Давайте"})]
            if cfg["user"] == "me@my.ru" and folder == "spam":
                return [("7", {"kind": "reply", "from": "klen@mail.ru", "subject": "Re", "text": "Сколько стоит?"}),
                        ("8", {"kind": "reply", "from": "casino@spam.ru", "subject": "Win", "text": "$$$"})]
            return []
        with mock.patch.object(bot.mailer, "fetch_new", side_effect=fetch) as fm:
            self.b.check_inbox()
        hosts = {c.args[0]["host"] for c in fm.call_args_list}
        self.assertEqual(hosts, {"imap.test", "imap.yandex.ru"})  # для второго ящика адрес IMAP — из SMTP
        texts = "\n".join(t for t, _ in self.out)
        self.assertIn("📩 Ответ от «Ромашка» (boss@romashka.ru) (в ящик second@my.ru)", texts)
        self.assertIn("«Клён» (klen@mail.ru) (в ящик me@my.ru) — письмо лежало в «Спаме»", texts)
        self.assertNotIn("casino", texts)  # чужой спам не пересылаем
        self.assertEqual(sorted(self.b.st["mail_seen"]), ["smtp2:1", "spam:7", "spam:8"])
        n = len(self.out)
        seen_args = []
        with mock.patch.object(bot.mailer, "fetch_new", side_effect=lambda cfg, seen, folder="INBOX": seen_args.append(
                (cfg["user"], folder, sorted(seen))) or []):
            self.b.check_inbox()
        self.assertEqual(len(self.out), n)
        self.assertIn(("second@my.ru", "INBOX", ["1"]), seen_args)
        self.assertIn(("me@my.ru", "spam", ["7", "8"]), seen_args)

    def test_inbox_error_is_reported_once(self):
        self.b.handle(1, 42, "/start")
        err = bot.mailer.imaplib.IMAP4.error("LOGIN failed")
        with mock.patch.object(bot.mailer, "fetch_new", side_effect=err):
            self.b.check_inbox()
            self.b.check_inbox()
        warnings = [t for t, _ in self.out if "Не могу прочитать ответы" in t]
        self.assertEqual(len(warnings), 1)
        self.assertIn("IMAP", warnings[0])
        self.b.handle(1, 42, "/status")
        self.assertIn("❌ ответы не читаются", self.last)
        with mock.patch.object(bot.mailer, "fetch_new", return_value=[]):
            self.b.check_inbox()
        self.assertIn("снова читается", self.last)

    def test_twelve_cards_from_other_cities(self):
        self.b.handle(1, 42, "/start")
        nb = leads.new_business

        def found(niche_name, niche, city):
            n = 3 if city == "Омск" else 4
            return [nb(name=f"Кузня {city} {i}", phone=f"+7 913 {abs(hash(city)) % 900 + 100} 00 {i:02d}", reviews=20,
                       sources=["2ГИС"]) for i in range(n)], ["2ГИС: n"]
        with mock.patch.object(leads, "find_businesses", side_effect=found):
            self.b.handle(1, 42, "кузовной ремонт Омск")
        cards = [t for t, m in self.out if m and "demo:" in m]
        self.assertEqual(len(cards), 12)
        self.assertTrue(cards[11].startswith("12. "))
        self.assertEqual(len(self.b.st["last"]), 12)
        self.assertNotIn("📍 город", cards[0])
        self.assertIn("📍 город:", cards[3])
        texts = [t for t, _ in self.out]
        self.assertTrue(any("В городе Омск подходящих только 3. Добираю до 12" in t for t in texts))
        self.assertTrue(any(t.startswith("Готово: 12 компаний (Омск — 3,") for t in texts))
        self.assertEqual(len(self.files), 1)  # один файл со всеми компаниями

    def test_closed_companies_are_dropped(self):
        self.b.handle(1, 42, "/start")
        nb = leads.new_business
        biz = [nb(name="Закрытый гараж", phone="+7 913 000 00 01", sources=["OSM"], closed="в OpenStreetMap помечена как закрытая"),
               nb(name="Старый гараж", phone="+7 913 000 00 02", email="old@g.ru", hours="Mo-Fr 09:00-18:00",
                  sources=["OSM"], osm_date="2014-05-01"),
               nb(name="Живой гараж", phone="+7 913 000 00 03", reviews=40, sources=["2ГИС"])]
        with mock.patch.object(leads, "find_businesses", return_value=(biz, ["n"])), \
                mock.patch.object(leads, "other_cities", return_value=[]):
            self.b.handle(1, 42, "кузовной ремонт Омск")
        texts = "\n".join(t for t, _ in self.out)
        self.assertNotIn("Закрытый гараж", texts.split("Нашёл", 1)[1])
        self.assertIn("Ещё 1 закрыты по данным карт", texts)
        self.assertIn("🟢 работает: есть в 2ГИС, 40 отзывов", texts)
        self.assertIn("🟠 могла закрыться: данные о компании не обновлялись с 2014 г.", texts)
        self.assertIn("tel:9130000001", self.b.seen)  # закрытую больше не рассматриваем


if __name__ == "__main__":
    unittest.main()
