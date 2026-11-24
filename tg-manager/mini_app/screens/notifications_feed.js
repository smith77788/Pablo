// ── «Уведомления»: история событий внутри приложения ───────────────────────
// Уведомления продукта жили ТОЛЬКО сообщениями в личке бота. Владелец, который
// был не в сети или промотал чат, не мог посмотреть, что произошло: ни истории,
// ни счётчика непрочитанного в приложении не было. Здесь лента всего, что
// прошло настройки уведомлений (одна дверь записи — db.notify_if_enabled), с
// отбором по типу события, отметкой прочитанного и значком на главном экране.
//
// Сервер: /api/miniapp/notifications (services/mini_app_notifications.py).
// Текст приходит уже БЕЗ HTML-разметки Telegram: снимает её сервер.
'use strict';

let NF_ROWS = [], NF_OFFSET = 0, NF_HAS_MORE = false, NF_KINDS = [],
    NF_PREF = '', NF_UNREAD_ONLY = false, NF_LOADING = false;
const NF_PAGE = 30;

function _nfQuery(offset) {
  const p = new URLSearchParams();
  p.set('limit', NF_PAGE);
  p.set('offset', offset);
  if (NF_PREF) p.set('pref', NF_PREF);
  if (NF_UNREAD_ONLY) p.set('unread', '1');
  return '/api/miniapp/notifications?' + p.toString();
}

async function openNotificationsFeed() {
  push('s-notifications');
  NF_OFFSET = 0;
  txt('nfList', '<div class="spin-wrap"><div class="spin"></div></div>');
  await loadNotificationsFeed();
}

async function loadNotificationsFeed() {
  if (NF_LOADING) return;
  NF_LOADING = true;
  try {
    const d = await api(_nfQuery(0));
    NF_ROWS = d.items || [];
    NF_KINDS = d.kinds || [];
    NF_OFFSET = NF_ROWS.length;
    NF_HAS_MORE = !!(d.page && d.page.has_more);
    _nfRender(d.unread || 0);
    setNotifBadge(d.unread || 0);
  } catch (e) {
    txt('nfList', errHtml(errRu(e), 'loadNotificationsFeed()'));
  } finally { NF_LOADING = false; }
}

async function nfMore() {
  if (NF_LOADING || !NF_HAS_MORE) return;
  NF_LOADING = true;
  try {
    const d = await api(_nfQuery(NF_OFFSET));
    const more = d.items || [];
    NF_ROWS = NF_ROWS.concat(more);
    NF_OFFSET += more.length;
    NF_HAS_MORE = !!(d.page && d.page.has_more);
    _nfRender(d.unread || 0);
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось подгрузить'));
  } finally { NF_LOADING = false; }
}

function nfSetKind(pref) {
  NF_PREF = (NF_PREF === pref) ? '' : pref;
  loadNotificationsFeed();
}
function nfToggleUnread() {
  NF_UNREAD_ONLY = !NF_UNREAD_ONLY;
  loadNotificationsFeed();
}

