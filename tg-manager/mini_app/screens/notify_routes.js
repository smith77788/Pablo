// Каналы уведомлений о ботах: отдельный канал-приёмник на каждую сеть проектов.
// Маршрут = куда (канал, где главный бот админ) + какие боты + какие события
// («запустили бота», «заявка на вступление»). Одно событие уходит во все
// подходящие каналы; личка — страховка, её можно выключить у маршрута.
//
// Сервер: /api/miniapp/notify_routes (services/mini_app_notify_routes.py).

let _nrData = null;
let _nrEdit = null;   // маршрут в редакторе (копия), null — список

function _nrModal(inner) {
  let el = document.getElementById('nrMbg');
  if (!el) {
    el = document.createElement('div');
    el.className = 'mbg';
    el.id = 'nrMbg';
    el.addEventListener('click', ev => { if (ev.target === el) closeModal('nrMbg'); });
    document.body.appendChild(el);
  }
  el.innerHTML = '<div class="modal">' +
    `<div class="modal-x" onclick="closeModal('nrMbg')" role="button" tabindex="0" aria-label="Закрыть">✕</div>` +
    inner + '</div>';
}

async function openNotifyRoutes() {
  _nrEdit = null;
  _nrModal('<div class="modal-title">📡 Каналы уведомлений</div><div class="spin-wrap"><div class="spin"></div></div>');
  showModal('nrMbg');
  await _nrLoad();
}

async function _nrLoad() {
  try {
    _nrData = await api('/api/miniapp/notify_routes');
    _nrEdit ? _nrRenderEdit() : _nrRenderList();
  } catch (e) {
    _nrModal('<div class="modal-title">📡 Каналы уведомлений</div>' +
      `<div style="color:var(--red);font-size:14px;padding:8px 0">${esc(errRu(e, 'Не удалось загрузить'))}</div>` +
      '<button class="btn btn-s" style="width:100%" onclick="_nrLoad()">Повторить</button>');
  }
}

function _nrBotName(id) {
  if (Number(id) === 0) return 'Каналы с главным ботом';
  const b = (_nrData?.bots || []).find(x => Number(x.bot_id) === Number(id));
  return b ? b.title : 'бот #' + id;
}

function _nrHowTo() {
  const d = _nrData || {};
  return `<div style="font-size:12px;color:var(--hint);line-height:1.45;background:var(--bg3);border-radius:var(--radius);padding:8px 10px;margin:8px 0">
    Чтобы канал появился в списке, добавьте в него ${d.bot_username ? '@' + esc(d.bot_username) : 'главного бота'} администратором с правом «Публикация сообщений».
    ${d.add_url ? `<div style="margin-top:6px"><a href="${esc(d.add_url)}" onclick="try{tg.openTelegramLink('${escJs(d.add_url)}');return false}catch(_){return true}" style="color:var(--accent)">➕ Добавить бота в канал</a> · <span style="color:var(--accent);cursor:pointer" onclick="_nrLoad()">обновить список</span></div>` : ''}
  </div>`;
}

