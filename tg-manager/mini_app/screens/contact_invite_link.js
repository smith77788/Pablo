// ── Инвайтинг на карточке контакта (screens/contact_invite_link.js) ────────
// Первопричина: invite_target_log/contact_opt_out хранят инвайты/отказы как
// строки-target'ы, изолированно от unified_contacts — карточка контакта не
// могла показать «приглашался N раз» и не давала поставить opt-out без
// ручного ввода username/телефона. Бэкенд (services/contact_invite_link.py +
// 3 эндпоинта uch_contact_*) уже готов и протестирован; этот файл — тонкий
// UI-слой поверх него.
//
// Подключается ОДНОЙ строкой из openContactDetail(id) в index.html —
// самостоятельный аддитивный блок в конце карточки, не трогает остальной
// рендер. Экран s-contactdetail уже существует — здесь только новая секция.
'use strict';

function _cilEnsureBox() {
  let box = document.getElementById('cilBox');
  if (box) return box;
  const host = document.getElementById('cdBody');
  if (!host) return null;
  host.insertAdjacentHTML('beforeend',
    '<div class="sec" style="margin-top:8px">📨 Инвайтинг</div>' +
    '<div id="cilBox" style="padding:4px 16px 12px"></div>');
  return document.getElementById('cilBox');
}

async function renderContactInviteLink(contactId) {
  const box = _cilEnsureBox();
  if (!box) return;
  box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/uch/contacts/' + contactId + '/invite_history');
    box.innerHTML = _cilRender(contactId, d);
  } catch (e) {
    box.innerHTML = '<div style="font-size:13px;color:var(--hint)">' +
      'Не удалось загрузить историю инвайтов</div>';
  }
}

function _cilRender(contactId, d) {
  const hist = d.history || [];
  const optedOut = !!d.opted_out;
  const histHtml = hist.length
    ? '<div class="lst" style="margin:6px 0">' + hist.slice(0, 10).map(h =>
        '<div class="row"><div class="row-body">' +
        '<div class="row-name">' + esc(h.group_key || '—') + '</div>' +
        '<div class="row-val">' + esc(String(h.created_at || '').slice(0, 16).replace('T', ' ')) +
        '</div></div></div>'
      ).join('') + '</div>'
    : '<div style="font-size:13px;color:var(--hint);padding:4px 0">Инвайтов не найдено</div>';
  const badge = optedOut
    ? '<span class="badge b-rd">🚫 В реестре «не приглашать»</span>'
    : '<span class="badge b-gr">✅ Можно приглашать</span>';
  const btn = optedOut
    ? '<button class="btn btn-s" onclick="_cilAllow(\'' + contactId + '\')" style="flex:0">' +
      '✅ Разрешить приглашать</button>'
    : '<button class="btn btn-s" onclick="_cilOptOut(\'' + contactId + '\')" style="flex:0">' +
      '🚫 Не приглашать</button>';
  return '<div style="margin-bottom:6px">' + badge + '</div>' + histHtml +
    '<div style="margin-top:6px">' + btn + '</div>';
}

async function _cilOptOut(contactId) {
  try {
    // Бэкенд регистрирует ВСЕ формы контакта разом (id/username/телефон) —
    // достаточно вызова без выбора конкретной формы (см. contact_invite_link.py).
    await api('/api/miniapp/uch/contacts/' + contactId + '/opt_out',
      { method: 'POST', body: JSON.stringify({}) });
    toast('🚫 Добавлено в реестр «не приглашать»');
    await renderContactInviteLink(contactId);
  } catch (e) {
    toast('Не удалось: ' + ((e && e.message) || 'ошибка'));
  }
}

async function _cilAllow(contactId) {
  try {
    await api('/api/miniapp/uch/contacts/' + contactId + '/allow_invite', { method: 'POST' });
    toast('✅ Ограничение снято');
    await renderContactInviteLink(contactId);
  } catch (e) {
    toast('Не удалось: ' + ((e && e.message) || 'ошибка'));
  }
}
