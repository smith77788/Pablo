// ── Общая механика экранов (screens/ux_kit.js) ─────────────────────────────
// Три вещи, которых не было НИ НА ОДНОМ экране, хотя касаются они всех:
//
//  1. ВОЗВРАТ НА МЕСТО. `push()` ставил новому экрану scrollTop = 0, а `back()`
//     не восстанавливал ничего. Пролистал список аккаунтов до трёхсотого,
//     открыл карточку, вернулся — и ты снова наверху, листать заново. На
//     длинных списках (аккаунты, контакты, операции) это главная потеря
//     времени в приложении.
//  2. «НАВЕРХ». Вернуться к началу тысячестрочного списка можно было только
//     прокруткой пальцем. Кнопка появляется сама, когда прокрутили заметно.
//  3. «НЕТ СЕТИ». Обрыв связи выглядел как сломанное приложение: экраны
//     показывали ошибку загрузки, причина не называлась нигде. Теперь сверху
//     честная полоска, а возврат сети обновляет текущий экран сам.
//
// Всё держится на ОДНОМ месте: `push`/`back` зовут хуки отсюда, а слушатель
// прокрутки один на документ (события прокрутки не всплывают, но ловятся в
// фазе перехвата) — по слушателю на каждый из двухсот экранов обошлось бы
// дороже самой функции.
'use strict';

// Где владелец стоял на каждом экране: id экрана → scrollTop.
const UX_SCROLL = Object.create(null);
const UX_TOP_AT = 600;      // с какой прокрутки показываем «наверх»

function uxRememberScroll(sid) {
  const el = sid && document.getElementById(sid);
  if (el && el.scrollTop > 0) UX_SCROLL[sid] = el.scrollTop;
}

// Вернуть прокрутку экрана. Содержимое экрана часто дорисовывается ПОСЛЕ
// показа (ответ сервера), поэтому пробуем ещё раз на следующем кадре: иначе
// восстанавливать было бы некуда — высота ещё нулевая.
function uxRestoreScroll(sid) {
  const want = UX_SCROLL[sid];
  const el = sid && document.getElementById(sid);
  if (!el) return;
  if (!want) { el.scrollTop = 0; return; }
  el.scrollTop = want;
  requestAnimationFrame(() => {
    if (document.getElementById(sid) === el && el.classList.contains('show')
        && Math.abs(el.scrollTop - want) > 4) el.scrollTop = want;
  });
}

function uxForgetScroll(sid) { delete UX_SCROLL[sid]; }

function uxActiveScreen() {
  const list = document.querySelectorAll('.screen.show');
  return list.length ? list[list.length - 1] : null;
}

function uxScrollTop() {
  const el = uxActiveScreen();
  if (!el) return;
  try { el.scrollTo({ top: 0, behavior: 'smooth' }); } catch (_) { el.scrollTop = 0; }
  try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch (_) {}
}

function uxSyncTopButton(el) {
  const btn = document.getElementById('uxTopBtn');
  if (!btn) return;
  const active = el || uxActiveScreen();
  const show = !!active && active.scrollTop > UX_TOP_AT;
  btn.style.display = show ? '' : 'none';
}

// ── «Нет сети» ─────────────────────────────────────────────────────────────
let UX_OFFLINE = false;

function uxOffline(on) {
  on = !!on;
  if (on === UX_OFFLINE) return;
  UX_OFFLINE = on;
  const bar = document.getElementById('uxNetBar');
  if (bar) bar.style.display = on ? '' : 'none';
  // Связь вернулась — обновляем то, на что человек смотрит. Без этого экран
  // оставался с ошибкой загрузки до ручного нажатия, хотя сеть уже есть.
  if (!on && typeof uxReloadCurrent === 'function') uxReloadCurrent();
}

// Обновить текущий экран его же средствами. Общего «перезагрузить экран» в
// приложении нет, поэтому зовём то, что точно есть у вкладок, и не трогаем
// вложенные экраны: их обновит человек, а угадывать за него хуже, чем молчать.
function uxReloadCurrent() {
  try {
    if (typeof STACK !== 'undefined' && STACK.length) return;
    if (typeof CUR === 'undefined') return;
    // Функции зовём ПО ИМЕНИ, а не через window[...]: имя в коде видит и
    // сторож мёртвых функций, и поиск по репозиторию, а строка в словаре —
    // нет (CLAUDE.md: «мёртвый код во фронте нельзя искать только по фронту»).
    if (CUR === 'home' && typeof loadHome === 'function') loadHome();
    else if (CUR === 'accounts' && typeof loadAccounts === 'function') loadAccounts();
    else if (CUR === 'bots' && typeof loadBots === 'function') loadBots();
    else if (CUR === 'broadcasts' && typeof loadBroadcasts === 'function') loadBroadcasts();
  } catch (_) { /* обновление — удобство, а не обязанность */ }
}

function uxInit() {
  if (uxInit.done) return;
  uxInit.done = true;
  // Полоска сети и кнопка «наверх» создаются здесь, а не в разметке: это
  // общая механика, и двум сотням экранов она нужна одинаково.
  const bar = document.createElement('div');
  bar.id = 'uxNetBar';
  bar.className = 'ux-netbar';
  bar.setAttribute('role', 'status');
  bar.style.display = 'none';
  bar.textContent = '📵 Нет сети. Ждём соединения…';
  document.body.appendChild(bar);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'uxTopBtn';
  btn.className = 'ux-top';
  btn.setAttribute('aria-label', 'Наверх');
  btn.textContent = '↑';
  btn.style.display = 'none';
  btn.addEventListener('click', uxScrollTop);
  document.body.appendChild(btn);

  document.addEventListener('scroll', ev => {
    const t = ev.target;
    if (t && t.classList && t.classList.contains('screen')) uxSyncTopButton(t);
  }, true);

  window.addEventListener('offline', () => uxOffline(true));
  window.addEventListener('online', () => uxOffline(false));
  if (navigator && navigator.onLine === false) uxOffline(true);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', uxInit);
} else {
  uxInit();
}

