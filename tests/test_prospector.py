"""Тесты на локальном сервере: настоящие сайты не нужны.

Запуск: python -m unittest discover -s tests
"""
import base64
import csv
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prospector as p  # noqa: E402

OLD = """<html><head><title>ООО Ромашка</title></head><body>
<center><font size=4>Добро пожаловать!</font></center>
<a href="/contacts">Контакты</a> <a href="/broken">Прайс</a> <a href="/ok">Услуги</a>
<img src="logo@2x.png"><p>© 2012–2015 ООО Ромашка</p></body></html>"""
CONTACTS = """<html><body>Пишите: <a href="mailto:info@romashka.ru">info</a>,
ivan.petrov@romashka.ru, test@example.com</body></html>"""
GOOD = f"""<!doctype html><html><head><title>Хороший сайт</title>
<meta name="viewport" content="width=device-width"><meta name="description" content="Описание">
</head><body><a href="/ok">ok</a><footer>© {p.YEAR}</footer></body></html>"""
ROBOTS = "User-agent: *\nDisallow: /private\n"
PARKED = "<html><body><h1>Этот домен продаётся!</h1><p>Купить домен romashka.ru</p></body></html>"
PAGES = {"/parked/": PARKED, "/old/": OLD, "/contacts": CONTACTS, "/ok": "<html>ok</html>", "/good/": GOOD,
         "/private/": GOOD, "/robots.txt": ROBOTS}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def audit(self, path):
        return p.audit_site(self.base + path, p.Fetcher(p.DEFAULT_UA), 10, False, None)

    def test_old_site(self):
        a = self.audit("/old/")
        self.assertTrue(a.ok, a.error)
        for k in ("no_https", "no_viewport", "no_description", "old_markup", "old_copyright", "broken_links"):
            self.assertIn(k, a.issues)
        self.assertEqual(a.copyright_year, 2015)
        self.assertEqual(a.issues["broken_links"]["count"], 1)
        # общий ящик идёт первым, мусор и чужие домены отброшены
        self.assertEqual(a.emails, ["info@romashka.ru", "ivan.petrov@romashka.ru"])
        self.assertGreaterEqual(a.score, 70)

    def test_good_site(self):
        a = self.audit("/good/")
        self.assertEqual(set(a.issues), {"no_https"})  # локальный сервер без TLS

    def test_robots(self):
        a = self.audit("/private/")
        self.assertFalse(a.ok)
        self.assertIn("robots", a.error)

    def test_unreachable(self):
        a = p.audit_site("http://127.0.0.1:1/", p.Fetcher(p.DEFAULT_UA), 0, False, None)
        self.assertFalse(a.ok)

    def test_template_has_only_real_issues(self):
        a = self.audit("/old/")
        me = {"name": "Дмитрий", "company": "", "offer": "Могу бесплатно прислать разбор.", "signature": "Дмитрий"}
        subject, body = p.template_draft(a, me)
        self.assertIn("127.0.0.1", subject)
        self.assertIn("больше писать не буду", body)
        self.assertEqual(body.count("\n— "), 3)

    def test_full_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "urls.txt").write_text(f"# комментарий\n{self.base}/old/\n{self.base}/good/\n", encoding="utf-8")
            (d / "sender.json").write_text(json.dumps({"name": "Д", "offer": "О", "signature": "П"}), encoding="utf-8")
            p.main(["audit", str(d / "urls.txt"), "-o", str(d / "out"), "--sender", str(d / "sender.json"),
                    "--suppress", str(d / "none.txt")])
            # оба URL на одном хосте 127.0.0.1 → после дедупликации по домену остаётся один
            rows = list(csv.reader((d / "out/report.csv").open(encoding="utf-8-sig"), delimiter=";"))
            self.assertEqual(len(rows), 2)
            drafts = list((d / "out/drafts").glob("*.txt"))
            self.assertEqual(len(drafts), 1)
            h, body = p.read_draft(drafts[0])
            self.assertEqual(h["to"], "info@romashka.ru")

            # send без одобрения ничего не делает
            p.main(["send", "-o", str(d / "out"), "--suppress", str(d / "none.txt")])
            self.assertFalse((d / "out/sent.csv").exists())

            drafts[0].rename(d / "out/approved" / drafts[0].name)
            fake = mock.MagicMock()
            env = {"SMTP_USER": "me@my.ru", "SMTP_FROM_NAME": "Дмитрий"}
            with mock.patch.object(p, "smtp_connect", return_value=fake), mock.patch.dict(os.environ, env):
                p.main(["send", "-o", str(d / "out"), "--suppress", str(d / "none.txt"), "--delay", "0"])
            msg = fake.send_message.call_args[0][0]
            self.assertEqual(msg["To"], "info@romashka.ru")
            self.assertIn("unsubscribe", msg["List-Unsubscribe"])
            self.assertTrue((d / "out/sent" / drafts[0].name).exists())

            # повторный audit не берёт уже обработанный домен
            (d / "out/approved").mkdir(exist_ok=True)
            p.main(["audit", str(d / "urls.txt"), "-o", str(d / "out"), "--sender", str(d / "sender.json"),
                    "--suppress", str(d / "none.txt")])
            self.assertEqual(json.loads((d / "out/report.json").read_text(encoding="utf-8")), [])

    def test_flash(self):
        PAGES["/flash/"] = '<html><body><object data="intro.swf"></object></body></html>'
        self.assertIn("flash", self.audit("/flash/").issues)

    def test_llm_draft_parsing(self):
        a = self.audit("/old/")
        block = mock.Mock(type="text", text="Тема: Сайт romashka.ru\n\nЗдравствуйте!\nТекст.")
        resp = mock.Mock(stop_reason="end_turn", content=[block])
        fake = mock.MagicMock()
        fake.Anthropic.return_value.beta.messages.create.return_value = resp
        with mock.patch.dict(sys.modules, {"anthropic": fake}):
            subject, body = p.llm_draft(a, {"name": "Д"}, "claude-opus-5-5")
        kw = fake.Anthropic.return_value.beta.messages.create.call_args.kwargs
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertIn("проблемы", kw["messages"][0]["content"])
        self.assertEqual((subject, body), ("Сайт romashka.ru", "Здравствуйте!\nТекст."))
        resp.stop_reason = "refusal"
        with mock.patch.dict(sys.modules, {"anthropic": fake}), self.assertRaises(p.LLMRefused):
            p.llm_draft(a, {"name": "Д"}, "claude-opus-5-5")

    def test_yandex_search(self):
        xml = """<?xml version="1.0" encoding="utf-8"?><yandexsearch version="1.0"><response><results><grouping>
<group><doc><url>https://romashka.ru/</url><domain>romashka.ru</domain></doc></group>
<group><doc><url>https://www.avito.ru/kazan/uslugi</url></doc></group>
<group><doc><url>https://spb.zoon.ru/x</url></doc></group>
<group><doc><url>http://www.lyutik-dent.ru/about</url></doc></group>
<group><doc><url>https://already.ru/</url></doc></group>
</grouping></results></response></yandexsearch>"""
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"rawData": base64.b64encode(xml.encode()).decode()}
        empty_xml = '<yandexsearch><response><error code="15">Нет результатов</error></response></yandexsearch>'
        empty = mock.Mock(status_code=200)
        empty.json.return_value = {"rawData": base64.b64encode(empty_xml.encode()).decode()}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "urls.txt"
            out.write_text("already.ru\n", encoding="utf-8")
            env = {"YANDEX_SEARCH_API_KEY": "k", "YANDEX_FOLDER_ID": "f"}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(p.requests, "post", side_effect=[ok, empty]) as post:
                p.main(["search", "стоматология Казань", "--pages", "5", "--output", str(out), "--delay", "0",
                        "--region", "213", "--suppress", str(Path(d) / "none.txt")])
            self.assertEqual(out.read_text(encoding="utf-8").split(), ["already.ru", "romashka.ru", "lyutik-dent.ru"])
            self.assertEqual(post.call_count, 2)  # вторая страница пустая — дальше не идём
            kw = post.call_args_list[0].kwargs
            self.assertEqual(kw["headers"]["Authorization"], "Api-Key k")
            self.assertEqual(kw["json"]["query"]["queryText"], "стоматология Казань")
            self.assertEqual(kw["json"]["folderId"], "f")
            self.assertEqual(kw["json"]["region"], "213")

    def test_yandex_error(self):
        bad = mock.Mock(status_code=403, text="Permission denied")
        with mock.patch.object(p.requests, "post", return_value=bad), self.assertRaises(p.YandexError):
            p.yandex_search("q", 0, "k", "f", None)

    def test_parked_and_dead(self):
        a = self.audit("/parked/")
        self.assertFalse(a.ok)
        self.assertIn("заглушка", a.error)
        a = self.audit("/nope/")
        self.assertFalse(a.ok)
        self.assertIn("404", a.error)

    def test_email_order(self):
        emails = ["lerner1966@mail.ru", "zyb-ok@mail.ru", "ivan.petrov@gmail.com", "info@clinic.ru", "doc@zyb-ok.ru"]
        ranked = sorted(emails, key=lambda e: p.email_rank(e, "zyb-ok.ru"))
        self.assertEqual(ranked[:2], ["doc@zyb-ok.ru", "zyb-ok@mail.ru"])
        self.assertEqual(set(ranked[-2:]), {"lerner1966@mail.ru", "ivan.petrov@gmail.com"})
        self.assertTrue(p.looks_personal("lerner1966@mail.ru"))
        self.assertFalse(p.looks_personal("zyb-ok@mail.ru"))

    def test_bad_cert_not_counted_twice(self):
        fetcher = mock.Mock()
        fetcher.allowed.return_value = True
        page = mock.Mock(url="http://zyb-ok.ru/", status_code=200, text=GOOD, content=GOOD.encode())
        fetcher.get.side_effect = [p.requests.exceptions.SSLError(), p.requests.exceptions.SSLError(), page]
        a = p.audit_site("zyb-ok.ru", fetcher, 0, False, None)
        self.assertIn("bad_cert", a.issues)
        self.assertNotIn("no_https", a.issues)

    def test_suppression(self):
        block = {"romashka.ru", "boss@other.ru"}
        self.assertTrue(p.suppressed("info@romashka.ru", block))
        self.assertTrue(p.suppressed("www.romashka.ru", block))
        self.assertTrue(p.suppressed("BOSS@other.ru", block))
        self.assertFalse(p.suppressed("info@other.ru", block))


if __name__ == "__main__":
    unittest.main()
