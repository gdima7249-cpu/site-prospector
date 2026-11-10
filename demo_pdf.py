"""PDF «как мог бы выглядеть ваш сайт»: снимки демо на телефоне и на компьютере в одном файле.

Демо открывается браузером Chromium (Playwright) на внутреннем адресе 127.0.0.1 — публичный адрес для этого не нужен.
Анимации появления отключаются, чтобы на снимках были видны все блоки.
"""
from __future__ import annotations

import base64
import functools
import html
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MOBILE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True,
          "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/17.0 Mobile/15E148 Safari/604.1"}
DESKTOP = {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1}
SECTIONS = ("#services", "#rating", "#contacts")  # что снимать после первого экрана
# всё видно сразу: без анимаций появления, бегущей строки и плавных переходов
FREEZE = """
html { scroll-behavior: auto !important; }
*, *::before, *::after { animation: none !important; transition: none !important; }
[data-reveal] { opacity: 1 !important; transform: none !important; }
body::after { display: none !important; }
"""


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _prepare(page):
    page.add_style_tag(content=FREEZE)
    page.evaluate("""() => {
        document.querySelectorAll('[data-reveal]').forEach(e => e.classList.add('is-in'));
        const h = document.querySelector('.hero'); if (h) h.classList.add('is-loaded');
    }""")
    page.wait_for_timeout(300)


def _goto(page, url):
    try:
        page.goto(url, wait_until="networkidle", timeout=25000)  # ждём и карту, если она успеет
    except Exception:  # noqa: BLE001 — карта или шрифт не дождались: снимаем как есть
        pass
    _prepare(page)


def _shots(page, anchors, quality) -> list[bytes]:
    """Первый экран и экран, начинающийся с каждого найденного блока."""
    out = [page.screenshot(type="jpeg", quality=quality)]
    for anchor in anchors:
        if page.query_selector(anchor):
            page.evaluate(f"""() => {{
                const el = document.querySelector('{anchor}');
                window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 20);
            }}""")
            page.wait_for_timeout(300)
            out.append(page.screenshot(type="jpeg", quality=quality))
    return out


def _b64(img: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(img).decode()


def render_pdf(demo_root: Path, token: str, name: str, out: Path, chromium_path: str | None = None) -> Path:
    """Снимает демo demo_root/<token>/ и сохраняет PDF в out."""
    from playwright.sync_api import sync_playwright  # тяжёлая зависимость — только когда нужен PDF

    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(demo_root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/{token}/"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"], **({"executable_path": chromium_path} if chromium_path else {}))
            try:
                # телефон и компьютер: первый экран и смысловые блоки (услуги, рейтинг, контакты)
                m = browser.new_context(**MOBILE).new_page()
                _goto(m, url)
                phone = _shots(m, ("#services", "#contacts"), 82)
                d = browser.new_context(**DESKTOP).new_page()
                _goto(d, url)
                slices = _shots(d, SECTIONS, 80)
                # PDF: титульная страница с телефоном, дальше — компьютер
                e = html.escape
                phones = "".join(f'<img class="ph" src="{_b64(x)}">' for x in phone)
                desks = "".join(f'<section class="pg"><img class="dk" src="{_b64(x)}"></section>' for x in slices)
                doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>
                  @page {{ size: A4 landscape; margin: 0; }}
                  body {{ margin: 0; font-family: Arial, sans-serif; background: #0a0a0b; color: #f2f2f0; }}
                  .pg {{ width: 297mm; height: 210mm; box-sizing: border-box; padding: 9mm 12mm; page-break-after: always;
                        display: flex; flex-direction: column; align-items: center; justify-content: center; overflow: hidden; }}
                  .pg:last-child {{ page-break-after: auto; }}
                  h1 {{ font-size: 22pt; margin: 0 0 2mm; letter-spacing: -.02em; align-self: flex-start; }}
                  p {{ margin: 0 0 6mm; color: #9a9aa0; font-size: 10.5pt; align-self: flex-start; }}
                  .row {{ display: flex; gap: 8mm; justify-content: center; }}
                  .ph {{ height: 150mm; border-radius: 6mm; border: 2.5mm solid #26262a; }}
                  .dk {{ max-width: 100%; max-height: 100%; border: 1px solid #26262a; }}
                </style></head><body>
                <section class="pg"><h1>Как может выглядеть сайт «{e(name)}»</h1>
                  <p>Демо-концепт на телефоне. Данные взяты из открытых источников и могут быть неточны; это не официальный сайт.</p>
                  <div class="row">{phones}</div></section>
                {desks}</body></html>"""
                pdf_page = browser.new_page()
                pdf_page.set_content(doc, wait_until="load")
                out.parent.mkdir(parents=True, exist_ok=True)
                pdf_page.pdf(path=str(out), format="A4", landscape=True, print_background=True,
                             margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
            finally:
                browser.close()
    finally:
        srv.shutdown()
    return out
