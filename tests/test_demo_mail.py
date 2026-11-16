"""Тесты сборщика демо и почты (SMTP и IMAP подменены)."""
import email
import re
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import demo_builder as d  # noqa: E402
import leads  # noqa: E402
import mailer  # noqa: E402

try:
    import playwright  # noqa: F401
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False


class Demo(unittest.TestCase):
    def test_hours(self):
        h = d.parse_osm_hours("Mo-Fr 09:00-18:00; Sa 10:00-15:00; Su off")
        self.assertEqual(h["1"], ["09:00", "18:00"])
        self.assertNotIn("7", h)
        self.assertEqual(d.hours_human(h), "Пн–Пт 9:00–18:00<br>Сб 10:00–15:00<br>Вс — выходной")
        self.assertEqual(d.parse_osm_hours("Fr-Mo 10:00-20:00").keys(), {"5", "6", "7", "1"})
        self.assertEqual(d.hours_human(d.parse_osm_hours("24/7")), "Пн–Вс круглосуточно")
        self.assertIn("1", d.parse_osm_hours("Mo-Fr 09:00-18:00; PH off"))
        self.assertIsNone(d.parse_osm_hours("Mo-Fr 09:00-13:00,14:00-18:00"))  # сложный формат — без бейджа
        self.assertIsNone(d.parse_osm_hours("пн-пт 9:00–18:00"))

    def test_helpers(self):
        self.assertEqual([d.plural(n, "отзыв", "отзыва", "отзывов") for n in (1, 3, 5, 11, 21, 64)],
                         ["отзыв", "отзыва", "отзывов", "отзывов", "отзыв", "отзыва"])
        self.assertEqual(d.phone_parts("8 (3812) 60-51-23; +7 913"), ("tel:+73812605123", "8 (3812) 60-51-23"))
        self.assertEqual(d.phone_parts(""), ("#", ""))

    def test_build(self):
        niches = leads.load_niches()
        b = leads.new_business(name="Кузов <b>&</b> Ко", phone="+7 (913) 977-45-07", address="Заводская улица, 1/2",
                               hours="Mo-Fr 09:00-18:00; Sa 10:00-15:00", rating=4.6, reviews=64, sources=["2ГИС"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tok = d.build_demo(b, "кузовной ремонт", niches["кузовной ремонт"], "Омск", {"name": "Дмитрий"}, root, "simple")
            self.assertGreaterEqual(len(tok), 12)
            page = (root / tok / "index.html").read_text(encoding="utf-8")
            self.assertIn("Кузов &lt;b&gt;&amp;&lt;/b&gt; Ко", page)  # HTML из данных не исполняется
            self.assertNotIn("<b>&</b>", page)
            self.assertIsNone(re.search(r"\$[a-z_]+", page))  # все подстановки заполнены
            self.assertNotIn("Гараж 55", page)
            self.assertIn('data-tz="Asia/Omsk"', page)
            self.assertIn("Пн–Пт 9:00–18:00", page)
            self.assertIn("4,6", page)
            self.assertIn("64 отзыва в 2ГИС", page)
            self.assertIn("подготовил Дмитрий", page)
            self.assertIn('name="robots" content="noindex', page)
            self.assertIn("Удаление вмятин", page)
            self.assertNotIn("от 2 500", page)  # никаких цен из эталона
            self.assertNotIn('id="praise"', page)  # «За что хвалят» без отзывов не заполняем
            self.assertTrue((root / tok / "img" / "hero-car.svg").exists())
            self.assertIn("Disallow: /", (root / "robots.txt").read_text())
            tok2 = d.build_demo(leads.new_business(name="Без данных"), "автосервис", niches["автосервис"], "Тула",
                                {"name": "Д"}, root, "simple")
            page2 = (root / tok2 / "index.html").read_text(encoding="utf-8")
            self.assertNotIn('id="status"', page2)
            self.assertNotIn('id="rating"', page2)
            self.assertIn("уточняйте по телефону", page2)
            with self.assertRaises(ValueError):
                d.build_demo(b, "стоматология", niches["стоматология"], "Омск", {}, root)


    def test_build_studio(self):
        niches = leads.load_niches()
        b = leads.new_business(name="Кузов <b>&</b> Ко", phone="+7 (913) 977-45-07", address="ул. Заводская, 1/2",
                               hours="Mo-Fr 09:00-18:00; Sa 10:00-15:00", rating=4.6, reviews=64, sources=["2ГИС"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tok = d.build_demo(b, "кузовной ремонт", niches["кузовной ремонт"], "Омск", {"name": "Дмитрий"}, root)
            page = (root / tok / "index.html").read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\$\{?[a-z_]+", page))
            self.assertIn("Кузов &lt;b&gt;&amp;&lt;/b&gt; Ко", page)
            self.assertIn('data-tz="Asia/Omsk"', page)
            self.assertIn('"6": ["10:00", "15:00"]', page)
            self.assertIn("Кузовной ремонт<br><span class=\"hero__title-muted\">и покраска</span>", page)
            self.assertIn("tel:+79139774507", page)
            self.assertIn('id="rating"', page)
            self.assertIn("noindex", page)
            # ничего из настоящего сайта kuzovomsk.ru: ни цен, ни отзывов, ни обещаний, ни фото их цеха
            for banned in ("от 5 000", "DOOCAR", "Mitra", "1999", "гарантия до", "Павел", "svc-geometry", "svc-bumper",
                           "muhachev", "908 807", "калькулятор", "Калькулятор"):
                self.assertNotIn(banned, page)
            # для каждого использованного фото CC BY — автор в подвале
            for img in set(re.findall(r"img/(svc-[a-z]+-800|tech-1000)\.webp", page)):
                self.assertIn(d.STUDIO_PHOTOS[img].split("</a>")[0], page)
            self.assertIn("Andrew Milligan", page)
            assets = root / "_assets" / "studio"
            for f in ("css/styles.css", "css/demo.css", "js/demo.js", "fonts/inter-cyrillic-wght-normal.woff2",
                      "img/hero-1600.jpg", "favicon.svg"):
                self.assertTrue((assets / f).exists(), f)
            self.assertFalse((assets / "index.tpl.html").exists())
            self.assertFalse(any("geometry" in p.name or "bumper" in p.name for p in (assets / "img").iterdir()))
            for ref in re.findall(r'(?:href|src)="\.\./_assets/studio/([^"#?]+)"', page):
                self.assertTrue((assets / ref).exists(), ref)  # все ссылки на файлы дизайна рабочие
            bare = d.build_demo(leads.new_business(name="Шины"), "шиномонтаж", niches["шиномонтаж"], "Тула", {"name": "Д"}, root)
            page2 = (root / bare / "index.html").read_text(encoding="utf-8")
            self.assertNotIn('id="rating"', page2)
            self.assertIn("data-hours='{}'", page2)
            self.assertIn("Режим уточняйте по телефону", page2)


    # Тексты кузовного ремонта, которых не должно быть в демо шиномонтажа и автомойки
    BODY_REPAIR = ("Кузовной", "кузовн", "покраск", "Покраска", "вмятин", "бампер", "Бампер", "ДТП", "страхов",
                   "что случилось", "после ремонта", "svc-body", "svc-paint", "work-dent", "work-bumper", "work-paint",
                   "Welder", "respray")

    def test_tyre_and_wash_demos(self):
        niches = leads.load_niches()
        cases = {
            "шиномонтаж": ("Шиномонтаж", "и балансировка", ["Сезонная замена шин", "Балансировка", "Ремонт проколов",
                                                            "Хранение шин", "Правка дисков"],
                           "Мастер посмотрит колёса", "Поезжайте дальше"),
            "автомойка": ("Автомойка", "и уход за авто", ["Мойка кузова", "Комплексная мойка", "Химчистка салона",
                                                          "Полировка"],
                          "Скажите, какая мойка нужна", "Заберите чистую машину"),
        }
        b = leads.new_business(name="Колесо", phone="+7 (913) 111-22-33", address="ул. Лукашевича, 10",
                               hours="Mo-Su 08:00-22:00", rating=4.8, reviews=21, sources=["2ГИС"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for niche, (head, rest, services, step2, step3) in cases.items():
                with self.subTest(niche=niche, template="studio"):
                    page = (root / d.build_demo(b, niche, niches[niche], "Омск", {"name": "Д"}, root)
                            / "index.html").read_text(encoding="utf-8")
                    self.assertIsNone(re.search(r"\$\{?[a-z_]+", page))
                    self.assertIn(f'{head}<br><span class="hero__title-muted">{rest}</span>', page)
                    self.assertIn(niches[niche]["demo"]["what"], page)
                    for s in services:
                        self.assertIn(f'<span class="svc__title">{s}</span>', page)
                    self.assertIn(step2, page)
                    self.assertIn(step3, page)
                    self.assertIn('href="tel:+79131112233">+7 (913) 111-22-33</a>&nbsp;— узнайте', page)
                    self.assertIn("Адрес: Омск, ул. Лукашевича, 10.", page)
                    for bad in self.BODY_REPAIR:
                        self.assertNotIn(bad, page)
                    used = set(re.findall(r"img/(svc-[a-z]+-800|tech-1000)\.webp", page))
                    self.assertTrue(used)
                    self.assertTrue(used <= set(niches[niche]["demo"]["photos"]))
                    for img in used:  # авторы фото CC BY — в подвале
                        self.assertIn(d.STUDIO_PHOTOS[img].split("</a>")[0], page)
                    preview = re.search(r'class="preview"[^>]*><img src="[^"]*img/([a-z0-9-]+)\.webp"', page).group(1)
                    self.assertEqual(preview, niches[niche]["demo"]["photos"][0])
                with self.subTest(niche=niche, template="simple"):
                    tok = d.build_demo(b, niche, niches[niche], "Омск", {"name": "Д"}, root, "simple")
                    page = (root / tok / "index.html").read_text(encoding="utf-8")
                    self.assertIsNone(re.search(r"\$[a-z_]+", page))
                    for s in services:
                        self.assertIn(s, page)
                    works = re.findall(r'<div class="cap"><b>([^<]+)</b>', page)
                    self.assertEqual(works, [w["title"] for w in niches[niche]["demo"]["works"]])
                    for bad in self.BODY_REPAIR:
                        self.assertNotIn(bad, page)
                    for img in re.findall(r'src="img/([^"]+)"', page):
                        self.assertTrue((root / tok / "img" / img).exists(), img)

    def test_body_repair_demo_keeps_defaults(self):
        """Без своих photos/steps/works в нише — тексты и фото эталона, как раньше."""
        niches = leads.load_niches()
        b = leads.new_business(name="Кузов", phone="+7 913 977-45-07", address="ул. Заводская, 1")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = (root / d.build_demo(b, "кузовной ремонт", niches["кузовной ремонт"], "Омск", {}, root)
                    / "index.html").read_text(encoding="utf-8")
            self.assertIn("&nbsp;— расскажите, что случилось.", page)
            self.assertIn("<h3>Приезжайте на&nbsp;осмотр</h3><p>Адрес: Омск, ул. Заводская, 1. Мастер посмотрит "
                          "автомобиль и&nbsp;назовёт стоимость.</p>", page)
            self.assertIn("Сроки и&nbsp;цену согласуем до&nbsp;начала работ.", page)
            self.assertIn('img/svc-body-800.webp" alt=""></div>', page)  # фото у курсора
            page = (root / d.build_demo(b, "кузовной ремонт", niches["кузовной ремонт"], "Омск", {}, root, "simple")
                    / "index.html").read_text(encoding="utf-8")
            self.assertEqual(re.findall(r'<div class="cap"><b>([^<]+)</b>', page),
                             ["Покраска", "Кузовные работы", "Полировка", "Бампер"])

    def test_bad_niche_demo_config(self):
        b = leads.new_business(name="X")
        base = {"what": "Шиномонтаж", "services": ["Балансировка"]}
        with tempfile.TemporaryDirectory() as tmp:
            for extra, tpl in (({"photos": ["svc-nope-800"]}, "studio"), ({"photos": ["hero"]}, "studio"),
                               ({"steps": [{"title": "a", "text": "b"}]}, "studio"),
                               ({"works": [{"title": "a", "img": "nope.svg"}]}, "simple")):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    d.build_demo(b, "x", {"demo": {**base, **extra}}, "Тула", {}, Path(tmp), tpl)

    @unittest.skipUnless(HAVE_PLAYWRIGHT, "нет playwright — PDF не проверить")
    def test_render_pdf(self):
        import demo_pdf
        niches = leads.load_niches()
        b = leads.new_business(name="Центр Кузовного Ремонта", phone="+7 913 977-45-07", address="ул. Заводская, 1/2",
                               hours="Mo-Fr 09:00-18:00", rating=4.6, reviews=64, sources=["2ГИС"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demos"
            tok = d.build_demo(b, "кузовной ремонт", niches["кузовной ремонт"], "Омск", {"name": "Д"}, root)
            out = demo_pdf.render_pdf(root, tok, b["name"], Path(tmp) / "pdf" / "x.pdf")
            data = out.read_bytes()
            self.assertTrue(data.startswith(b"%PDF"))
            pages = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
            self.assertEqual(pages, 5)  # телефон + первый экран, услуги, рейтинг, контакты
            self.assertLess(len(data), 3_000_000)


def make_msg(frm, subject, body):
    m = EmailMessage()
    m["From"], m["Subject"] = frm, subject
    m.set_content(body)
    return email.message_from_bytes(m.as_bytes())


class Mail(unittest.TestCase):
    def test_classify(self):
        quote = "\n\nВт, 10 нояб. 2026 г. в 10:00, Дмитрий <me@my.ru>:\n> Если не актуально — ответьте «нет»"
        c = mailer.classify(make_msg("Иван <boss@romashka.ru>", "Re: Сайт", "Да, интересно, позвоните" + quote))
        self.assertEqual((c["kind"], c["from"], c["text"]), ("reply", "boss@romashka.ru", "Да, интересно, позвоните"))
        self.assertEqual(mailer.classify(make_msg("a@b.ru", "Re: Сайт", "Нет, спасибо" + quote))["kind"], "refusal")
        self.assertEqual(mailer.classify(make_msg("a@b.ru", "Re", "Добрый день, нам не интересно"))["kind"], "refusal")
        self.assertEqual(mailer.classify(make_msg("a@b.ru", "Re", "Нет проблем, давайте созвонимся"))["kind"], "reply")
        self.assertEqual(mailer.classify(make_msg("a@b.ru", "unsubscribe", ""))["kind"], "refusal")
        self.assertEqual(mailer.classify(make_msg("Google <no-reply@accounts.google.com>", "Оповещение системы безопасности",
                                                  "Создан пароль приложения"))["kind"], "auto")
        news = make_msg("Магазин <shop@example.ru>", "Скидки", "Купите")
        news["List-Unsubscribe"] = "<mailto:x@example.ru>"
        self.assertEqual(mailer.classify(news)["kind"], "auto")
        robot = make_msg("bot@service.ru", "Уведомление", "текст")
        robot["Auto-Submitted"] = "auto-generated"
        self.assertEqual(mailer.classify(robot)["kind"], "auto")
        bounce = mailer.classify(make_msg("Mail Delivery Subsystem <mailer-daemon@yandex.ru>", "Недоставленное сообщение",
                                          "Final-Recipient: rfc822; info@dead.ru\nStatus: 5.1.1"))
        self.assertEqual((bounce["kind"], bounce["failed"]), ("bounce", "info@dead.ru"))

    def test_send(self):
        with mock.patch.object(mailer.smtplib, "SMTP_SSL") as ssl_cls:
            mid = mailer.send({"host": "smtp.yandex.ru", "port": 465, "user": "me@my.ru", "password": "p",
                               "from_name": "Дмитрий"}, "boss@romashka.ru", "Тема", "Текст")
        srv = ssl_cls.return_value
        srv.login.assert_called_once_with("me@my.ru", "p")
        msg = srv.send_message.call_args.args[0]
        self.assertEqual(msg["To"], "boss@romashka.ru")
        self.assertIn("unsubscribe", msg["List-Unsubscribe"])
        self.assertIn("my.ru", mid)
        srv.quit.assert_called_once()
        with mock.patch.object(mailer.smtplib, "SMTP_SSL") as ssl_cls:
            mailer.send({"host": "h", "port": 465, "user": "u@x.ru", "password": "p"}, "a@b.ru", "s", "b",
                        [("Сайт-Ромашка.pdf", b"%PDF-1.4 x")])
        att = list(ssl_cls.return_value.send_message.call_args.args[0].iter_attachments())
        self.assertEqual((att[0].get_filename(), att[0].get_content_type()), ("Сайт-Ромашка.pdf", "application/pdf"))
        with mock.patch.object(mailer.smtplib, "SMTP") as plain:
            mailer.send({"host": "h", "port": 587, "user": "u@x.ru", "password": "p"}, "a@b.ru", "s", "b")
        plain.return_value.starttls.assert_called_once()

    def test_fetch_new(self):
        raw = make_msg("boss@romashka.ru", "Re: Сайт", "Да").as_bytes()
        box = mock.MagicMock()
        box.uid.side_effect = lambda cmd, *a: ("OK", [b"1 2"]) if cmd == "search" else ("OK", [(b"2 (BODY[]", raw), b")"])
        with mock.patch.object(mailer.imaplib, "IMAP4_SSL", return_value=box):
            got = mailer.fetch_new({"host": "imap", "user": "u", "password": "p"}, seen={"1"})
        self.assertEqual([(u, e["kind"]) for u, e in got], [("2", "reply")])
        box.select.assert_called_once_with("INBOX", readonly=True)
        fetch_args = [c.args for c in box.uid.call_args_list if c.args[0] == "fetch"]
        self.assertEqual(fetch_args, [("fetch", b"2", "(BODY.PEEK[])")])  # письмо не помечается прочитанным


if __name__ == "__main__":
    unittest.main()
