// 🗄 Подключение Хранилища диалогов — окно «в один-два тапа».
//
// Хранилище — одна из главных фишек продукта, а подключить его можно было
// только с экрана Хранилища, о котором новичок не знает. Это окно открывают
// шаг «Быстрого старта» (services/onboarding.py, action vault_connect), ссылка
// #vault_connect из бота и подсказки на главной.
//
// Само подключение делает Telegram, не мы: бота добавляют в «Telegram для
// бизнеса → Чат-боты». Поэтому окно сводит работу к двум действиям —
// скопировать имя бота и вставить его там, — а статус проверяет само, когда
// человек возвращается в приложение из настроек Telegram.

let _VC_OPEN = false;
let _VC_BOT = '';

function _vcBody(st) {
  const bot = _VC_BOT ? '@' + _VC_BOT : 'бота';
  const diag = (st && st.diagnostics) || {};
  const connected = !!(st && st.connected);
  const disabled = !connected && diag.health === 'disabled';
  const status = connected
    ? '<div class="vc-status vc-on">✅ Подключено — переписка сохраняется</div>'
    : (disabled
      ? '<div class="vc-status vc-warn">⏸ Отключено — новые сообщения не сохраняются</div>'
      : '<div class="vc-status vc-off">○ Ещё не подключено</div>');
  const perks =
    '<div class="vc-perks">' +
      '<div>🗑 <b>Удалённые сообщения</b> — остаются у вас, даже если собеседник стёр их или весь диалог</div>' +
      '<div>✏️ <b>Правки</b> — видно, что было написано до изменения</div>' +
      '<div>🔔 <b>Уведомления</b> — бот сообщит, когда кто-то удалит сообщение</div>' +
      '<div>🔎 <b>Поиск и ответ</b> — по всей истории, ответ прямо из архива</div>' +
      '<div>🔒 Всё хранится в зашифрованном виде</div>' +
    '</div>';
  if (connected) {
    return status + perks +
      '<button class="btn btn-p" style="width:100%;margin-top:12px" onclick="vcOpenArchive()">🗄 Открыть Хранилище</button>' +
      '<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="closeVaultConnect()">Готово</button>';
  }
  return status + perks +
    '<div class="vc-steps">' +
      '<div><span class="vc-n">1</span> Скопируйте имя бота</div>' +
      '<div><span class="vc-n">2</span> Настройки Telegram → <b>Telegram для бизнеса</b> → <b>Чат-боты</b></div>' +
      '<div><span class="vc-n">3</span> Вставьте <b>' + esc(bot) + '</b> и разрешите доступ к чатам</div>' +
    '</div>' +
    (_VC_BOT ? '<button class="btn btn-p" style="width:100%;margin-top:12px" onclick="vcCopyBot()">📋 Скопировать ' + esc(bot) + '</button>' : '') +
    '<button class="btn btn-s" id="vcCheckBtn" style="width:100%;margin-top:8px" onclick="vcCheck(true)">🔄 Я подключил — проверить</button>' +
    '<button class="btn btn-d" style="width:100%;margin-top:8px" onclick="closeVaultConnect()">Позже</button>' +
    '<div class="vc-hint">Вернитесь сюда после подключения — статус обновится сам.</div>';
}

function _vcEl() {
  let bg = document.getElementById('vcMbg');
  if (bg) return bg;
  bg = document.createElement('div');
  bg.className = 'mbg';
  bg.id = 'vcMbg';
  bg.style.zIndex = '9998';
  bg.innerHTML =
    '<style>' +
      '#vcMbg .vc-status{font-size:13px;font-weight:700;padding:8px 10px;border-radius:10px;margin-top:8px}' +
      '#vcMbg .vc-on{background:var(--bg-green-16,rgba(52,199,89,.16));color:var(--green)}' +
      '#vcMbg .vc-warn{background:rgba(251,191,36,.14);color:var(--orange)}' +
      '#vcMbg .vc-off{background:var(--bg2);color:var(--hint)}' +
      '#vcMbg .vc-perks{display:flex;flex-direction:column;gap:6px;margin-top:10px;font-size:13px;line-height:1.4}' +
      '#vcMbg .vc-note{margin-top:10px;font-size:12px;line-height:1.45;padding:8px 10px;border-radius:10px;background:rgba(251,191,36,.12)}' +
      '#vcMbg .vc-steps{display:flex;flex-direction:column;gap:6px;margin-top:12px;font-size:13px;line-height:1.4}' +
      '#vcMbg .vc-n{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;background:var(--accent);color:#fff;font-size:11px;font-weight:700;margin-right:6px}' +
      '#vcMbg .vc-hint{font-size:11px;color:var(--hint);text-align:center;margin-top:8px}' +
    '</style>' +
    '<div class="modal">' +
      '<div class="modal-title">🗄 Хранилище диалогов</div>' +
      '<div style="font-size:12px;color:var(--hint);margin-top:2px">Ваша переписка не пропадёт — даже если её удалят.</div>' +
      '<div id="vcBody"><div class="spin-wrap"><div class="spin"></div></div></div>' +
    '</div>';
  bg.onclick = e => { if (e.target === bg) closeVaultConnect(); };
  document.body.appendChild(bg);
  return bg;
}

async function openVaultConnect() {
  const bg = _vcEl();
  bg.classList.add('show');
  _VC_OPEN = true;
  await vcCheck(false);
}

function closeVaultConnect() {
  _VC_OPEN = false;
  const bg = document.getElementById('vcMbg');
  if (bg) bg.classList.remove('show');
}

// Проверка статуса. manual — нажата кнопка: тогда и отрицательный итог
// говорим вслух, иначе кнопка «проверить» выглядит как ничего не сделавшая.
async function vcCheck(manual) {
  const body = document.getElementById('vcBody');
  if (!body) return;
  const btn = document.getElementById('vcCheckBtn');
  if (btn) { btn.disabled = true; btn.textContent = '🔄 Проверяю…'; }
  let st = null;
  try {
    st = await api('/api/miniapp/vault/status');
  } catch (e) {
    body.innerHTML = errHtml(errRu(e, 'Не удалось узнать статус'), 'vcCheck(true)');
    return;
  }
  const wasOff = body.dataset.connected !== '1';
  _VC_BOT = (st && st.bot_username) || _VC_BOT;
  body.innerHTML = _vcBody(st);
  body.dataset.connected = st && st.connected ? '1' : '0';
  if (st && st.connected && wasOff && body.dataset.seen === '1') {
    try { tg.HapticFeedback && tg.HapticFeedback.notificationOccurred('success'); } catch (_) {}
    toast('✅ Хранилище подключено');
    if (typeof loadOnboarding === 'function') loadOnboarding();
  } else if (manual && !(st && st.connected)) {
    toast('Пока не видим подключения — проверьте, что бот добавлен в «Чат-боты»');
  }
  body.dataset.seen = '1';
}

function vcCopyBot() {
  if (!_VC_BOT) return;
  try { tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch (_) {}
  copyToClipboard('@' + _VC_BOT, '📋 Скопировано — вставьте в «Чат-боты»');
}

function vcOpenArchive() {
  closeVaultConnect();
  openVault();
}

// Человек ушёл в настройки Telegram и вернулся — проверяем сами, без кнопки.
function _vcOnReturn() {
  if (_VC_OPEN && document.visibilityState !== 'hidden') vcCheck(false);
}
try { document.addEventListener('visibilitychange', _vcOnReturn); } catch (_) {}
try { tg && tg.onEvent && tg.onEvent('activated', _vcOnReturn); } catch (_) {}
