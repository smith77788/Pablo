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

  // Полоска жеста «потянуть вниз — обновить».
  const pull = document.createElement('div');
  pull.id = 'uxPullBar';
  pull.className = 'ux-pullbar';
  pull.setAttribute('role', 'status');
  pull.style.display = 'none';
  document.body.appendChild(pull);
  document.addEventListener('touchstart', uxPullStart, { passive: true });
  document.addEventListener('touchmove', uxPullMove, { passive: true });
  document.addEventListener('touchend', uxPullEnd, { passive: true });
  document.addEventListener('touchcancel', uxPullEnd, { passive: true });
  document.addEventListener('touchstart', uxSwipeStart, { passive: true });
  document.addEventListener('touchend', uxSwipeEnd, { passive: true });
  document.addEventListener('touchstart', uxHoldStart, { passive: true });
  document.addEventListener('touchmove', uxHoldMove, { passive: true });
  document.addEventListener('touchend', uxHoldCancel, { passive: true });
  document.addEventListener('touchcancel', uxHoldCancel, { passive: true });

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
  // Главный экран показывает то же закреплённое: звезда, нажатая в каталоге,
  // обязана отразиться там сразу, а не после перезагрузки приложения.
  uxPinnedHome();
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

// ── «Потянуть вниз — обновить» ─────────────────────────────────────────────
// Обновить список можно было только одним способом: уйти с экрана и зайти
// снова (а на вкладках — нажать вкладку второй раз, о чём нигде не написано).
// На телефоне обновление списка — жест, и его ждут: владелец смотрит на
// очередь операций и тянет вниз, потому что так работает всё остальное у него
// в телефоне. До этого он получал нативный «отскок» прокрутки и ничего больше.
//
// Экран объявляет свою загрузку САМ — атрибутом data-reload="loadOps" в
// разметке. Так имя функции видно и в index.html, и поиску по репозиторию, и
// сторожу мёртвых функций (CLAUDE.md: имя в коде видят все, строка в словаре —
// никто), а общая механика не хранит у себя карту двух сотен экранов.
const UX_PULL_AT = 70;        // сколько тянуть, чтобы жест сработал
let UX_PULL = null;           // {el, y0, phase} — пока палец на экране

// Фаза жеста по тому, насколько протянули. Отдельной функцией: на ней стоит
// тест, а в обработчике её не проверить — там нет ни пальца, ни экрана.
function uxPullPhase(dy) {
  if (!(dy > 12)) return '';
  return dy >= UX_PULL_AT ? 'armed' : 'pull';
}

// Чем экран обновляется. Пусто — значит экран не объявил загрузку, и жест для
// него выключен: лучше нативный отскок, чем «обновляем…» без обновления.
function uxReloadFn(el) {
  const name = el && el.dataset ? String(el.dataset.reload || '') : '';
  if (!name) return null;
  const fn = typeof window !== 'undefined' ? window[name] : undefined;
  return typeof fn === 'function' ? fn : null;
}

function uxPullBar(text) {
  const bar = document.getElementById('uxPullBar');
  if (!bar) return;
  bar.textContent = text || '';
  bar.style.display = text ? '' : 'none';
}

function uxPullStart(ev) {
  UX_PULL = null;
  const t = ev.touches;
  if (!t || t.length !== 1) return;
  const el = uxActiveScreen();
  // Тянуть можно только с самого верха: иначе жест отбирал бы прокрутку.
  if (!el || el.scrollTop > 0 || !uxReloadFn(el)) return;
  UX_PULL = { el, y0: t[0].clientY, phase: '' };
}

function uxPullMove(ev) {
  if (!UX_PULL) return;
  const t = ev.touches;
  if (!t || t.length !== 1) { UX_PULL = null; uxPullBar(''); return; }
  // Пока палец идёт вверх (или экран успел прокрутиться) — это обычная
  // прокрутка, а не жест обновления.
  if (UX_PULL.el.scrollTop > 0) { UX_PULL = null; uxPullBar(''); return; }
  const phase = uxPullPhase(t[0].clientY - UX_PULL.y0);
  if (phase === UX_PULL.phase) return;
  UX_PULL.phase = phase;
  if (phase === 'armed') {
    uxPullBar('↻ Отпустите — обновим');
    try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch (_) {}
  } else if (phase === 'pull') {
    uxPullBar('↓ Потяните, чтобы обновить');
  } else {
    uxPullBar('');
  }
}

function uxPullEnd() {
  const st = UX_PULL;
  UX_PULL = null;
  if (!st || st.phase !== 'armed') { uxPullBar(''); return; }
  const fn = uxReloadFn(st.el);
  if (!fn) { uxPullBar(''); return; }
  uxPullBar('⏳ Обновляем…');
  try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred('medium'); } catch (_) {}
  // Полоску убираем по таймеру, а не по ответу: загрузки экранов ничего не
  // возвращают (и не обязаны), а висящее «обновляем…» — это ложь о состоянии.
  setTimeout(() => uxPullBar(''), 900);
  try { fn(); } catch (_) { uxPullBar(''); }
}

