"""Тесты бота: источники подменены, сайты — на локальном сервере."""
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot  # noqa: E402
import leads  # noqa: E402
from test_prospector import H  # noqa: E402


class BotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # два «сайта» с главной в корне: старый (127.0.0.1) и хороший (localhost)
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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(bot, "STATE", d / "state.json"),
                        mock.patch.object(leads, "CUSTOM_NICHES_FILE", d / "custom.json"),
                        mock.patch.object(bot, "HERE", d)]
        for x in self.patches:
            x.start()
        self.b = bot.Bot("TOKEN")
        self.said, self.files = [], []
        self.b.say = lambda chat, text: self.said.append(text)
        self.b.send_file = lambda chat, name, content, caption="": self.files.append((name, content))

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def businesses(self):
        nb = leads.new_business
        return [nb(name="Ромашка", site=self.old, email="boss@romashka.ru", sources=["OSM"]),
                nb(name="Хорошая", site=self.good, phone="+7 900 111 11 11", sources=["OSM"]),
                nb(name="Гараж Петровича", phone="+7 (913) 222-33-44", reviews=70, rating=4.8, sources=["2ГИС"]),
                nb(name="Без контактов", sources=["OSM"])]

    def run_search(self, niche="кузовной ремонт", city="Омск"):
        with mock.patch.object(leads, "find_businesses", return_value=(self.businesses(), ["OpenStreetMap: 3", "2ГИС: 1"])):
            self.b.busy.acquire()
            self.b.search(1, niche, city)

    def test_owner_only(self):
        self.b.handle(1, 42, "/start")
        self.assertEqual(self.b.st["owner"], 42)
        self.b.handle(1, 99, "/help")
        self.assertEqual(len(self.said), 1)

    def test_search_flow(self):
        self.b.handle(1, 42, "/start")
        self.run_search()
        report = self.said[-1]
        # компания без сайта, но с отзывами и телефоном — первая
        first = report.split("\n\n")[1]
        self.assertIn("Гараж Петровича", first)
        self.assertIn("нет сайта", first)
        self.assertIn("💰 сайт-визитка", first)
        self.assertIn("Ромашка", report)
        self.assertIn("нет мобильной версии", report)
        self.assertNotIn("Хорошая", report)       # хороший сайт ниже порога
        self.assertNotIn("Без контактов", report)  # отсеяна
        self.assertIn("pricing.json", report)
        name, content = self.files[0]
        self.assertEqual(name, "кузовной_ремонт-Омск.csv")
        self.assertIn("Гараж Петровича".encode(), content)
        self.assertIn("рекомендация".encode(), content)

        self.said.clear()
        self.b.handle(1, 42, "/draft 1")
        self.assertIn("сайта у вас нет", self.said[-1])
        self.assertIn("+7 (913) 222-33-44", self.said[-1])
        num = next(i for i, L in enumerate(self.b.st["last"], 1) if L["b"]["name"] == "Ромашка")
        self.b.handle(1, 42, f"/draft {num}")
        self.assertIn("Кому: boss@romashka.ru", self.said[-1])
        self.b.handle(1, 42, "/draft 99")
        self.assertIn("Укажите номер", self.said[-1])

        self.b.handle(1, 42, "/skip 1")
        self.assertIn("tel:9132223344", self.b.st["skip"])
        # повторный поиск: всё уже видели
        self.said.clear()
        self.run_search()
        self.assertIn("Новых компаний не нашёл", self.said[-1])

    def test_niche_commands(self):
        self.b.handle(1, 42, "/niches")
        self.assertIn("стоматология", self.said[-1])
        self.b.handle(1, 42, "/addniche Натяжные потолки люкс: потолк, натяжн | high")
        self.assertIn("Добавил нишу «натяжные потолки люкс»", self.said[-1])
        self.assertEqual(leads.load_niches()["натяжные потолки люкс"]["keywords"], ["потолк", "натяжн"])
        self.b.handle(1, 42, "/addniche плохо")
        self.assertIn("Формат", self.said[-1])
        with mock.patch.object(threading, "Thread") as th:
            self.b.handle(1, 42, "натяжные потолки люкс Казань")
        self.assertEqual(th.call_args.kwargs["args"], (1, "натяжные потолки люкс", "Казань"))

    def test_sources_command(self):
        with mock.patch.object(leads, "api_keys", return_value={"yandex": "", "2gis": "K"}):
            self.b.handle(1, 42, "/sources")
        self.assertIn("2ГИС — подключён", self.said[-1])
        self.assertIn("Яндекс — нет ключа", self.said[-1])

    def test_map_error_reaches_user(self):
        with mock.patch.object(leads, "find_businesses", side_effect=leads.MapError("OpenStreetMap: HTTP 403")):
            self.b.busy.acquire()
            self.b.search(1, "кафе", "Казань")
        self.assertIn("HTTP 403", self.said[-1])
        self.assertFalse(self.b.busy.locked())

    def test_busy(self):
        self.b.busy.acquire()
        self.b.handle(1, 42, "кафе Казань")
        self.assertIn("подождите", self.said[-1])

    def test_old_state_format(self):
        bot.STATE.write_text('{"owner": 1, "offset": 5, "seen": [], "skip": [], "last": {"a.ru": {}}}', encoding="utf-8")
        self.assertEqual(bot.load_state()["last"], [])

    def test_long_message_split(self):
        b = bot.Bot("TOKEN")
        sent = []
        b.call = lambda method, **kw: sent.append(kw["text"])
        b.say(1, "\n\n".join(["x" * 1000] * 10))
        self.assertTrue(len(sent) >= 3 and all(len(t) <= 4096 for t in sent))
        self.assertEqual(sum(t.count("x") for t in sent), 10000)
        sent.clear()
        b.say(1, "y" * 9000)
        self.assertEqual([len(t) for t in sent], [4000, 4000, 1000])


if __name__ == "__main__":
    unittest.main()
