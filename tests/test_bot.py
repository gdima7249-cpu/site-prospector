"""Тесты бота: OpenStreetMap и Telegram подменены, сайты — на локальном сервере."""
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
from test_prospector import H  # noqa: E402


def resp(data):
    r = mock.Mock(status_code=200)
    r.json.return_value = data
    r.raise_for_status.return_value = None
    return r


class BotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_state = mock.patch.object(bot, "STATE", Path(self.tmp.name) / "state.json")
        self.patch_state.start()

    def tearDown(self):
        self.patch_state.stop()
        self.tmp.cleanup()

    def test_parse_request(self):
        self.assertEqual(bot.parse_request("Стоматология  Казань"), ("стоматология", "Казань"))
        self.assertEqual(bot.parse_request("салон красоты Нижний Новгород"), ("салон красоты", "Нижний Новгород"))
        self.assertEqual(bot.parse_request("стоматолог Москва"), ("стоматология", "Москва"))
        self.assertIsNone(bot.parse_request("привет"))
        self.assertIsNone(bot.parse_request("стоматология"))

    def test_overpass_query(self):
        q = bot.overpass_query([("amenity", "dentist")], (55.6, 48.8, 55.9, 49.4))
        self.assertIn('nwr["amenity"="dentist"]["website"](55.60000,48.80000,55.90000,49.40000);', q)
        self.assertIn('["contact:website"]', q)
        self.assertTrue(q.startswith("[out:json]") and q.endswith("out tags;"))

    def osm(self):
        nominatim = resp([{"boundingbox": ["55.6", "55.9", "48.8", "49.4"]}])
        overpass = resp({"elements": [
            {"tags": {"name": "Ромашка", "website": self.base + "/old/", "email": "boss@romashka.ru"}},
            {"tags": {"name": "Хорошая", "contact:website": self.base.replace("127.0.0.1", "localhost") + "/good/"}},
            {"tags": {"name": "Дубль", "website": self.base + "/old/"}},
            {"tags": {"name": "Авито", "website": "https://www.avito.ru/x"}},
            {"tags": {"name": "Без сайта"}},
        ]})
        return mock.patch.multiple(bot.requests, get=mock.Mock(return_value=nominatim),
                                   post=mock.Mock(return_value=overpass))

    def test_find_businesses(self):
        with self.osm():
            found = bot.find_businesses("стоматология", "Казань")
            get_kw = bot.requests.get.call_args.kwargs
        self.assertEqual([b["name"] for b in found], ["Ромашка", "Хорошая"])  # дубль, Avito и без сайта отброшены
        self.assertEqual(found[0]["email"], "boss@romashka.ru")
        self.assertEqual(get_kw["params"]["q"], "Казань")
        self.assertIn("User-Agent", get_kw["headers"])

    def test_city_not_found(self):
        with mock.patch.object(bot.requests, "get", return_value=resp([])), self.assertRaises(LookupError):
            bot.find_businesses("стоматология", "Нигдеград")

    def test_overpass_mirror_fallback(self):
        nominatim = resp([{"boundingbox": ["55.6", "55.9", "48.8", "49.4"]}])
        busy = mock.Mock(status_code=504, text="timeout")
        good = resp({"elements": [{"tags": {"name": "А", "website": "https://a-dent.ru"}}]})
        with mock.patch.object(bot.requests, "get", return_value=nominatim), \
                mock.patch.object(bot.requests, "post", side_effect=[busy, good]) as post:
            found = bot.find_businesses("стоматология", "Казань")
        self.assertEqual([b["domain"] for b in found], ["a-dent.ru"])
        self.assertEqual(post.call_args_list[1].args[0], bot.OVERPASS_URLS[1])

    def test_map_errors_are_specific(self):
        nominatim = resp([{"boundingbox": ["55.6", "55.9", "48.8", "49.4"]}])
        fails = [mock.Mock(status_code=429, text=""), bot.requests.ConnectionError(), mock.Mock(status_code=504, text="")]
        with mock.patch.object(bot.requests, "get", return_value=nominatim), \
                mock.patch.object(bot.requests, "post", side_effect=fails), self.assertRaises(bot.MapError) as cm:
            bot.find_businesses("стоматология", "Казань")
        msg = str(cm.exception)
        self.assertIn("overpass-api.de: HTTP 429 — слишком много запросов", msg)
        self.assertIn("overpass.kumi.systems: ConnectionError", msg)
        self.assertIn("HTTP 504", msg)

        denied = mock.Mock(status_code=403, text="blocked")
        with mock.patch.object(bot.requests, "get", return_value=denied) as get, \
                mock.patch.object(bot.time, "sleep"), self.assertRaises(bot.MapError) as cm:
            bot.find_city("Казань")
        self.assertEqual(get.call_count, 2)  # одна повторная попытка
        self.assertIn("поиск города (nominatim.openstreetmap.org): HTTP 403 — доступ запрещён", str(cm.exception))
        self.assertIn("github.com", get.call_args.kwargs["headers"]["User-Agent"])

    def test_map_error_reaches_user(self):
        b = bot.Bot("TOKEN")
        said = []
        b.say = lambda chat, text: said.append(text)
        with mock.patch.object(bot, "find_businesses", side_effect=bot.MapError("поиск города: HTTP 403")):
            b.busy.acquire()
            b.search(1, "кафе", "Казань")
        self.assertIn("HTTP 403", said[-1])
        self.assertFalse(b.busy.locked())

    def test_search_flow(self):
        b = bot.Bot("TOKEN")
        said, files = [], []
        b.say = lambda chat, text: said.append(text)
        b.send_file = lambda chat, name, content, caption="": files.append((name, content))
        with self.osm():
            b.handle(1, 42, "/start")
            self.assertEqual(b.st["owner"], 42)
            b.handle(1, 99, "стоматология Казань")  # чужой — тишина
            self.assertEqual(len(said), 1)
            b.busy.acquire()
            b.search(1, "стоматология", "Казань")
        report = said[-1]
        self.assertIn("Ромашка", report)
        self.assertIn("boss@romashka.ru", report)
        self.assertNotIn("Хорошая", report)  # у хорошего сайта оценка ниже порога
        self.assertEqual(files[0][0], "стоматология-Казань.csv")
        self.assertIn("Ромашка".encode(), files[0][1])

        said.clear()
        b.handle(1, 42, "/draft 127.0.0.1")
        self.assertIn("Кому: boss@romashka.ru", said[-1])
        self.assertIn("больше писать не буду", said[-1])

        # повторный поиск не показывает уже проверенные сайты
        said.clear()
        with self.osm():
            b.busy.acquire()
            b.search(1, "стоматология", "Казань")
        self.assertIn("новых: 0", said[-1])

    def test_skip_and_busy(self):
        b = bot.Bot("TOKEN")
        said = []
        b.say = lambda chat, text: said.append(text)
        b.handle(1, 42, "/skip https://www.romashka.ru/")
        self.assertIn("romashka.ru", b.st["skip"])
        b.busy.acquire()
        b.handle(1, 42, "кафе Казань")
        self.assertIn("подождите", said[-1])

    def test_long_message_split(self):
        b = bot.Bot("TOKEN")
        sent = []
        b.call = lambda method, **kw: sent.append(kw["text"])
        b.say(1, "\n\n".join(["x" * 1000] * 10))
        self.assertTrue(len(sent) >= 3 and all(len(t) <= 4096 for t in sent))
        self.assertEqual(sum(t.count("x") for t in sent), 10000)
        sent.clear()
        b.say(1, "y" * 9000)  # без абзацев — режем по 4000
        self.assertEqual([len(t) for t in sent], [4000, 4000, 1000])


if __name__ == "__main__":
    unittest.main()