// ── Свайп от левого края — назад ───────────────────────────────────────────
// Кнопка «←» живёт в левом верхнем углу, и на телефоне с большим экраном до
// неё надо тянуться второй рукой: в приложении, где экраны вложены на три-
// четыре уровня (список → карточка → раздел карточки), это самый частый жест
// за день. Системный свайп Telegram закрывает ВСЁ приложение, а не шаг назад,
// поэтому его не хватало ровно там, где он привычен.
//
// Жест читается только от самого края (иначе он отбирал бы горизонтальную
// прокрутку у полос чипов) и только когда есть куда возвращаться.
const UX_EDGE = 24;           // откуда начинается «от края»
const UX_BACK_AT = 64;        // сколько протянуть вбок
const UX_BACK_SLOPE = 48;     // насколько вертикальным жест быть не должен
let UX_SWIPE = null;

// Вердикт по жесту отдельной функцией: в обработчике его не проверить — там
// нет ни пальца, ни экрана.
function uxSwipeVerdict(x0, dx, dy) {
  if (x0 > UX_EDGE) return '';            // не от края — это прокрутка
  if (dx < UX_BACK_AT) return '';
  if (Math.abs(dy) > UX_BACK_SLOPE) return '';   // слишком вертикально
  return 'back';
}

function uxSwipeStart(ev) {
  UX_SWIPE = null;
  const t = ev.touches;
  if (!t || t.length !== 1) return;
  if (t[0].clientX > UX_EDGE) return;
  UX_SWIPE = { x0: t[0].clientX, y0: t[0].clientY };
}

function uxSwipeEnd(ev) {
  const st = UX_SWIPE;
  UX_SWIPE = null;
  if (!st) return;
  const t = ev.changedTouches;
  if (!t || t.length !== 1) return;
  if (uxSwipeVerdict(st.x0, t[0].clientX - st.x0, t[0].clientY - st.y0) !== 'back') return;
  // Возвращаться некуда — молчим: «назад» с корневого экрана закрыл бы
  // приложение, а этого человек жестом не просил.
  if (typeof STACK === 'undefined' || !STACK.length) return;
  if (typeof back !== 'function') return;
  try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch (_) {}
  back();
}

// ── «Показаны последние данные» ────────────────────────────────────────────
// Список открывается последними данными, а не спиннером: в метро и в лифте
// ответ сервера идёт секундами, и всё это время экран был пуст — приложение
// выглядело сломанным там, где оно просто ждало сеть. Но показанное из кэша
// обязано НАЗЫВАТЬ СЕБЯ: молча показать вчерашний список хуже, чем пустой
// экран, потому что по нему принимают решения.
function uxStale(id, on) {
  const el = document.getElementById(id);
  if (!el || !el.parentNode) return;
  const noteId = id + '-stale';
  let note = document.getElementById(noteId);
  if (!on) { if (note && note.parentNode) note.parentNode.removeChild(note); return; }
  if (note) return;
  note = document.createElement('div');
  note.id = noteId;
  note.className = 'hint ux-stale';
  note.setAttribute('role', 'status');
  note.textContent = '⏳ Показаны последние данные, обновляем…';
  el.parentNode.insertBefore(note, el);
}

// ── Долгий тап — скопировать ───────────────────────────────────────────────
// Номер аккаунта, его Telegram ID, ник бота — это то, что переносят руками в
// другой чат, в таблицу, в поддержку. Скопировать их было нельзя НИГДЕ: в
// мини-аппе выделение текста отключено (иначе любой скролл превращается в
// выделение), а кнопки «копировать» стояли только у ссылок. Владелец
// переписывал номер с экрана глазами.
//
// Значение объявляет сама строка — data-copy="+79990000001". Долгий тап по
// ней копирует и говорит, что скопировано. Копирует ЕДИНСТВЕННАЯ дверь
// (copyToClipboard): она честно отвечает об отказе и умеет запасной путь для
// незащищённого контекста, где navigator.clipboard нет.
const UX_HOLD_MS = 550;
let UX_HOLD = null;

// Ближайший предок со значением: палец попадает в подпись или в значок внутри
// строки, а значение объявлено на строке.
function uxCopyTarget(el) {
  for (let n = el; n; n = n.parentNode) {
    if (n.dataset && n.dataset.copy) return n;
    if (n.tagName === 'BODY') return null;
  }
  return null;
}

function uxHoldStart(ev) {
  uxHoldCancel();
  const t = ev.touches;
  if (!t || t.length !== 1) return;
  const node = uxCopyTarget(ev.target);
  if (!node) return;
  const y0 = t[0].clientY;
  const timer = setTimeout(() => {
    UX_HOLD = null;
    const what = node.dataset.copyWhat || 'Скопировано';
    try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred('medium'); } catch (_) {}
    if (typeof copyToClipboard === 'function') {
      copyToClipboard(node.dataset.copy, '📋 ' + what);
    }
  }, UX_HOLD_MS);
  UX_HOLD = { timer, y0 };
}

