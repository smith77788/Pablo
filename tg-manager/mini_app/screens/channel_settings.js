// Настройки канала и группы — то, что в Telegram лежит в «Изменить»:
// тип (публичный/частный), обсуждение, подпись авторов, запрет копирования,
// реакции, автоудаление, заявки на вступление; у групп — разрешения
// участников, медленный режим, темы, история для новичков.
//
// Сервер: GET/POST /api/miniapp/channel/{id}/settings (services/channel_settings.py).
// Каждая правка применяется сразу через аккаунт-владельца канала, и экран
// перерисовывается по ответу — то есть показывает, что РЕАЛЬНО стоит в Telegram.

let _csId = null;
let _csData = null;

const CS_TTL = [[0, 'Выкл.'], [86400, '1 день'], [604800, '1 неделя'], [2592000, '1 месяц']];
const CS_SLOW = [[0, 'Выкл.'], [10, '10 сек'], [30, '30 сек'], [60, '1 мин'], [300, '5 мин'], [900, '15 мин'], [3600, '1 час']];

function _csModal(inner) {
  let el = document.getElementById('csMbg');
  if (!el) {
    el = document.createElement('div');
    el.className = 'mbg';
    el.id = 'csMbg';
    el.addEventListener('click', ev => { if (ev.target === el) closeModal('csMbg'); });
    document.body.appendChild(el);
  }
  el.innerHTML = '<div class="modal">' +
    `<div class="modal-x" onclick="closeModal('csMbg')" role="button" tabindex="0" aria-label="Закрыть">✕</div>` +
    inner + '</div>';
  return el;
}

async function openChSettings(chId) {
  _csId = chId;
  _csData = null;
  _csModal('<div class="modal-title">⚙️ Настройки</div><div class="spin-wrap"><div class="spin"></div></div>');
  showModal('csMbg');
  try {
    _csData = await api('/api/miniapp/channel/' + encodeURIComponent(chId) + '/settings');
    _csRender();
  } catch (e) {
    _csModal('<div class="modal-title">⚙️ Настройки</div>' +
      `<div style="color:var(--red);font-size:14px;padding:8px 0">${esc(errRu(e, 'Не удалось прочитать настройки'))}</div>` +
      `<button class="btn btn-s" style="width:100%" onclick="openChSettings('${escJs(String(chId))}')">Повторить</button>`);
  }
}

function _csToggle(key, label, on, hint) {
  return `<label style="display:flex;align-items:center;justify-content:space-between;gap:10px;cursor:pointer;padding:9px 0;border-bottom:1px solid var(--sep)">
    <span style="font-size:14px">${label}${hint ? `<div style="font-size:12px;color:var(--hint);line-height:1.35">${hint}</div>` : ''}</span>
    <input type="checkbox" ${on ? 'checked' : ''} style="width:18px;height:18px;accent-color:var(--accent);flex:none"
      onchange="csSet('${key}', this.checked, this)">
  </label>`;
}

function _csSelect(key, label, value, opts, hint) {
  const o = opts.map(([v, t]) => `<option value="${esc(String(v))}" ${String(v) === String(value) ? 'selected' : ''}>${esc(t)}</option>`).join('');
  return `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 0;border-bottom:1px solid var(--sep)">
    <span style="font-size:14px">${label}${hint ? `<div style="font-size:12px;color:var(--hint);line-height:1.35">${hint}</div>` : ''}</span>
    <select style="min-height:34px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);padding:4px 8px;font-size:13px;max-width:55%"
      onchange="csSet('${key}', this.value, this)">${o}</select>
  </div>`;
}

