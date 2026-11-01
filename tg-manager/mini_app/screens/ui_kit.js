// ── Дизайн-система: компоненты (screens/ui_kit.js) ─────────────────────────
// Начало компонентного слоя мини-аппа. Каждый компонент — чистая функция
// «данные → HTML-строка» с классом .ui-<имя> из ui_kit.css. Экран собирает
// разметку из этих функций и не пишет style="…" руками: так все новые экраны
// выглядят одинаково, а правка вида делается в одном месте.
//
// Правила (docs/DESIGN_SYSTEM.md):
//   • текст из данных ВСЕГДА проходит через UI.esc — имена приходят из
//     Telegram и от пользователя;
//   • обработчик передаётся именем глобальной функции и аргументами-числами
//     или ключами, а не строкой кода из данных;
//   • новый компонент сначала сюда, потом на экран.
//
// Файл — классический скрипт, как остальные screens/*.js: грузится после
// основного блока index.html и делит с ним глобальную область.
'use strict';

const UI = (() => {
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  // Атрибут onclick из имени функции и простых аргументов (числа и ключи
  // [a-z0-9_-]). Всё остальное отбрасывается: код из данных не исполняем.
  function call(fn, ...args) {
    if (!/^[A-Za-z_$][\w$]*$/.test(String(fn || ''))) return '';
    const safe = args.map(a => (typeof a === 'number' && isFinite(a)) ? String(a)
      : (/^[a-z0-9_-]{1,40}$/i.test(String(a)) ? "'" + a + "'" : 'null'));
    return esc(fn + '(' + safe.join(',') + ')');
  }
  function num(n) {
    if (n === null || n === undefined || n === '') return '—';
    const v = Number(n);
    if (!isFinite(v)) return '—';
    return Math.round(v).toLocaleString('ru-RU');
  }
  function block(title, bodyHtml, link) {
    const l = link ? `<span class="ui-link" onclick="${call(link.fn, ...(link.args || []))}">${esc(link.label)}</span>` : '';
    return `<div class="ui-block"><div class="ui-block-hdr"><div class="ui-block-title">${esc(title)}</div>${l}</div>${bodyHtml}</div>`;
  }
  // value === null → «нет данных»: ноль и отсутствие данных — разные ответы.
  function stat({ ico, label, value, delta, sub, fn, args }) {
    const empty = value === null || value === undefined;
    let d = '';
    if (!empty && delta !== null && delta !== undefined && isFinite(delta)) {
      d = `<span class="${delta >= 0 ? 'ui-up' : 'ui-down'}">${delta >= 0 ? '▲' : '▼'} ${Math.abs(delta)}%</span> к прошлому периоду`;
    }
    const oc = fn ? ` onclick="${call(fn, ...(args || []))}" role="button" tabindex="0"` : '';
    return `<div class="ui-stat"${oc}>` +
      `<div class="ui-stat-lbl">${ico ? `<span>${esc(ico)}</span>` : ''}${esc(label)}</div>` +
      `<div class="ui-stat-val${empty ? ' is-empty' : ''}">${empty ? 'нет данных' : esc(value)}</div>` +
      `<div class="ui-stat-sub">${d || esc(sub || '')}</div></div>`;
  }
  function stats(items) { return `<div class="ui-stats">${items.map(stat).join('')}</div>`; }
  // Карточка «требует внимания». Кнопку рисует экран (у него свой обработчик),
  // сюда приходит готовая разметка кнопки или ничего.
  function alert({ severity, title, detail, buttonHtml }) {
    const sev = ['high', 'medium'].includes(severity) ? ' is-' + severity : '';
    return `<div class="ui-alert${sev}"><div class="ui-alert-body">` +
      `<div class="ui-alert-title">${esc(title)}</div>` +
      (detail ? `<div class="ui-alert-detail">${esc(detail)}</div>` : '') +
      (buttonHtml || '') + `</div></div>`;
  }
  function button(label, fn, ...args) {
    return `<span class="ui-btn" role="button" tabindex="0" onclick="${call(fn, ...args)}">${esc(label)}</span>`;
  }
  function sectionTile({ key, ico, title, desc }) {
    return `<div class="ui-section" role="button" tabindex="0" onclick="${call('openSection', key)}">` +
      `<div class="ui-section-ico">${esc(ico)}</div><div class="ui-section-title">${esc(title)}</div>` +
      (desc ? `<div class="ui-section-desc">${esc(desc)}</div>` : '') + `</div>`;
  }
  function sections(items) { return `<div class="ui-sections">${items.map(sectionTile).join('')}</div>`; }
  function ok(text) { return `<div class="ui-ok"><span>✅</span><span>${esc(text)}</span></div>`; }
  function empty(text) { return `<div class="ui-empty">${esc(text)}</div>`; }
  function skeleton(n) { return `<div class="ui-stats">${'<div class="ui-skel"></div>'.repeat(n || 4)}</div>`; }
  return { esc, call, num, block, stat, stats, alert, button, sectionTile, sections, ok, empty, skeleton };
})();