// Палец поехал — это прокрутка, а не удержание: копировать на ней значило бы
// копировать при каждом листании списка.
function uxHoldMove(ev) {
  if (!UX_HOLD) return;
  const t = ev.touches;
  if (!t || t.length !== 1 || Math.abs(t[0].clientY - UX_HOLD.y0) > 10) uxHoldCancel();
}

function uxHoldCancel() {
  if (UX_HOLD) clearTimeout(UX_HOLD.timer);
  UX_HOLD = null;
}

// Закреплённые разделы на главном экране. Каталог человек открывает не чтобы
// читать каталог, а чтобы попасть в свои пять-шесть разделов; значит им место
// на первом экране, а не за двумя нажатиями. Пусто — блока нет: пустая полка
// занимает место и ничего не говорит.
function uxPinnedHome() {
  const box = document.getElementById('homePinned');
  if (!box) return;
  const pinned = uxNavMemory().pinned;
  if (!pinned.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="sec">⭐ Закреплённое</div><div class="qa-grid">' +
    pinned.slice(0, 8).map(it =>
      `<div class="qa-tile" role="button" tabindex="0" onclick="${String(it.h || '').replace(/"/g, '&quot;')}">` +
      `<div class="qa-tile-ico">${uxNavEsc(it.ico)}</div>` +
      `<div class="qa-tile-lbl">${uxNavEsc(it.lbl)}</div></div>`).join('') +
    '</div>';
}

// ── «Продолжить с того места» ──────────────────────────────────────────────
// Мини-апп закрывают десятки раз в день — на звонок, на другой чат, по
// случайному смахиванию, — и каждый раз он открывается на «Итогах». Человек,
// разбиравший контакты или кампанию, возвращался к началу пути: вкладка →
// каталог → раздел. Помним ПОСЛЕДНИЙ открытый вложенный экран и предлагаем
// вернуться одним нажатием; предложение живёт сутки — через день это уже не
// «то место», а чужая строка на главном.
const UX_LAST_KEY = 'infragram_last_screen';
const UX_LAST_TTL = 24 * 3600e3;

function uxRememberScreen(title, handler) {
  if (!title || !handler) return;
  try {
    localStorage.setItem(UX_LAST_KEY, JSON.stringify(
      { title: String(title).slice(0, 60), h: handler, ts: Date.now() }));
  } catch (_) {}
}

function uxLastScreen() {
  try {
    const d = JSON.parse(localStorage.getItem(UX_LAST_KEY) || 'null');
    if (!d || !d.h || !d.ts || Date.now() - d.ts > UX_LAST_TTL) return null;
    return d;
  } catch (_) { return null; }
}

// Полоска на главном. Пусто — ничего не рисуем: «продолжить» без того, что
// продолжать, — это строка, которая просит нажать на неё впустую.
function uxResumeHome() {
  const box = document.getElementById('homeResume');
  if (!box) return;
  const d = uxLastScreen();
  box.innerHTML = (d && d.title)
    ? `<div class="ux-resume" role="button" tabindex="0" onclick="${String(d.h).replace(/"/g, '&quot;')}">` +
      `↩️ Продолжить: <b>${uxNavEsc(d.title)}</b></div>`
    : '';
}

// ── Сброс памяти приложения ────────────────────────────────────────────────
// Приложение помнит на устройстве немало: закреплённые и недавние разделы,
// последние запросы поиска, «то место», куда предлагает вернуться, кэш
// списков и выбранную тему. Когда что-то из этого показывает старое (телефон
// передали другому человеку, сменился проект), убрать это было нельзя ничем,
// кроме переустановки Telegram. Теперь есть одна кнопка — и она НАЗЫВАЕТ, что
// именно забудет: «очистить всё» без списка последствий человек нажимать
// боится, и правильно делает.
const UX_LOCAL_KEYS = [
  'infragram_nav_memory', 'infragram_find_history', 'infragram_last_screen',
  'infragram_theme',
];

function uxForgetLocal() {
  let n = 0;
  // Кэши списков и экранов зовутся одинаково (infragram_cache_*), поэтому
  // перебираем хранилище, а не держим второй список их имён: он устарел бы на
  // первом же новом экране.
  try {
    const keys = [];
    for (let i = 0; i < localStorage.length; i++) keys.push(localStorage.key(i));
    keys.forEach(k => {
      if (!k) return;
      if (UX_LOCAL_KEYS.indexOf(k) >= 0 || k.indexOf('infragram_cache_') === 0) {
        localStorage.removeItem(k);
        n++;
      }
    });
  } catch (_) { return 0; }
  return n;
}