function _nrRenderList() {
  const d = _nrData || {};
  const routes = d.routes || [];
  const ev = d.events || {};
  const rows = routes.map(r => {
    const bots = r.bot_ids == null ? 'все боты' : r.bot_ids.map(_nrBotName).join(', ');
    const events = (r.events || []).map(e => ev[e] || e).join(' · ');
    return `<div class="li tap" onclick="nrouteOpen(${Number(r.id)})">
      <div class="ava ava-purple">${r.enabled ? '📡' : '⏸'}</div>
      <div class="li-body">
        <div class="li-name">${esc(r.name || r.chat_title || 'Канал #' + r.chat_id)}</div>
        <div class="li-sub">→ ${esc(r.chat_title || String(r.chat_id))} · ${esc(bots)}</div>
        <div class="li-sub">${esc(events)}${r.mute_dm ? ' · без лички' : ''}</div>
        ${r.last_error ? `<div class="li-sub" style="color:var(--red)">⚠️ ${esc(r.last_error)}</div>` : ''}
      </div><span class="chev">›</span></div>`;
  }).join('');
  _nrModal(`<div class="modal-title">📡 Каналы уведомлений</div>
    <div style="font-size:13px;color:var(--hint);line-height:1.45;margin-bottom:8px">Отдельный канал на каждую сеть проектов: выберите ботов и события — уведомления о них придут в этот канал.</div>
    ${routes.length ? `<div class="lst">${rows}</div>` : '<div style="font-size:14px;padding:10px 0;color:var(--hint)">Пока нет ни одного канала — уведомления приходят в личку.</div>'}
    <button class="btn btn-p" style="width:100%;margin-top:10px" onclick="nrouteOpen(0)">➕ Новый канал уведомлений</button>
    ${_nrHowTo()}`);
}

function nrouteOpen(id) {
  const r = (_nrData?.routes || []).find(x => Number(x.id) === Number(id));
  _nrEdit = r ? JSON.parse(JSON.stringify(r)) : {
    id: null, name: '', chat_id: (_nrData?.chats || [])[0]?.chat_id || 0,
    bot_ids: null, events: Object.keys(_nrData?.events || {start: 1, join_request: 1}),
    mute_dm: false, enabled: true,
  };
  _nrRenderEdit();
}

function _nrRenderEdit() {
  const d = _nrData || {};
  const r = _nrEdit;
  const chats = d.chats || [];
  const chatOpts = chats.map(c => `<option value="${esc(String(c.chat_id))}" ${String(c.chat_id) === String(r.chat_id) ? 'selected' : ''}>${esc(c.title)}</option>`).join('');
  const all = r.bot_ids == null;
  const sel = new Set((r.bot_ids || []).map(Number));
  const botRows = [{bot_id: 0, title: 'Каналы и чаты, где админ главный бот'}].concat(d.bots || []).map(b => `
    <label style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--sep);cursor:pointer;${all ? 'opacity:.5' : ''}">
      <input type="checkbox" ${all || sel.has(Number(b.bot_id)) ? 'checked' : ''} ${all ? 'disabled' : ''} style="width:18px;height:18px;accent-color:var(--accent)"
        onchange="nrouteToggleBot(${Number(b.bot_id)}, this.checked)">
      <span style="font-size:14px">${esc(b.title)}</span></label>`).join('');
  const evRows = Object.entries(d.events || {}).map(([k, label]) => `
    <label style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--sep);cursor:pointer">
      <span style="font-size:14px">${esc(label)}</span>
      <input type="checkbox" ${(r.events || []).includes(k) ? 'checked' : ''} style="width:18px;height:18px;accent-color:var(--accent)"
        onchange="nrouteToggleEvent('${escJs(k)}', this.checked)"></label>`).join('');
  const field = 'width:100%;min-height:38px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);padding:6px 8px;font-size:14px;box-sizing:border-box';
  _nrModal(`<div class="modal-title">${r.id ? '✏️ Канал уведомлений' : '➕ Новый канал уведомлений'}</div>
    <div class="sec" style="margin:4px 0">Название сети</div>
    <input style="${field}" maxlength="60" placeholder="Например: Сеть «Крипто»" value="${esc(r.name || '')}" oninput="_nrEdit.name=this.value">
    <div class="sec" style="margin:10px 0 4px">Куда слать</div>
    ${chats.length ? `<select style="${field}" onchange="_nrEdit.chat_id=Number(this.value)">${chatOpts}</select>` : '<div style="font-size:14px;color:var(--red)">Нет подключённых каналов</div>'}
    ${_nrHowTo()}
    <div class="sec" style="margin:10px 0 4px">Какие боты</div>
    <label style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--sep);cursor:pointer">
      <span style="font-size:14px;font-weight:600">Все боты (и новые тоже)</span>
      <input type="checkbox" ${all ? 'checked' : ''} style="width:18px;height:18px;accent-color:var(--accent)" onchange="nrouteAllBots(this.checked)"></label>
    <div style="max-height:220px;overflow:auto">${botRows}</div>
    <div class="sec" style="margin:10px 0 4px">События</div>
    ${evRows}
    <label style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--sep);cursor:pointer">
      <span style="font-size:14px">🔕 Не дублировать в личку<div style="font-size:12px;color:var(--hint)">Если канал недоступен — всё равно придёт в личку</div></span>
      <input type="checkbox" ${r.mute_dm ? 'checked' : ''} style="width:18px;height:18px;accent-color:var(--accent)" onchange="_nrEdit.mute_dm=this.checked"></label>
    <label style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;cursor:pointer">
      <span style="font-size:14px">✅ Включён</span>
      <input type="checkbox" ${r.enabled ? 'checked' : ''} style="width:18px;height:18px;accent-color:var(--accent)" onchange="_nrEdit.enabled=this.checked"></label>
    <div id="nrErr" style="color:var(--red);font-size:13px;min-height:16px"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
      <button class="btn btn-p" style="flex:1 1 100%" onclick="nrouteSave(this)">Сохранить</button>
      ${r.id ? `<button class="btn btn-s" style="flex:1" onclick="nrouteTest(${Number(r.id)}, this)">🔔 Проверить</button>
      <button class="btn btn-d" style="flex:1" onclick="nrouteDelete(${Number(r.id)})">🗑 Удалить</button>` : ''}
      <button class="btn btn-s" style="flex:1 1 100%" onclick="_nrEdit=null;_nrRenderList()">← К списку</button>
    </div>`);
}

