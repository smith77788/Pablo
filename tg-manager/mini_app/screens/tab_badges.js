// ── Живые значки на вкладках (screens/tab_badges.js) ───────────────────────
// Нижние вкладки были статичны: ни «Итоги», ни «Аккаунты», ни «Сообщения» не
// говорили, что там что-то изменилось. Узнать, не ограничили ли аккаунт и не
// идёт ли рассылка, можно было только открыв экраны по очереди — а зайдя в
// приложение, владелец хочет знать ровно одно: есть ли где-то то, с чем надо
// что-то делать.
//
// Числа берутся ОДНОЙ ручкой на весь таббар (`/api/miniapp/badges`): значки
// обновляются вместе, поэтому три запроса означали бы три круга по сети на
// каждое обновление. Ноль значит «нечего показать» — значок прячется, а не
// показывает нуль: «0» на вкладке читается как сообщение, которого нет.
'use strict';

// Счётчик → вкладка, на которой он висит. Операции лежат в «Разделах».
const TAB_BADGE_OF = {
  notifications: 't-home',
  accounts: 't-accounts',
  broadcasts: 't-broadcasts',
  operations: 't-more',
};
const TAB_BADGE_EVERY = 60e3;   // как часто спрашиваем сами
let _tabBadgeAt = 0;

function setTabBadge(key, n) {
  const tab = document.getElementById(TAB_BADGE_OF[key] || '');
  if (!tab) return;
  let el = tab.querySelector('.tab-badge');
  if (!el) {
    el = document.createElement('span');
    el.className = 'tab-badge';
    tab.appendChild(el);
  }
  const num = Number(n) || 0;
  el.textContent = num > 99 ? '99+' : String(num);
  el.style.display = num ? '' : 'none';
  // Вкладка со значком обязана называть его вслух: иначе человек со
  // скринридером слышит «Аккаунты», а не «Аккаунты, 3 требуют внимания».
  tab.setAttribute('aria-label', (tab.querySelector('.tab-lbl')
    ? tab.querySelector('.tab-lbl').textContent : key)
    + (num ? ', требуют внимания: ' + num : ''));
}

async function refreshTabBadges(force) {
  if (!force && Date.now() - _tabBadgeAt < TAB_BADGE_EVERY) return;
  _tabBadgeAt = Date.now();
  try {
    const d = await api('/api/miniapp/badges');
    Object.keys(TAB_BADGE_OF).forEach(k => setTabBadge(k, d[k]));
    // Колокольчик на главном экране показывает то же число, что вкладка
    // «Итоги», и берёт его из этого же ответа: второй запрос ради одного
    // числа — это второй круг по сети на каждое обновление.
    if (typeof setNotifBadge === 'function') setNotifBadge(Number(d.notifications) || 0);
    // Плашка «идёт операция» берёт своё из этого же ответа и просит следующий
    // чаще, пока работа идёт (screens/op_live_bar.js).
    if (typeof renderOpLiveBar === 'function') {
      renderOpLiveBar(d.op);
      if (typeof opLiveBarTick === 'function') opLiveBarTick();
    }
  } catch (_) {
    // Значок — украшение: молчим и пробуем в следующий раз.
    _tabBadgeAt = 0;
  }
}

function tabBadgesInit() {
  if (tabBadgesInit.done) return;
  tabBadgesInit.done = true;
  refreshTabBadges(true);
  setInterval(() => refreshTabBadges(true), TAB_BADGE_EVERY);
  // Возврат в приложение — главный момент, когда числа устарели: телефон
  // держал мини-апп в фоне, пока операции шли.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshTabBadges(true);
  });
  window.addEventListener('online', () => refreshTabBadges(true));
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', tabBadgesInit);
} else {
  tabBadgesInit();
}
