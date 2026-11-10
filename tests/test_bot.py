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
                        mock.patch.object(bot.time, "sleep")]
        for x in self.patches:
            x.start()
        self.make_bot(json.loads(json.dumps(CONFIG)) if config is None else config)

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
        return [btn["callback_data"] for row in json.loads(m)["inline_keyboard"] for btn in row] if m else []

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
            to, subject, body = send.call_args.args[1:]
        self.assertEqual(to, "boss@romashka.ru")
        self.assertIn(r["demo"], body)
        self.assertIn("Отправлено", self.last)
        self.assertEqual(r["status"], "sent")
        with mock.patch.object(bot.mailer, "send") as send:
            self.press(f"send:{r['id']}")
        send.assert_not_called()
        self.assertIn("уже отправлено", self.last)

    def test_no_email_lead(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        g = self.lead("Гараж Петровича")
        self.press(f"demo:{g['id']}")
        html = (self.b.demo_root / g["demo"] / "index.html").read_text(encoding="utf-8")
        self.assertIn("tel:+79132223344", html)
        self.assertIn("70 отзывов", html)
        self.assertTrue((self.b.demo_root / "_assets" / "studio" / "css" / "styles.css").exists())
        self.assertIn("нет email", self.last)
        self.assertNotIn(f"send:{g['id']}", self.buttons())

    def test_niche_without_demo(self):
        self.b.handle(1, 42, "/start")
        self.run_search(niche="стоматология")
        r = self.lead("Ромашка")
        self.assertIn("✉️ Письмо без демо", json.dumps(json.loads(next(m for t, m in self.out if m and "Ромашка" in t)), ensure_ascii=False))
        self.press(f"demo:{r['id']}")
        self.assertNotIn("demo", r)
        self.assertNotIn("http://1.2.3.4", self.last)
        self.assertIn(f"send:{r['id']}", self.buttons())

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
        with mock.patch.object(bot.mailer, "fetch_new", return_value=events) as fetch:
            self.b.check_inbox()
        self.assertEqual(fetch.call_args.args[0]["user"], "me@my.ru")  # логин берётся из smtp
        texts = [t for t, _ in self.out]
        self.assertTrue(any("📩 Ответ от «Ромашка»" in t and "Да, интересно" in t for t in texts))
        self.assertTrue(any("отказался" in t for t in texts))
        self.assertIn("garage@mail.ru", (self.dir / "do_not_contact.txt").read_text(encoding="utf-8"))
        self.assertEqual((r["status"], g["status"]), ("replied", "refused"))
        self.assertTrue(self.b.st["paused"])
        self.assertIn("поставил отправку на паузу", self.last)
        self.assertEqual(sorted(self.b.st["mail_seen"]), ["1", "2", "3", "4", "5"])

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
        self.assertEqual(search.call_args.kwargs, {"limit": 5})

    def test_status_and_niches(self):
        self.b.handle(1, 42, "/status")
        self.assertIn("Сегодня отправлено: 0 из 20", self.last)
        self.assertIn("http://1.2.3.4:8080", self.last)
        self.b.handle(1, 42, "/niches")
        self.assertIn("кузовной ремонт 🖼", self.last)
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


if __name__ == "__main__":
    unittest.main()