function nrouteAllBots(on) {
  _nrEdit.bot_ids = on ? null : [];
  _nrRenderEdit();
}

function nrouteToggleBot(id, on) {
  const s = new Set((_nrEdit.bot_ids || []).map(Number));
  if (on) s.add(Number(id)); else s.delete(Number(id));
  _nrEdit.bot_ids = [...s];
}

function nrouteToggleEvent(k, on) {
  const s = new Set(_nrEdit.events || []);
  if (on) s.add(k); else s.delete(k);
  _nrEdit.events = [...s];
}

async function nrouteSave(btn) {
  const err = document.getElementById('nrErr');
  if (err) err.textContent = '';
  if (btn) { btn.disabled = true; btn.textContent = 'Сохраняю…'; }
  try {
    await api('/api/miniapp/notify_routes', {method: 'POST', body: JSON.stringify(_nrEdit)});
    toast('✅ Канал уведомлений сохранён');
    _nrEdit = null;
    await _nrLoad();
  } catch (e) {
    if (err) err.textContent = '⚠️ ' + errRu(e, 'Не удалось сохранить');
    if (btn) { btn.disabled = false; btn.textContent = 'Сохранить'; }
  }
}

async function nrouteTest(id, btn) {
  if (btn) btn.disabled = true;
  try {
    await api('/api/miniapp/notify_routes/' + Number(id) + '/test', {method: 'POST', body: '{}'});
    toast('🔔 Тестовое сообщение отправлено в канал');
  } catch (e) {
    const err = document.getElementById('nrErr');
    if (err) err.textContent = '⚠️ ' + errRu(e, 'Канал не принял сообщение');
  } finally { if (btn) btn.disabled = false; }
}

async function nrouteDelete(id) {
  if (!await askConfirm('Удалить этот канал уведомлений? Сам канал в Telegram не трогаем — уведомления просто перестанут туда приходить.')) return;
  try {
    await api('/api/miniapp/notify_routes/' + Number(id), {method: 'DELETE'});
    toast('🗑 Удалено');
    _nrEdit = null;
    await _nrLoad();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось удалить')); }
}
