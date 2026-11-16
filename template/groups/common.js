/* Общий скрипт шаблонов групп: «Открыто до …» по часам компании в её часовом поясе и текущий год. */
(function () {
  'use strict';
  var el = document.querySelectorAll('[data-open-status]');
  var hours = {};
  try { hours = JSON.parse(document.body.getAttribute('data-hours') || '{}'); } catch (e) { hours = {}; }
  if (el.length && Object.keys(hours).length) {
    try {
      var parts = new Intl.DateTimeFormat('en-GB', { timeZone: document.body.getAttribute('data-tz') || 'Europe/Moscow',
        weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date());
      var get = function (t) { var p = parts.find(function (x) { return x.type === t; }); return p ? p.value : ''; };
      var day = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }[get('weekday')];
      var now = get('hour') + ':' + get('minute');
      var short = function (t) { return t.replace(/^0(?=\d:)/, ''); };
      var NAMES = ['', 'в понедельник', 'во вторник', 'в среду', 'в четверг', 'в пятницу', 'в субботу', 'в воскресенье'];
      var today = hours[day], text, cls;
      if (today && now >= today[0] && now < today[1]) {
        text = (today[0] === '00:00' && today[1] === '24:00') ? 'Открыто круглосуточно' : 'Открыто до ' + short(today[1]);
        cls = 'is-open';
      } else {
        var label = '';
        if (today && now < today[0]) label = 'сегодня в ' + short(today[0]);
        else for (var i = 1; i <= 7; i++) { var d = (day - 1 + i) % 7 + 1; if (hours[d]) { label = (i === 1 ? 'завтра' : NAMES[d]) + ' в ' + short(hours[d][0]); break; } }
        text = 'Откроемся ' + label; cls = 'is-closed';
      }
      el.forEach(function (x) { x.textContent = text; x.classList.add(cls); });
    } catch (e) { /* остаётся статичный текст */ }
  }
  var y = document.querySelectorAll('[data-year]');
  y.forEach(function (x) { x.textContent = String(new Date().getFullYear()); });
})();