// ── «Закреплённые» и «Недавние» в каталоге ─────────────────────────────────
// Каталог — 80+ разделов в 11 категориях, шесть экранов прокрутки. Человек
// изо дня в день пользуется пятью-шестью из них, и каждый раз искал их заново:
// ни закрепить своё, ни увидеть, где был вчера, было нельзя (поиск по каталогу
// помогает, только если помнишь название).
//
// Храним в localStorage, а не на сервере: это привычка устройства, и ради неё
// не стоит ни ручки, ни таблицы. Пункт описывается тремя полями — подпись,
// значок и ТОТ ЖЕ обработчик, что у плитки каталога: так закреплённое ведёт
// ровно туда же, куда плитка, и не разъедется с ней при переименовании экрана.
const UX_NAV_KEY = 'infragram_nav_memory';
const UX_RECENT_MAX = 6;
const UX_PINNED_MAX = 12;

function uxNavMemory() {
  try {
    const raw = JSON.parse(localStorage.getItem(UX_NAV_KEY) || '{}');
    return { pinned: Array.isArray(raw.pinned) ? raw.pinned : [],
             recent: Array.isArray(raw.recent) ? raw.recent : [] };
  } catch (_) { return { pinned: [], recent: [] }; }
}

function uxNavSave(mem) {
  try { localStorage.setItem(UX_NAV_KEY, JSON.stringify(mem)); } catch (_) {}
  // Каталог собирается один раз и кэшируется — после правки памяти его надо
  // собрать заново, иначе изменения видно только после перезагрузки.
  const body = document.getElementById('navBody');
  if (body) body.dataset.built = '';
}

function uxNavRemember(lbl, ico, handler) {
  if (!lbl || !handler) return;
  const mem = uxNavMemory();
  mem.recent = [{ lbl, ico: ico || '•', h: handler }]
    .concat(mem.recent.filter(x => x && x.lbl !== lbl))
    .slice(0, UX_RECENT_MAX);
  uxNavSave(mem);
}

function uxNavPinned(lbl) {
  return uxNavMemory().pinned.some(x => x && x.lbl === lbl);
}

// Закрепить/открепить. Закрытый потолок намеренно: «закреплено всё» — это
// снова каталог, только сверху.
function uxNavPin(lbl, ico, handler) {
  const mem = uxNavMemory();
  if (mem.pinned.some(x => x && x.lbl === lbl)) {
    mem.pinned = mem.pinned.filter(x => x && x.lbl !== lbl);
    if (typeof toast === 'function') toast('☆ Откреплено: ' + lbl);
  } else if (mem.pinned.length >= UX_PINNED_MAX) {
    if (typeof toast === 'function') toast('⚠️ Закреплено уже ' + UX_PINNED_MAX + ' — открепите лишнее');
    return;
  } else {
    mem.pinned = mem.pinned.concat([{ lbl, ico: ico || '•', h: handler }]);
    if (typeof toast === 'function') toast('⭐ Закреплено: ' + lbl);
  }
  uxNavSave(mem);
  if (typeof buildDrawer === 'function') buildDrawer();
  if (typeof markDrawerActive === 'function') markDrawerActive();
}

// Разметка блоков «Закреплённые» и «Недавние» для каталога. Отдаём строкой —
// её вставляет buildDrawer, одно место сборки меню.
function uxNavBlocks() {
  const mem = uxNavMemory();
  const row = (it, star) => {
    const h = String(it.h || '').replace(/"/g, '&quot;');
    return `<div class="nav-item" onclick="closeDrawer(); ${h}">` +
      `<span class="ni-ico">${uxNavEsc(it.ico)}</span>` +
      `<span class="ni-body"><span>${uxNavEsc(it.lbl)}</span></span>` +
      `<span class="ni-pin" role="button" tabindex="0" aria-label="${star ? 'Открепить' : 'Закрепить'}" ` +
      `onclick="event.stopPropagation(); uxNavPin('${uxNavQ(it.lbl)}','${uxNavQ(it.ico)}','${uxNavQ(it.h)}')">` +
      `${star ? '★' : '☆'}</span></div>`;
  };
  let html = '';
  if (mem.pinned.length) {
    html += '<div class="nav-cat">⭐ Закреплённые</div>' + mem.pinned.map(it => row(it, true)).join('');
  }
  const recent = mem.recent.filter(it => !mem.pinned.some(p => p.lbl === it.lbl));
  if (recent.length) {
    html += '<div class="nav-cat">🕘 Недавние</div>' + recent.map(it => row(it, false)).join('');
  }
  return html;
}

function uxNavEsc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
// Значение внутри одинарных кавычек атрибута onclick="...". Порядок важен:
// сначала HTML-сущности (иначе собственный «&» испортил бы уже готовую
// сущность), потом экранирование для строки JS. Двойную кавычку не
// превращаем в &quot;, а выбрасываем: браузер развернул бы сущность ДО того,
// как JS увидит строку, и атрибут порвался бы ровно так, как однажды уже
// рвался список сообществ (CLAUDE.md).
function uxNavQ(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '')
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}
