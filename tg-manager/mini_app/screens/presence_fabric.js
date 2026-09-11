// Ткань присутствия — декларативная самовосстанавливающаяся плоскость присутствия.
// Логическая ЛИЧНОСТЬ живёт отдельно от физического аккаунта («тела»). Умерло
// тело — личность переносится на новое, присутствие восстанавливается само.

let _pfCurrent = null;

const PF_STATE = {
  desired:    ['⏳', 'ждёт'],
  converging: ['🔄', 'вступает'],
  present:    ['✅', 'на месте'],
  lost:       ['⚠️', 'потеряно'],
};
const PF_EVENT = {
  materialized:      '🧬 получила тело',
  rematerialized:    '♻️ перенесена на новое тело',
  no_body:           '🚫 нет свободного аккаунта',
  drift:             '🌀 дрейф — восстанавливаю',
  target_converging: '🔄 вступает в чат',
  target_present:    '✅ на месте в чате',
  target_lost:       '⚠️ не удалось — повторю',
};

function _pfScreen(id, title, refreshFn) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="hdr-burger" onclick="back()">‹</div>' +
      '<div class="hdr-title">' + esc(title) + '</div>' +
      (refreshFn ? '<div class="hdr-burger" onclick="' + refreshFn + '" title="Обновить">⟳</div>'
                 : '<div class="hdr-burger"></div>') +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

async function openPresenceFabric() {
  _pfScreen('s-presence', '🧬 Ткань присутствия', 'openPresenceFabric()');
  push('s-presence');
  const body = document.getElementById('s-presence-body');
  body.innerHTML = '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>';
  try {
    const d = await api('/api/miniapp/presence/identities');
    body.innerHTML = _pfRenderList(d.identities || []);
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);padding:20px">' + esc(e.message) + '</div>';
  }
}

function _pfRenderList(items) {
  let h = '';
  h += '<div style="font-size:13px;color:var(--hint);padding:2px 2px 12px;line-height:1.5">' +
    'Опишите <b>кого</b> и <b>в каких чатах</b> нужно держать — система сама вступит и ' +
    '<b>будет удерживать</b> это состояние. Забанят аккаунт — личность автоматически ' +
    'переедет на другой и восстановит присутствие. Личность (имя, аватар, память) не ' +
    'зависит от конкретного аккаунта.</div>';

  h += '<div style="display:flex;gap:8px;margin-bottom:12px">' +
    '<input type="text" id="pf_name" placeholder="Имя личности (напр. Аня)" class="inp" style="flex:2">' +
    '<input type="text" id="pf_emoji" placeholder="🙂" class="inp" style="flex:1" maxlength="2">' +
    '<button class="btn btn-s" onclick="pfCreate()">➕</button></div>';

  if (!items.length) {
    h += '<div style="text-align:center;color:var(--hint);padding:20px">Личностей пока нет</div>';
    return h;
  }
  h += items.map(row => {
    const it = row.identity;
    const br = row.breakdown || {};
    const bodyDot = row.body_alive ? '🟢' : (it.acc_id ? '🔴' : '⚪️');
    const counts = ['present', 'converging', 'desired', 'lost']
      .filter(s => br[s]).map(s => PF_STATE[s][0] + br[s]).join(' ');
    const paused = it.status !== 'active';
    return '<div class="row" onclick="openPresenceIdentity(' + it.id + ')" style="cursor:pointer">' +
      '<div class="row-body"><div class="row-name">' + esc(it.avatar_emoji || '🧑') + ' ' + esc(it.name) +
      (paused ? ' <span style="color:var(--hint);font-size:11px">(на паузе)</span>' : '') + '</div>' +
      '<div class="row-val">' + bodyDot + ' тело ' + (row.body_alive ? 'живо' : (it.acc_id ? 'потеряно' : 'не назначено')) +
      (counts ? ' · ' + counts : '') + '</div></div><div style="color:var(--hint)">›</div></div>';
  }).join('');
  return h;
}

