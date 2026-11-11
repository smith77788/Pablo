// «Хранилище» как список диалогов Telegram: аватар с инициалами, имя, время,
// превью последнего сообщения, счётчик непрочитанных, закреплённые сверху,
// папки (Все / Непрочитанные / Ждут ответа / Удалённые / Архив), долгое
// нажатие — меню действий, тап по шапке переписки — карточка диалога с
// аналитикой. Решение «ждёт ли ответа» принимает сервер
// (services/vault_dialogs.py) — тот же, что шлёт напоминание в бот, поэтому
// число в уведомлении и список здесь совпадают.
//
// Классический скрипт: глобальные переменные (VAULT_FOLDER, _VAULT_COUNTS,
// _VAULT_CHATS, VAULT_INFO) объявлены в index.html, здесь только функции.

const _VAULT_FOLDER_TITLES = [
  ['all', 'Все'], ['unread', 'Непрочитанные'], ['waiting', 'Ждут ответа'],
  ['deleted', 'Удалённые'], ['archived', 'Архив'],
];

// Цвет аватара — по id, как в Telegram: один человек всегда одного цвета.
const _VAULT_AVA_COLORS = ['#e17076', '#7bc862', '#65aadd', '#a695e7', '#ee7aae', '#6ec9cb', '#faa774'];
function vaultAvatar(name, chatId, size) {
  const s = size || 48;
  const parts = String(name || '?').replace(/^@/, '').trim().split(/\s+/);
  const ini = ((parts[0] || '?')[0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
  const col = _VAULT_AVA_COLORS[Math.abs(Number(chatId) || 0) % _VAULT_AVA_COLORS.length];
  return `<div class="vava" style="width:${s}px;height:${s}px;background:${col};font-size:${Math.round(s * 0.38)}px">${esc(ini)}</div>`;
}

// Время в списке — как в Telegram: сегодня часы, на этой неделе день, дальше дата.
function vaultListTime(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString())
    return d.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'});
  if (now - d < 6 * 86400000) return d.toLocaleDateString('ru-RU', {weekday: 'short'});
  return d.toLocaleDateString('ru-RU', {day: '2-digit', month: '2-digit', year: '2-digit'});
}

function renderVaultFolders() {
  const el = document.getElementById('vaultFolders');
  if (!el) return;
  const c = _VAULT_COUNTS || {};
  el.innerHTML = _VAULT_FOLDER_TITLES.map(([f, t]) => {
    const n = f === 'all' ? 0 : (c[f] || 0);
    if (n === 0 && (f === 'deleted' || f === 'archived') && VAULT_FOLDER !== f) return '';
    const badge = n ? ` <span class="vfold-n">${n}</span>` : '';
    return `<span class="vfold${VAULT_FOLDER === f ? ' on' : ''}" role="tab" tabindex="0" onclick="setVaultInbox('${f}')">${t}${badge}</span>`;
  }).join('');
}

function vaultRowHtml(c) {
  const time = vaultListTime(c.last_date);
  let prev = esc(c.last_preview || '');
  if (c.last_direction === 'out' && prev) prev = `<span class="vrow-you">Вы:</span> ${prev}`;
  if (c.last_deleted) prev = '🗑 ' + prev;
  const marks = (c.purchased ? '<span title="Купил">💰</span>' : '')
              + (c.muted ? '<span title="Без напоминаний">🔕</span>' : '');
  let right = '';
  if (c.unread) right = `<span class="vrow-unread${c.muted ? ' muted' : ''}">${c.unread}</span>`;
  else if (c.awaiting_reply) right = '<span class="vrow-wait" title="Ждёт ответа">⏳</span>';
  else if (c.pinned) right = '<span class="vrow-pin">📌</span>';
  const tick = c.last_direction === 'out' ? '<span class="vrow-tick">✓</span>' : '';
  return `<div class="vrow tap" data-cid="${c.chat_id}" onclick="openVaultChat(${c.chat_id})"
      oncontextmenu="event.preventDefault();openVaultRowMenu(${c.chat_id})">
    ${vaultAvatar(c.peer_name, c.chat_id)}
    <div class="vrow-body">
      <div class="vrow-top"><div class="vrow-name">${esc(c.peer_name)} ${marks}</div>
        <div class="vrow-time">${tick}${esc(time)}</div></div>
      <div class="vrow-bot"><div class="vrow-prev">${prev || '—'}</div>${right}</div>
    </div></div>`;
}

// Долгое нажатие на строке открывает меню (на телефоне contextmenu не всегда
// приходит, поэтому ещё и таймер на касании).
let _vaultPressTimer = null;
function vaultBindLongPress(root) {
  if (!root || root.dataset.lp) return;
  root.dataset.lp = '1';
  root.addEventListener('touchstart', e => {
    const row = e.target.closest('.vrow');
    if (!row) return;
    clearTimeout(_vaultPressTimer);
    _vaultPressTimer = setTimeout(() => {
      _vaultPressTimer = 'fired';
      openVaultRowMenu(Number(row.dataset.cid));
    }, 550);
  }, {passive: true});
  const cancel = () => { if (_vaultPressTimer !== 'fired') clearTimeout(_vaultPressTimer); };
  root.addEventListener('touchmove', cancel, {passive: true});
  root.addEventListener('touchend', e => {
    if (_vaultPressTimer === 'fired') { e.preventDefault(); _vaultPressTimer = null; }
    else cancel();
  });
}

function _vaultChatById(cid) {
  return (_VAULT_CHATS || []).find(c => Number(c.chat_id) === Number(cid)) || null;
}

function openVaultRowMenu(cid) {
  if (VAULT_READ_ONLY) { toast('Чужое хранилище доступно только для просмотра'); return; }
  const c = _vaultChatById(cid) || VAULT_INFO || {};
  const items = [
    [c.pinned ? '📌 Открепить' : '📌 Закрепить', `vaultChatAction(${cid},{pinned:${!c.pinned}})`],
    [c.unread ? '👁 Отметить прочитанным' : '', `vaultChatAction(${cid},{read:true})`],
    [c.awaiting_reply ? '✅ Отвечено (ответил другим путём)' : (c.handled ? '↩️ Снять «отвечено»' : ''),
     `vaultChatAction(${cid},{handled:${!c.handled}})`],
    [c.purchased ? '💰 Снять «купил»' : '💰 Купил / воспользовался услугой', `vaultChatAction(${cid},{purchased:${!c.purchased}})`],
    [c.muted ? '🔔 Напоминать о нём' : '🔕 Не напоминать о нём', `vaultChatAction(${cid},{muted:${!c.muted}})`],
    [c.archived ? '🗂 Вернуть из архива' : '🗄 В архив', `vaultChatAction(${cid},{archived:${!c.archived}})`],
    ['ℹ️ О диалоге', `openVaultDialogInfo(${cid})`],
  ].filter(i => i[0]);
  const box = document.getElementById('vaultMenuBody');
  if (!box) return;
  document.getElementById('vaultMenuTitle').textContent = c.peer_name || 'Диалог';
  box.innerHTML = items.map(([t, fn]) =>
    `<div class="vmenu-i tap" role="button" tabindex="0" onclick="closeModal('mo-vault-menu');${fn}">${t}</div>`).join('');
  showModal('mo-vault-menu');
}

async function vaultChatAction(cid, change) {
  try {
    await api(vaultPath('/api/miniapp/vault/chat/' + cid + '/state'),
              {method: 'POST', body: JSON.stringify(change)});
    const msg = change.purchased ? '💰 Отмечено: купил — напоминаний о нём не будет'
              : change.handled ? '✅ Отмечено отвеченным'
              : change.archived ? '🗄 В архиве' : change.archived === false ? '🗂 Возвращён из архива'
              : change.pinned ? '📌 Закреплён' : change.muted ? '🔕 Без напоминаний' : 'Готово';
    if (!change.read) toast(msg);
    await reloadVaultDialogs();
    if (VAULT_CHAT === cid && document.getElementById('s-vaultchat').classList.contains('active')) {
      vaultChatSubtitle();
    }
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не получилось')); }
}

async function reloadVaultDialogs() {
  const q = (document.getElementById('vaultSearch') || {}).value || '';
  const d = await api(vaultPath('/api/miniapp/vault/dialogs?folder=' + encodeURIComponent(VAULT_FOLDER)
                                + (q.trim().length >= 2 ? '&q=' + encodeURIComponent(q.trim()) : '')));
  _VAULT_COUNTS = d.counts || {};
  renderVaultChats(d.chats || []);
  return d;
}

// Подзаголовок шапки переписки: как «был(а) недавно» в Telegram, только про дело.
function vaultChatSubtitle() {
  const el = document.getElementById('vchat-sub');
  if (!el) return;
  const c = _vaultChatById(VAULT_CHAT) || VAULT_INFO;
  if (!c) { el.textContent = ''; return; }
  const bits = [];
  if (c.awaiting_reply) bits.push('⏳ ждёт ответа');
  if (c.purchased) bits.push('💰 купил');
  if (c.archived) bits.push('в архиве');
  bits.push((c.total || 0) + ' сообщ.');
  el.textContent = bits.join(' · ');
}

function _vaultDur(mins) {
  if (mins == null) return '—';
  if (mins < 1) return 'меньше минуты';
  if (mins < 60) return Math.round(mins) + ' мин';
  if (mins < 1440) return (mins / 60).toFixed(1).replace('.', ',') + ' ч';
  return (mins / 1440).toFixed(1).replace('.', ',') + ' дн';
}

async function openVaultDialogInfo(cid) {
  cid = cid || VAULT_CHAT;
  const box = document.getElementById('vaultInfoBody');
  if (!box) return;
  box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  showModal('mo-vault-info');
  try {
    const i = await api(vaultPath('/api/miniapp/vault/chat/' + cid + '/info'));
    VAULT_INFO = i;
    const dt = s => s ? new Date(s).toLocaleString('ru-RU') : '—';
    const cell = (v, l) => `<div class="vinfo-c"><div class="vinfo-v">${v}</div><div class="vinfo-l">${l}</div></div>`;
    const media = (i.media_by_type || []).map(m => `${esc(m.label)} ${m.count}`).join(' · ');
    const ct = i.contact;
    const crm = ct ? [ct.funnel_label ? 'воронка: ' + esc(ct.funnel_label) : '',
                      ct.orders_paid ? 'оплаченных заказов: ' + ct.orders_paid : ''].filter(Boolean).join(' · ') : '';
    const ro = VAULT_READ_ONLY;
    const act = (on, t, ch) => ro ? '' :
      `<button class="btn btn-s${on ? ' on' : ''}" style="font-size:12px;padding:8px 6px" onclick="vaultInfoAction(${cid},${ch})">${t}</button>`;
    box.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
        ${vaultAvatar(i.peer_name, i.chat_id, 56)}
        <div><div style="font-size:17px;font-weight:700">${esc(i.peer_name)}</div>
          <div style="font-size:13px;color:var(--hint)">${i.peer_username ? '@' + esc(i.peer_username) : 'id ' + i.chat_id}</div></div>
      </div>
      <div class="vinfo-grid">
        ${cell(num(i.total), 'сообщений')}${cell(num(i.incoming), 'от собеседника')}
        ${cell(num(i.outgoing), 'ваших')}${cell(num(i.active_days), 'активных дней')}
        ${cell(_vaultDur(i.owner_reply_median_min), 'ваш ответ, медиана')}
        ${cell(_vaultDur(i.peer_reply_median_min), 'ответ собеседника')}
        ${cell(num(i.last_7d), 'за 7 дней')}${cell(num(i.last_30d), 'за 30 дней')}
      </div>
      <div class="vinfo-rows">
        <div><span>Начало диалога</span><b>${dt(i.first_date)}</b></div>
        <div><span>Последнее сообщение</span><b>${dt(i.last_date)}</b></div>
        <div><span>Последний ваш ответ</span><b>${dt(i.last_out)}</b></div>
        ${i.deleted_by_peer ? `<div><span>Удалено собеседником</span><b>${i.deleted_by_peer}</b></div>` : ''}
        ${i.edited ? `<div><span>Изменено</span><b>${i.edited}</b></div>` : ''}
        ${media ? `<div><span>Вложения</span><b>${media}</b></div>` : ''}
        ${crm ? `<div><span>Контакт</span><b>${crm}</b></div>` : ''}
      </div>
      ${ro ? '' : `<div class="vinfo-acts">
        ${act(i.handled, i.handled ? '↩️ Снять «отвечено»' : '✅ Отвечено', `{handled:${!i.handled}}`)}
        ${act(i.purchased, i.purchased ? '💰 Не купил' : '💰 Купил', `{purchased:${!i.purchased}}`)}
        ${act(i.pinned, i.pinned ? '📌 Открепить' : '📌 Закрепить', `{pinned:${!i.pinned}}`)}
        ${act(i.muted, i.muted ? '🔔 Напоминать' : '🔕 Не напоминать', `{muted:${!i.muted}}`)}
        ${act(i.archived, i.archived ? '🗂 Из архива' : '🗄 В архив', `{archived:${!i.archived}}`)}
        <button class="btn btn-s" style="font-size:12px;padding:8px 6px" onclick="downloadVaultExport('html', ${cid})">⬇️ Экспорт</button>
      </div>
      <label style="display:block;font-size:12px;color:var(--hint);margin:12px 0 4px">📝 Заметка к диалогу</label>
      <textarea id="vaultNote" class="inp" rows="2" style="margin:0" placeholder="Что важно помнить про этого человека">${esc(i.note || '')}</textarea>
      <button class="btn btn-s" style="width:100%;margin-top:6px;font-size:12px" onclick="saveVaultNote(${cid})">Сохранить заметку</button>`}
      <div style="font-size:11px;color:var(--hint);margin-top:10px">${i.awaiting_reply ? '⏳ Ждёт вашего ответа с ' + dt(i.waiting_since) : 'Ответа не ждёт'}</div>`;
  } catch (e) { box.innerHTML = errHtml(errRu(e), 'openVaultDialogInfo(' + cid + ')'); }
}

async function vaultInfoAction(cid, change) {
  await vaultChatAction(cid, change);
  openVaultDialogInfo(cid);
}

async function saveVaultNote(cid) {
  const v = (document.getElementById('vaultNote') || {}).value || '';
  try {
    await api(vaultPath('/api/miniapp/vault/chat/' + cid + '/state'),
              {method: 'POST', body: JSON.stringify({note: v})});
    toast('📝 Заметка сохранена');
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не сохранено')); }
}

// Разделитель дат между сообщениями — «9 ноября», как в Telegram.
function vaultDaySep(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  const t = d.toDateString() === now.toDateString() ? 'Сегодня'
          : d.toLocaleDateString('ru-RU', {day: 'numeric', month: 'long',
              year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric'});
  return `<div class="vday"><span>${esc(t)}</span></div>`;
}

function vaultBubblesHtml(msgs, prevIso) {
  let last = prevIso ? new Date(prevIso).toDateString() : null;
  return msgs.map(m => {
    const day = m.date ? new Date(m.date).toDateString() : null;
    const sep = day && day !== last ? vaultDaySep(m.date) : '';
    if (day) last = day;
    return sep + vaultBubble(m);
  }).join('');
}