function _csRender() {
  const d = _csData || {};
  const s = d.settings || {};
  const g = !!s.is_group;
  const what = g ? 'группы' : 'канала';
  const stat = (ico, name, val) => `<div style="flex:1 1 30%;background:var(--bg3);border-radius:var(--radius);padding:8px;text-align:center">
    <div style="font-size:16px">${ico} ${num(val || 0)}</div><div style="font-size:11px;color:var(--hint)">${name}</div></div>`;

  let html = `<div class="modal-title">⚙️ Настройки ${what}</div>
    <div style="font-size:13px;color:var(--hint);margin:-4px 0 10px">${esc(s.title || '')}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
      ${stat('👥', g ? 'участников' : 'подписчиков', s.members)}
      ${stat('🛡', 'админов', s.admins)}
      ${stat('🚫', 'в чёрном списке', s.banned)}
      ${s.requests_pending ? stat('📨', 'заявок ждут', s.requests_pending) : ''}
    </div>`;

  // Тип: публичный с адресом или частный.
  html += `<div class="sec" style="margin:4px 0">Тип ${what}</div>
    <div style="display:flex;gap:8px;align-items:center;padding:4px 0 8px">
      <input id="csUsername" value="${esc(s.username || '')}" placeholder="адрес (пусто — частный)"
        style="flex:1;min-height:36px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);padding:6px 8px;font-size:13px">
      <button class="btn btn-s" onclick="csSaveUsername(this)">Сохранить</button>
    </div>
    <div style="font-size:12px;color:var(--hint);margin-bottom:8px">${s.username ? '🌐 Публичный: t.me/' + esc(s.username) : '🔒 Частный: вход только по ссылке-приглашению'}</div>`;

  html += `<div class="sec" style="margin:8px 0 0">Основное</div>`;
  if (!g) {
    const groups = d.groups || [];
    const opts = [[0, s.linked_chat_id ? 'Отвязать' : 'Нет']].concat(groups.map(x => [x.channel_id, x.title]));
    if (s.linked_chat_id && !groups.some(x => Number(x.channel_id) === Number(s.linked_chat_id))) {
      opts.unshift([s.linked_chat_id, 'Привязанная группа #' + s.linked_chat_id]);
    }
    html += _csSelect('discussion', '💬 Обсуждение', s.linked_chat_id || 0, opts,
      groups.length ? 'Группа для комментариев' : 'Нет ваших групп под тем же аккаунтом');
    html += _csToggle('signatures', '✍️ Подписывать сообщения', s.signatures);
  }
  html += _csToggle('noforwards', '🔒 Запретить копирование', s.noforwards,
    'Нельзя пересылать, копировать и сохранять материалы');
  html += _csSelect('reactions', '❤️ Реакции', s.reactions === 'none' ? 'none' : 'all',
    [['all', 'Все'], ['none', 'Выключены']].concat(s.reactions === 'some' ? [['some', 'Выборочно (в Telegram)']] : []));
  html += _csSelect('ttl', '⏳ Автоудаление сообщений', s.ttl || 0, CS_TTL);
  html += _csToggle('join_request', '📨 Вступление по заявке', s.join_request,
    'Новых участников одобряет админ; о заявках придёт уведомление');

  if (g) {
    html += `<div class="sec" style="margin:8px 0 0">Группа</div>`;
    html += _csSelect('slowmode', '🐢 Медленный режим', s.slowmode || 0, CS_SLOW);
    html += _csToggle('forum', '🗂 Темы', s.forum, 'Чат делится на темы');
    html += _csToggle('hidden_prehistory', '🙈 Скрыть историю от новичков', s.hidden_prehistory);
    html += _csToggle('join_to_send', '✋ Писать — только вступившим', s.join_to_send);
    const perms = s.permissions || {};
    const labels = d.perm_labels || {};
    const allowed = Object.keys(labels).filter(k => perms[k] !== false).length;
    html += `<div class="sec" style="margin:8px 0 0">🔑 Разрешения участников · ${allowed}/${Object.keys(labels).length}</div>`;
    for (const k of Object.keys(labels)) {
      html += `<label style="display:flex;align-items:center;justify-content:space-between;cursor:pointer;padding:8px 0;border-bottom:1px solid var(--sep)">
        <span style="font-size:14px">${esc(labels[k])}</span>
        <input type="checkbox" ${perms[k] !== false ? 'checked' : ''} style="width:18px;height:18px;accent-color:var(--accent)"
          onchange="csSet('permissions', {'${escJs(k)}': this.checked}, this)">
      </label>`;
    }
  }
  html += `<div id="csErr" style="color:var(--red);font-size:13px;min-height:16px;padding-top:8px"></div>
    <div style="font-size:12px;color:var(--hint);line-height:1.4">Изменения применяются сразу через аккаунт-владельца ${what}. Приветствие и чистка системных сообщений — в «Модераторе чатов».</div>`;
  _csModal(html);
}

async function csSet(key, value, el) {
  const err = document.getElementById('csErr');
  if (err) err.textContent = '';
  if (el) el.disabled = true;
  if (key === 'discussion' || key === 'ttl' || key === 'slowmode') value = Number(value);
  if (key === 'reactions' && value === 'some') { if (el) el.disabled = false; return; }
  try {
    const r = await api('/api/miniapp/channel/' + encodeURIComponent(_csId) + '/settings', {
      method: 'POST', body: JSON.stringify({key, value, is_group: !!(_csData?.settings?.is_group)})});
    _csData.settings = r.settings;
    _csRender();
    toast('✅ Сохранено');
  } catch (e) {
    // Возвращаем экран к тому, что реально стоит, и говорим почему не вышло.
    _csRender();
    const er = document.getElementById('csErr');
    if (er) er.textContent = '⚠️ ' + errRu(e, 'Не удалось применить');
  }
}

async function csSaveUsername(btn) {
  const v = (document.getElementById('csUsername')?.value || '').trim().replace(/^@/, '');
  const was = _csData?.settings?.username || '';
  if (v === was) { toast('Адрес не изменился'); return; }
  if (!v && was && !await askConfirm(`Сделать частным? Адрес @${was} освободится, и его может занять кто угодно.`)) return;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  await csSet('username', v, null);
  if (btn) { btn.disabled = false; btn.textContent = 'Сохранить'; }
}
