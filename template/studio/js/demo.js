/* Скрипты демо-страницы. Основа — эталон kuzovomsk-site/js/main.js (шапка, меню, появление блоков,
   фото у курсора) без калькулятора, прайса, отзывов, скидок и формы.
   Часы работы и часовой пояс компании берутся из <body data-hours data-tz>. */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ---------- Плашка «демо» над фиксированной шапкой ---------- */
  var bar = $('.demo-bar');
  var setBar = function () { if (bar) document.documentElement.style.setProperty('--demo-bar-h', bar.offsetHeight + 'px'); };
  setBar();
  window.addEventListener('resize', setBar);

  /* ---------- Шапка: фон после прокрутки ---------- */
  var hdr = $('[data-hdr]');
  var onScroll = function () { hdr.classList.toggle('is-scrolled', window.scrollY > 40); };
  onScroll();
  window.addEventListener('scroll', onScroll, { passive: true });

  /* ---------- Первый экран: плавное приближение фото ---------- */
  var hero = $('.hero');
  var heroImg = $('.hero__media img');
  var heroReady = function () { requestAnimationFrame(function () { hero.classList.add('is-loaded'); }); };
  if (heroImg.complete) heroReady(); else heroImg.addEventListener('load', heroReady);
  $$('.hero [data-reveal]').forEach(function (el, i) { el.style.setProperty('--d', (0.15 + i * 0.12).toFixed(2)); });

  /* ---------- Мобильное меню ---------- */
  var burger = $('.burger');
  var nav = $('#nav');
  var setMenu = function (open) {
    burger.setAttribute('aria-expanded', String(open));
    burger.setAttribute('aria-label', open ? 'Закрыть меню' : 'Открыть меню');
    nav.classList.toggle('is-open', open);
    document.body.classList.toggle('menu-open', open);
  };
  burger.addEventListener('click', function () { setMenu(burger.getAttribute('aria-expanded') !== 'true'); });
  nav.addEventListener('click', function (e) { if (e.target.closest('a')) setMenu(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setMenu(false); });
  window.matchMedia('(min-width: 921px)').addEventListener('change', function (m) { if (m.matches) setMenu(false); });

  /* ---------- Появление при прокрутке ---------- */
  var revealEls = $$('[data-reveal]');
  if ('IntersectionObserver' in window && !reduceMotion) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add('is-in'); io.unobserve(en.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add('is-in'); });
  }

  /* ---------- Услуги: фото следует за курсором ---------- */
  var preview = $('.preview');
  if (preview && window.matchMedia('(hover: hover) and (pointer: fine)').matches && window.innerWidth > 900) {
    var pImg = $('img', preview);
    var px = 0, py = 0, tx = 0, ty = 0, raf = null;
    var loop = function () {
      px += (tx - px) * 0.16; py += (ty - py) * 0.16;
      preview.style.transform = 'translate(' + (px - 180) + 'px,' + (py - 120) + 'px) scale(' + (preview.classList.contains('is-on') ? 1 : 0.9) + ')';
      raf = Math.abs(tx - px) + Math.abs(ty - py) > 0.3 ? requestAnimationFrame(loop) : null;
    };
    $$('.svc__row').forEach(function (row) {
      var src = row.getAttribute('data-preview');
      var pre = new Image(); pre.src = src;
      row.addEventListener('mouseenter', function (e) {
        pImg.src = src; tx = px = e.clientX; ty = py = e.clientY;
        preview.classList.add('is-on');
        if (!raf) raf = requestAnimationFrame(loop);
      });
      row.addEventListener('mousemove', function (e) { tx = e.clientX; ty = e.clientY; if (!raf) raf = requestAnimationFrame(loop); });
      row.addEventListener('mouseleave', function () { preview.classList.remove('is-on'); if (!raf) raf = requestAnimationFrame(loop); });
    });
  }

  /* ---------- Открыто / закрыто — по часам компании в её часовом поясе ---------- */
  var statusEl = $('[data-open-status]');
  var hours = {};
  try { hours = JSON.parse(document.body.getAttribute('data-hours') || '{}'); } catch (e) { hours = {}; }
  if (statusEl && Object.keys(hours).length) {
    try {
      var parts = new Intl.DateTimeFormat('en-GB', { timeZone: document.body.getAttribute('data-tz') || 'Europe/Moscow',
        weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date());
      var get = function (t) { var p = parts.find(function (x) { return x.type === t; }); return p ? p.value : ''; };
      var day = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }[get('weekday')];
      var now = get('hour') + ':' + get('minute');
      var short = function (t) { return t.replace(/^0(?=\d:)/, ''); };
      var NAMES = ['', 'в понедельник', 'во вторник', 'в среду', 'в четверг', 'в пятницу', 'в субботу', 'в воскресенье'];
      var today = hours[day];
      if (today && now >= today[0] && now < today[1]) {
        statusEl.textContent = (today[0] === '00:00' && today[1] === '24:00') ? 'Открыто круглосуточно' : 'Открыто до ' + short(today[1]);
        statusEl.classList.add('is-open');
      } else {
        var label = '';
        if (today && now < today[0]) label = 'сегодня в ' + short(today[0]);
        else for (var i = 1; i <= 7; i++) { var d = (day - 1 + i) % 7 + 1; if (hours[d]) { label = (i === 1 ? 'завтра' : NAMES[d]) + ' в ' + short(hours[d][0]); break; } }
        statusEl.textContent = 'Откроемся ' + label;
        statusEl.classList.add('is-closed');
      }
    } catch (e) { /* остаётся статичный текст */ }
  }

  /* ---------- Год ---------- */
  var y = $('[data-year]');
  if (y) y.textContent = String(new Date().getFullYear());
})();
