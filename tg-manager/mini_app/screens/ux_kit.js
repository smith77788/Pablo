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
