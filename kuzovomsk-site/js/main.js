/* Центр кузовного ремонта — скрипты сайта (без зависимостей) */
(function () {
  'use strict';

  var PHONE_WA = '79088079293';
  var EMAIL = 'muhachev83@gmail.com,9774507@mail.ru'; // письмо уходит на оба адреса со старого сайта
  var TZ = 'Asia/Omsk';
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var rub = function (n) { return n.toLocaleString('ru-RU').replace(/ | /g, ' ') + ' ₽'; };

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

  /* ---------- Вкладки прайса ---------- */
  var tabs = $$('.tabs [role="tab"]');
  var selectTab = function (name, focus) {
    tabs.forEach(function (t) {
      var on = t.dataset.tab === name;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
      if (on && focus) t.focus();
    });
  };
  if (tabs.length) {
    selectTab('paint');
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { selectTab(t.dataset.tab); });
      t.addEventListener('keydown', function (e) {
        var d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
        if (!d) return;
        e.preventDefault();
        selectTab(tabs[(i + d + tabs.length) % tabs.length].dataset.tab, true);
      });
    });
    $$('a[href="#prices"][data-tab]').forEach(function (a) {
      a.addEventListener('click', function () { selectTab(a.dataset.tab); });
    });
  }

  /* ---------- Калькулятор покраски (цены из прайса, «от») ---------- */
  var PARTS = {
    'hood':         { name: 'Капот', price: 6000 },
    'roof':         { name: 'Крыша', price: 6000 },
    'front-bumper': { name: 'Передний бампер', price: 5000 },
    'rear-bumper':  { name: 'Задний бампер', price: 5000 },
    'trunk':        { name: 'Крышка багажника / 5-я дверь', price: 5000 },
    'fender-fl':    { name: 'Крыло переднее левое', price: 5000 },
    'fender-fr':    { name: 'Крыло переднее правое', price: 5000 },
    'fender-rl':    { name: 'Крыло заднее левое', price: 5000 },
    'fender-rr':    { name: 'Крыло заднее правое', price: 5000 },
    'door-fl':      { name: 'Дверь передняя левая', price: 5000 },
    'door-fr':      { name: 'Дверь передняя правая', price: 5000 },
    'door-rl':      { name: 'Дверь задняя левая', price: null },
    'door-rr':      { name: 'Дверь задняя правая', price: null },
    'mirror-l':     { name: 'Зеркало левое (с установкой)', price: 2000 },
    'mirror-r':     { name: 'Зеркало правое (с установкой)', price: 2000 },
    'panel-front':  { name: 'Передняя панель', price: 5000 },
    'panel-rear':   { name: 'Задняя панель', price: 5000 },
    'panel-tail':   { name: 'Панель задка', price: 5000 },
    'trunk-floor':  { name: 'Панель пола багажника', price: 4000 },
    'full':         { name: 'Полная окраска кузова', price: 70000 }
  };
  var calc = $('#calc');
  if (calc) {
    var carBox = $('.calc__car', calc);
    var shapes = $$('.part', calc);
    var chips = $$('.chip', calc);
    var sumEl = $('[data-sum]', calc);
    var extraEl = $('[data-sum-extra]', calc);
    var listEl = $('[data-list]', calc);
    var sendBtn = $('[data-calc-send]', calc);
    var resetBtn = $('[data-calc-reset]', calc);
    var selected = {};
    var shown = 0;

    shapes.forEach(function (s) {
      var p = PARTS[s.dataset.part];
      s.setAttribute('tabindex', '0');
      s.setAttribute('role', 'button');
      s.setAttribute('aria-label', p.name);
      s.setAttribute('aria-pressed', 'false');
      var t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      t.textContent = p.name + (p.price ? ' — от ' + p.price.toLocaleString('ru-RU') + ' ₽' : ' — цена после осмотра');
      s.appendChild(t);
    });

    var animateSum = function (to) {
      if (reduceMotion) { shown = to; sumEl.textContent = rub(to); return; }
      var from = shown, start = null, dur = 500;
      var step = function (ts) {
        if (!start) start = ts;
        var k = Math.min(1, (ts - start) / dur);
        k = 1 - Math.pow(1 - k, 3);
        sumEl.textContent = rub(Math.round((from + (to - from) * k) / 100) * 100);
        if (k < 1) requestAnimationFrame(step); else shown = to;
      };
      requestAnimationFrame(step);
    };

    var render = function () {
      var full = !!selected.full;
      var keys = Object.keys(selected);
      var total = 0, unknown = 0;
      listEl.innerHTML = '';
      keys.forEach(function (k) {
        var p = PARTS[k];
        var li = document.createElement('li');
        var name = document.createElement('span'); name.textContent = p.name;
        var price = document.createElement('b');
        if (p.price) { total += p.price; price.textContent = 'от ' + rub(p.price); }
        else { unknown++; price.textContent = 'после осмотра'; }
        li.appendChild(name); li.appendChild(price); listEl.appendChild(li);
      });
      if (!keys.length) {
        var e = document.createElement('li'); e.className = 'calc__empty';
        e.textContent = 'Выберите детали на схеме или в списке'; listEl.appendChild(e);
      }
      shapes.forEach(function (s) {
        var on = !!selected[s.dataset.part];
        s.classList.toggle('is-on', on);
        s.setAttribute('aria-pressed', String(on));
      });
      chips.forEach(function (c) {
        var k = c.dataset.part;
        c.setAttribute('aria-pressed', String(!!selected[k]));
        c.disabled = full && k !== 'full';
      });
      carBox.classList.toggle('is-full', full);
      var onlyUnknown = keys.length && !total;
      sumEl.classList.toggle('is-text', !!onlyUnknown);
      if (onlyUnknown) { shown = 0; sumEl.textContent = 'по осмотру'; }
      else { sumEl.textContent = ''; animateSum(total); }
      sumEl.setAttribute('data-prefix', keys.length && total ? 'от' : '');
      if (unknown && total) {
        extraEl.textContent = '+ ' + (unknown === 1 ? 'задняя дверь' : 'задние двери') + ' — цена после осмотра';
        extraEl.hidden = false;
      } else { extraEl.hidden = true; }
      sendBtn.disabled = resetBtn.disabled = !keys.length;
      calc._total = total; calc._unknown = unknown;
    };

    var toggle = function (k) {
      if (k === 'full') {
        if (selected.full) delete selected.full; else selected = { full: true };
      } else {
        if (selected.full) return;
        if (selected[k]) delete selected[k]; else selected[k] = true;
      }
      render();
    };

    var hover = function (k, on) {
      shapes.forEach(function (s) { if (s.dataset.part === k) s.classList.toggle('is-hover', on); });
      chips.forEach(function (c) { if (c.dataset.part === k) c.classList.toggle('is-hover', on); });
    };

    shapes.forEach(function (s) {
      s.addEventListener('click', function () { toggle(s.dataset.part); });
      s.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(s.dataset.part); } });
      s.addEventListener('mouseenter', function () { hover(s.dataset.part, true); });
      s.addEventListener('mouseleave', function () { hover(s.dataset.part, false); });
    });
    chips.forEach(function (c) {
      c.addEventListener('click', function () { toggle(c.dataset.part); });
      c.addEventListener('mouseenter', function () { hover(c.dataset.part, true); });
      c.addEventListener('mouseleave', function () { hover(c.dataset.part, false); });
    });
    resetBtn.addEventListener('click', function () { selected = {}; render(); });
    sendBtn.addEventListener('click', function () {
      var lines = ['Здравствуйте! Хочу рассчитать покраску:'];
      Object.keys(selected).forEach(function (k) {
        var p = PARTS[k];
        lines.push('— ' + p.name + (p.price ? ' (от ' + p.price.toLocaleString('ru-RU') + ' ₽)' : ' (цена после осмотра)'));
      });
      if (calc._total) lines.push('Итого по прайсу: от ' + calc._total.toLocaleString('ru-RU') + ' ₽' + (calc._unknown ? ' + детали, цена которых после осмотра' : ''));
      else lines.push('Стоимость — после осмотра');
      lines.push('Фото повреждений пришлю следующим сообщением.');
      window.open('https://wa.me/' + PHONE_WA + '?text=' + encodeURIComponent(lines.join('\n')), '_blank', 'noopener');
    });
    render();
  }

  /* ---------- Отзывы: слайдер ---------- */
  var rv = $('[data-rv]');
  if (rv) {
    var items = $$('.rv__item', rv);
    var cur = 0, timer = null;
    var curEl = $('[data-rv-cur]'), totalEl = $('[data-rv-total]');
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    totalEl.textContent = pad(items.length);
    var show = function (i) {
      cur = (i + items.length) % items.length;
      items.forEach(function (it, j) { it.classList.toggle('is-active', j === cur); it.setAttribute('aria-hidden', String(j !== cur)); });
      curEl.textContent = pad(cur + 1);
    };
    var auto = function () { clearInterval(timer); if (!reduceMotion) timer = setInterval(function () { show(cur + 1); }, 9000); };
    $('[data-rv-prev]').addEventListener('click', function () { show(cur - 1); auto(); });
    $('[data-rv-next]').addEventListener('click', function () { show(cur + 1); auto(); });
    rv.addEventListener('mouseenter', function () { clearInterval(timer); });
    rv.addEventListener('mouseleave', auto);
    var sx = null;
    rv.addEventListener('touchstart', function (e) { sx = e.touches[0].clientX; }, { passive: true });
    rv.addEventListener('touchend', function (e) {
      if (sx === null) return;
      var dx = e.changedTouches[0].clientX - sx;
      if (Math.abs(dx) > 50) { show(cur + (dx < 0 ? 1 : -1)); auto(); }
      sx = null;
    });
    show(0); auto();
  }

  /* ---------- Время в Омске ---------- */
  var omskNow = function () {
    var parts = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, weekday: 'short', hour: '2-digit', minute: '2-digit', month: '2-digit', day: '2-digit', hour12: false }).formatToParts(new Date());
    var get = function (t) { var p = parts.find(function (x) { return x.type === t; }); return p ? p.value : ''; };
    var days = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
    return { day: days[get('weekday')], minutes: (parseInt(get('hour'), 10) % 24) * 60 + parseInt(get('minute'), 10), md: get('month') + '-' + get('day') };
  };

  /* ---------- Открыто / закрыто ---------- */
  var HOURS = [null, [540, 1080], [540, 1080], [540, 1080], [540, 1080], [540, 1080], [600, 900]];
  var DAY_NAMES = ['в воскресенье', 'в понедельник', 'во вторник', 'в среду', 'в четверг', 'в пятницу', 'в субботу'];
  var fmt = function (m) { return Math.floor(m / 60) + ':' + ('0' + (m % 60)).slice(-2); };
  var statusEl = $('[data-open-status]');
  if (statusEl) {
    try {
      var now = omskNow(), today = HOURS[now.day];
      if (today && now.minutes >= today[0] && now.minutes < today[1]) {
        statusEl.textContent = 'Открыто до ' + fmt(today[1]); statusEl.classList.add('is-open');
      } else {
        var label = '';
        if (today && now.minutes < today[0]) label = 'сегодня в ' + fmt(today[0]);
        else for (var i = 1; i <= 7; i++) { var d = (now.day + i) % 7; if (HOURS[d]) { label = (i === 1 ? 'завтра' : DAY_NAMES[d]) + ' в ' + fmt(HOURS[d][0]); break; } }
        statusEl.textContent = 'Откроемся ' + label; statusEl.classList.add('is-closed');
      }
      statusEl.title = 'Время омское. В праздники режим уточняйте по телефону.';
    } catch (e) { /* статичный текст */ }
  }

  /* ---------- Сезонные скидки ---------- */
  var seasonal = $('[data-seasonal]'), nowEl = $('[data-discount-now]');
  if (seasonal && nowEl) {
    try {
      var md = omskNow().md;
      var MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
      var list = $$('li[data-from]', seasonal);
      var inRange = function (x, a, b) { return a <= b ? (x >= a && x <= b) : (x >= a || x <= b); };
      var active = list.find(function (li) { return inRange(md, li.dataset.from, li.dataset.to); });
      if (active) { active.classList.add('is-active'); nowEl.textContent = 'Сейчас действует скидка «' + active.dataset.name + '»'; }
      else {
        var sorted = list.slice().sort(function (a, b) { return a.dataset.from < b.dataset.from ? -1 : 1; });
        var next = sorted.find(function (li) { return li.dataset.from > md; }) || sorted[0];
        var p = next.dataset.from.split('-');
        nowEl.textContent = 'Ближайшая — «' + next.dataset.name + '» с ' + parseInt(p[1], 10) + ' ' + MONTHS[parseInt(p[0], 10) - 1];
      }
      nowEl.hidden = false;
    } catch (e) { /* без подсказки */ }
  }

  /* ---------- Форма оценки по фото ---------- */
  var form = $('#estimate-form');
  if (form) {
    var err = $('#form-error'), phone = form.elements.phone, channel = 'wa';
    $$('button[type="submit"]', form).forEach(function (b) { b.addEventListener('click', function () { channel = b.dataset.channel; }); });
    phone.addEventListener('input', function () { if (phone.value.replace(/\D/g, '').length >= 10) { phone.removeAttribute('aria-invalid'); err.hidden = true; } });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var f = form.elements;
      if (f.phone.value.replace(/\D/g, '').length < 10) { phone.setAttribute('aria-invalid', 'true'); err.hidden = false; phone.focus(); return; }
      var lines = ['Здравствуйте! Хочу оценить ремонт.'];
      if (f.name.value.trim()) lines.push('Имя: ' + f.name.value.trim());
      lines.push('Телефон: ' + f.phone.value.trim());
      if (f.car.value.trim()) lines.push('Автомобиль: ' + f.car.value.trim());
      if (f.message.value.trim()) lines.push('Повреждения: ' + f.message.value.trim());
      lines.push('Фото пришлю следующим сообщением.');
      var text = lines.join('\n');
      if (channel === 'mail') window.location.href = 'mailto:' + EMAIL + '?subject=' + encodeURIComponent('Оценка ремонта по фото') + '&body=' + encodeURIComponent(text);
      else window.open('https://wa.me/' + PHONE_WA + '?text=' + encodeURIComponent(text), '_blank', 'noopener');
    });
  }

  /* ---------- Год ---------- */
  var y = $('[data-year]');
  if (y) y.textContent = String(new Date().getFullYear());
})();
