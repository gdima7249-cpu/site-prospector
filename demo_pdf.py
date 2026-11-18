"""PDF «как мог бы выглядеть ваш сайт»: снимки демо на телефоне и на компьютере в одном файле.

Демо открывается браузером Chromium (Playwright) на внутреннем адресе 127.0.0.1 — публичный адрес для этого не нужен.
Анимации появления отключаются, чтобы на снимках были видны все блоки.

Почему раньше PDF иногда не собирался («браузер не отвечает», TimeoutError, Target closed):
- браузер Chromium занимает несколько сотен МБ памяти. Если нажать «Сделать демо» на нескольких карточках подряд,
  запускалось несколько браузеров сразу, и на небольшом сервере кончалась память — система их убивала;
- шрифты демо грузятся с fonts.googleapis.com. Если сервер шрифтов отвечал медленно, снимок ждал их и падал по
  таймауту.
Теперь PDF собираются строго по одному, внешние файлы ждутся не дольше EXTERNAL_WAIT_MS (дальше — запасной шрифт),
браузер запускается с настройками для серверов с малой памятью, а неудачная попытка повторяется один раз.
"""
from __future__ import annotations

import base64
import functools
import importlib.util
import html
import threading
import time
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
# настройки браузера для небольшого сервера: без /dev/shm (на VPS он маленький — из-за него Chromium падал), без GPU
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-extensions",
               "--mute-audio", "--no-first-run"]
EXTERNAL_WAIT_MS = 8000   # сколько ждать внешний файл (шрифт Google): дольше — снимаем с запасным шрифтом
PAGE_TIMEOUT_MS = 30000   # общий таймаут действий на странице
_ONE_AT_A_TIME = threading.Lock()  # один браузер за раз: несколько параллельных не помещаются в память сервера


def memory_mb() -> tuple[int, int] | None:
    """(свободно, всего) МБ памяти сервера из /proc/meminfo; None — не Linux."""
    try:
        info = dict(line.split(":", 1) for line in open("/proc/meminfo", encoding="ascii"))
        kb = lambda k: int(info[k].strip().split()[0])  # noqa: E731
        return kb("MemAvailable") // 1024, kb("MemTotal") // 1024
    except (OSError, KeyError, ValueError):
        return None


def busy() -> bool:
    """Собирается ли сейчас другой PDF (новый встанет в очередь)."""
    return _ONE_AT_A_TIME.locked()


def explain(e: BaseException) -> str:
    """Понятная причина ошибки браузера для сообщения в Telegram."""
    text = f"{type(e).__name__}: {e}".lower()
    mem = memory_mb()
    mem_note = f" Сейчас свободно {mem[0]} МБ памяти из {mem[1]} МБ." if mem else ""
    if isinstance(e, ModuleNotFoundError) or "executable doesn't exist" in text or "playwright install" in text:
        return "на сервере не установлен браузер Chromium (см. README, раздел про PDF)"
    if "timeout" in text:
        return "браузер не успел снять демо за отведённое время — сервер был занят или медленно грузились шрифты." + mem_note
    if any(x in text for x in ("target closed", "crash", "browser has been closed", "connection closed", "killed",
                               "out of memory", "sigkill", "sigtrap")):
        return "браузер аварийно закрылся — чаще всего так бывает, когда серверу не хватает памяти." + mem_note
    return f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}".strip(": ") + mem_note


def available() -> bool:
    """Установлен ли Playwright (на маленьком сервере установщик может его пропустить)."""
    return importlib.util.find_spec("playwright") is not None


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


def _route_external(route):
    """Свои файлы демо — как есть; чужие (шрифты Google) — не дольше EXTERNAL_WAIT_MS, иначе без них."""
    url = route.request.url
    if url.startswith(("http://127.0.0.1", "data:", "blob:")):
        return route.continue_()
    try:
        route.fulfill(response=route.fetch(timeout=EXTERNAL_WAIT_MS))
    except Exception:  # noqa: BLE001 — не дождались: страница возьмёт запасной шрифт
        try:
            route.abort()
        except Exception:  # noqa: BLE001
            pass


def _new_page(browser, **ctx):
    context = browser.new_context(**ctx)
    context.set_default_timeout(PAGE_TIMEOUT_MS)
    context.route("**/*", _route_external)
    return context, context.new_page()


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


def render_pdf(demo_root: Path, token: str, name: str, out: Path, chromium_path: str | None = None,
               attempts: int = 2) -> Path:
    """Снимает демо demo_root/<token>/ и сохраняет PDF в out. Одновременно собирается только один PDF
    (остальные ждут своей очереди); неудачная попытка повторяется ещё раз через несколько секунд."""
    from playwright.sync_api import sync_playwright  # noqa: F401 — тяжёлая зависимость: нет её — ошибка сразу

    with _ONE_AT_A_TIME:
        for attempt in range(attempts):
            try:
                return _render(demo_root, token, name, out, chromium_path)
            except ModuleNotFoundError:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt + 1 >= attempts or "executable doesn't exist" in str(e).lower():
                    raise
                print(f"PDF: попытка {attempt + 1} не удалась ({type(e).__name__}: {str(e)[:200]}), повторяю")
                time.sleep(5)
    raise RuntimeError("unreachable")


def _render(demo_root: Path, token: str, name: str, out: Path, chromium_path: str | None) -> Path:
    from playwright.sync_api import sync_playwright

    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(demo_root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/{token}/"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=LAUNCH_ARGS, timeout=60000,
                                         **({"executable_path": chromium_path} if chromium_path else {}))
            try:
                # телефон и компьютер: первый экран и смысловые блоки (услуги, рейтинг, контакты).
                # Каждую вкладку закрываем сразу после снимков — так браузеру нужно меньше памяти.
                ctx, m = _new_page(browser, **MOBILE)
                _goto(m, url)
                phone = _shots(m, ("#services", "#contacts"), 82)
                ctx.close()
                ctx, d = _new_page(browser, **DESKTOP)
                _goto(d, url)
                slices = _shots(d, SECTIONS, 80)
                ctx.close()
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
                pdf_page.set_default_timeout(PAGE_TIMEOUT_MS)
                pdf_page.set_content(doc, wait_until="load")
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(".part")
                pdf_page.pdf(path=str(tmp), format="A4", landscape=True, print_background=True,
                             margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
                tmp.replace(out)  # недописанный файл не останется под именем готового PDF
            finally:
                browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
    return out