async function pfCreate() {
  const name = (document.getElementById('pf_name').value || '').trim();
  const emoji = (document.getElementById('pf_emoji').value || '🧑').trim() || '🧑';
  if (!name) { toast('Имя личности'); return; }
  try {
    const r = await api('/api/miniapp/presence/identity',
      { method: 'POST', body: JSON.stringify({ name: name, avatar_emoji: emoji }) });
    openPresenceIdentity(r.identity.id);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function openPresenceIdentity(iid) {
  _pfScreen('s-presence-id', '🧬 Личность', 'openPresenceIdentity(' + iid + ')');
  push('s-presence-id');
  const body = document.getElementById('s-presence-id-body');
  body.innerHTML = '<div style="text-align:center;color:var(--hint);padding:40px">Загрузка…</div>';
  try {
    _pfCurrent = await api('/api/miniapp/presence/identity/' + iid);
    body.innerHTML = _pfRenderIdentity(_pfCurrent);
  } catch (e) {
    body.innerHTML = '<div style="color:var(--red);padding:20px">' + esc(e.message) + '</div>';
  }
}

function _pfRenderIdentity(d) {
  const it = d.identity || {};
  const targets = d.targets || [];
  const events = d.events || [];
  let h = '';

  // Тело
  const bodyTxt = d.body_alive ? '🟢 живо (аккаунт #' + it.acc_id + ')'
    : (it.acc_id ? '🔴 потеряно — переедет на новое при реконсайле' : '⚪️ ещё не назначено');
  h += '<div style="padding:10px 12px;background:var(--bg2);border-radius:10px;margin-bottom:12px">' +
    '<div style="font-weight:600">' + esc(it.avatar_emoji || '🧑') + ' ' + esc(it.name) + '</div>' +
    '<div style="font-size:12px;color:var(--hint);margin-top:4px">Тело: ' + bodyTxt + '</div></div>';

  // Цели присутствия
  h += '<div class="sec">Где должна быть (желаемое состояние)</div>';
  if (!targets.length) {
    h += '<div style="font-size:12px;color:var(--hint);padding:4px 2px 8px">Целей пока нет — добавьте чат/канал ниже.</div>';
  } else {
    h += targets.map(t => {
      const st = PF_STATE[t.state] || ['•', t.state];
      return '<div class="row"><div class="row-body"><div class="row-name">' + esc(t.chat_ref) + '</div>' +
        '<div class="row-val">' + st[0] + ' ' + st[1] + '</div></div>' +
        '<button class="btn btn-s" style="color:var(--red);padding:5px 10px" onclick="pfDelTarget(' + t.id + ')">✕</button></div>';
    }).join('');
  }
  h += '<div style="display:flex;gap:8px;margin:6px 0 4px">' +
    '<input type="text" id="pf_chat" placeholder="@канал / ссылка-приглашение" class="inp" style="flex:1">' +
    '<button class="btn btn-s" onclick="pfAddTarget()">➕</button></div>' +
    '<div style="font-size:12px;color:var(--hint);padding:0 2px 12px">Реконсайлер сам вступит и будет удерживать присутствие. Актуация идёт через движок операций (с лимитами и защитой аккаунтов).</div>';

  // Действия
  h += '<button class="btn btn-p" style="width:100%;margin-bottom:6px" onclick="pfReconcile(' + it.id + ')">▶️ Реконсайл сейчас</button>';
  const paused = it.status !== 'active';
  h += '<button class="btn btn-s" style="width:100%;margin-bottom:6px" onclick="pfToggle(' + it.id + ',' + (paused ? 'true' : 'false') + ')">' +
    (paused ? '▶️ Возобновить личность' : '⏸ Поставить на паузу') + '</button>';
  h += '<button class="btn btn-s" style="width:100%;color:var(--red);margin-bottom:16px" onclick="pfDelete(' + it.id + ')">🗑 Удалить личность</button>';

  // Таймлайн событий (event-sourced)
  h += '<div class="sec">История (event log)</div>';
  if (!events.length) {
    h += '<div style="font-size:12px;color:var(--hint);padding:4px 2px 20px">Событий пока нет</div>';
  } else {
    h += '<div style="padding-bottom:20px">' + events.map(e => {
      const label = PF_EVENT[e.kind] || esc(e.kind);
      return '<div style="font-size:12px;padding:5px 2px;border-bottom:1px solid var(--bg2)">' +
        label + '</div>';
    }).join('') + '</div>';
  }
  return h;
}

async function pfAddTarget() {
  const iid = _pfCurrent && _pfCurrent.identity && _pfCurrent.identity.id;
  if (!iid) return;
  const chat = (document.getElementById('pf_chat').value || '').trim();
  if (!chat) { toast('Укажите чат/канал'); return; }
  try {
    await api('/api/miniapp/presence/identity/' + iid + '/target',
      { method: 'POST', body: JSON.stringify({ chat_ref: chat }) });
    openPresenceIdentity(iid);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function pfDelTarget(tid) {
  const iid = _pfCurrent && _pfCurrent.identity && _pfCurrent.identity.id;
  try {
    await api('/api/miniapp/presence/target/' + tid, { method: 'DELETE' });
    openPresenceIdentity(iid);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function pfReconcile(iid) {
  try {
    const r = await api('/api/miniapp/presence/identity/' + iid + '/reconcile',
      { method: 'POST', body: '{}' });
    const res = r.result || {};
    if (res.no_body) toast('Нет свободного здорового аккаунта для тела');
    else toast('Реконсайл: запущено ' + (res.submitted || 0) + ', на месте ' + (res.present || 0));
    openPresenceIdentity(iid);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function pfToggle(iid, resume) {
  try {
    await api('/api/miniapp/presence/identity/' + iid,
      { method: 'PATCH', body: JSON.stringify({ status: resume ? 'active' : 'paused' }) });
    openPresenceIdentity(iid);
  } catch (e) { toast(e.message || 'Ошибка'); }
}

async function pfDelete(iid) {
  if (!confirm('Удалить личность и все её цели присутствия?')) return;
  try {
    await api('/api/miniapp/presence/identity/' + iid, { method: 'DELETE' });
    toast('Удалено');
    openPresenceFabric();
  } catch (e) { toast(e.message || 'Ошибка'); }
}