// Отметить прочитанным: всё или одну строку. Экран перерисовывается по ответу
// сервера, а не «оптимистично»: счётчик непрочитанного живёт в базе, и
// нарисованный у себя ноль при неудачной записи был бы неправдой.
async function nfReadAll() {
  try {
    const r = await api('/api/miniapp/notifications/read',
      { method: 'POST', body: JSON.stringify({ all: true }) });
    setNotifBadge(r.unread || 0);
    toast('✅ Всё прочитано');
    loadNotificationsFeed();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не получилось')); }
}

async function nfRead(id) {
  const row = NF_ROWS.find(x => x.id === id);
  if (!row || !row.unread) return;
  try {
    const r = await api('/api/miniapp/notifications/read',
      { method: 'POST', body: JSON.stringify({ ids: [id] }) });
    row.unread = false;
    setNotifBadge(r.unread || 0);
    _nfRender(r.unread || 0);
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не получилось')); }
}

function _nfRender(unread) {
  const chips = NF_KINDS.map(k =>
    `<span class="chip${NF_PREF === k.key ? ' on' : ''}" role="button" tabindex="0" ` +
    `onclick="nfSetKind('${esc(k.key)}')">${esc(k.icon)} ${esc(k.label)}</span>`).join('');
  txt('nfChips',
    `<span class="chip${NF_UNREAD_ONLY ? ' on' : ''}" role="button" tabindex="0" ` +
    `onclick="nfToggleUnread()">🔵 Непрочитанные${unread ? ' · ' + num(unread) : ''}</span>` + chips);
  const act = document.getElementById('nfReadAll');
  if (act) act.style.display = unread ? '' : 'none';

  if (!NF_ROWS.length) {
    // Пустое состояние отвечает на вопрос «почему пусто», а не советует
    // наугад: отбор и «только непрочитанные» — самые частые причины.
    txt('nfList', (NF_PREF || NF_UNREAD_ONLY)
      ? empty('🔍', 'Ничего не найдено',
              NF_UNREAD_ONLY ? 'Непрочитанных уведомлений нет' : 'В этом типе событий пока пусто',
              { label: '✖️ Сбросить отбор', fn: 'nfResetFilters()' })
      : empty('🔔', 'Уведомлений пока нет',
              'Здесь появится всё, о чём бот сообщал: итоги операций, ограничения аккаунтов, новые подписчики'));
    txt('nfMore', '');
    return;
  }
  txt('nfList', NF_ROWS.map(it => {
    const when = it.created_at ? ago(it.created_at) : '';
    return `<div class="li" style="display:block;padding:11px 14px${it.unread ? ';background:var(--bg3)' : ''}" ` +
      `onclick="nfRead(${it.id})">
      <div style="display:flex;align-items:center;gap:10px">
        <div class="ava">${esc(it.icon)}</div>
        <div class="li-body">
          <div class="li-name">${it.unread ? '<span style="color:var(--accent)">● </span>' : ''}${esc(it.title)}</div>
          <div class="li-sub">${esc(it.kind)} · ${esc(when)}</div>
        </div>
      </div>
      ${it.body ? `<div style="font-size:12px;color:var(--hint);margin-top:6px;white-space:pre-line">${esc(it.body)}</div>` : ''}
      ${_nfOpen(it)}
    </div>`;
  }).join(''));
  txt('nfMore', NF_HAS_MORE
    ? `<button class="btn btn-s" style="width:100%;font-size:13px;padding:10px" onclick="nfMore()">Загрузить ещё</button>`
    : '');
}

// Кнопка «Открыть» ведёт туда, о чём уведомление: «Операция #412 завершена»
// открывает саму операцию, а не оставляет владельца искать её руками через
// очередь. Имя функции приходит с сервера (белый список) — и всё равно
// проверяется здесь: экран мог переименовать её после того, как строка легла
// в ленту, а кнопка, которая ничего не делает, читается как сломанное
// приложение.
function _nfOpen(it) {
  const L = it.link;
  if (!L || !L.fn || typeof window[L.fn] !== 'function') return '';
  const arg = typeof L.arg === 'number'
    ? String(L.arg)
    : "'" + String(L.arg == null ? '' : L.arg).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  return '<button class="btn btn-s" style="margin-top:8px;padding:7px 12px;font-size:12px" ' +
    'onclick="event.stopPropagation(); nfRead(' + it.id + '); ' + esc(L.fn + '(' + arg + ')') +
    '">Открыть →</button>';
}

function nfResetFilters() {
  NF_PREF = '';
  NF_UNREAD_ONLY = false;
  loadNotificationsFeed();
}

// Значок непрочитанного на главном экране. Спрашиваем только счётчик, а не всю
// ленту: это пара байт на открытие приложения.
function setNotifBadge(n) {
  const el = document.getElementById('homeNotifBadge');
  if (!el) return;
  el.textContent = n > 99 ? '99+' : String(n || '');
  el.style.display = n ? '' : 'none';
}
