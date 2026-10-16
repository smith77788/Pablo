// Фабрика ботов: сборка бота по шаблону и её отправка.
//
// Вынесено из index.html без изменения поведения. Обычный классический
// скрипт, грузится после основного блока и делит с ним глобальную область.

async function openBotFactory() {
  push('s-botfactory');
  txt('botFactoryStats','<div class="spin-wrap"><div class="spin"></div></div>');
  txt('botFactoryRecent','');
  bfSetMode(_BF_MODE);
  // Аккаунты для режима «создать новых»: каждый выбранный пишет @BotFather.
  _renderContactAccPicker('bfAccts', []);
  // Пока идёт запрос, в выпадашке висели экосистемы с ПРОШЛОГО открытия — и
  // бот привязывался к той, которую человек выбрал из устаревшего списка.
  { const _bfE = document.getElementById('bfEcosystem');
    if (_bfE) _bfE.innerHTML = '<option value="">Загрузка экосистем…</option>'; }
  // Load ecosystems
  try {
    const ecos = await api('/api/miniapp/ecosystems');
    const sel = document.getElementById('bfEcosystem');
    // /api/miniapp/ecosystems отдаёт МАССИВ (экран «Экосистемы» так его и
    // читает: rows.length, rows.map). Здесь читали ecos.ecosystems — у массива
    // такого поля нет, выражение всегда давало [], и в выпадашке навсегда
    // оставалось одно «Не привязывать». То есть привязать к экосистеме было
    // нельзя, сколько бы их владелец ни создал.
    const ecoList = Array.isArray(ecos) ? ecos : [];
    sel.innerHTML = '<option value="">Не привязывать</option>' +
      ecoList.map(e=>`<option value="${e.id}">${esc(e.name||'Экосистема #'+e.id)}</option>`).join('');
  } catch(e) {
    const sel = document.getElementById('bfEcosystem');
    if (sel) sel.innerHTML = '<option value="">Не привязывать</option>'
      + '<option value="" disabled>⚠️ Экосистемы не загрузились</option>';
  }
  // Load stats and recent
  try {
    const d = await api('/api/miniapp/bot_factory');
    txt('botFactoryStats',`
      <div class="kpi-card"><div class="kpi-val">${d.total||0}</div><div class="kpi-lbl">Всего ботов</div></div>
      <div class="kpi-card"><div class="kpi-val" style="${kpiTone(d.active,'green')}">${d.active||0}</div><div class="kpi-lbl">Активных</div></div>
    `);
    txt('m-botfactory-val',`${d.total||0} ботов · ${d.active||0} активных`);
    const recent = d.recent||[];
    txt('botFactoryRecent', recent.length ? recent.map(b=>`
      <div class="row">
        <div class="row-ico" style="background:var(--bg-green-16)">${b.is_active?'🟢':'🔴'}</div>
        <div class="row-body">
          <div class="row-name">@${esc(b.username||b.first_name||b.bot_id)}</div>
          <div class="row-val">${(b.added_at||b.created_at)?new Date(b.added_at||b.created_at).toLocaleDateString('ru-RU'):''}</div>
        </div>
      </div>
    `).join('') : empty('🤖','Нет ботов','Добавьте первый бот'));
  } catch(e) {
    txt('botFactoryStats','');
    txt('botFactoryRecent', empty('⚠️','Ошибка',errRu(e)));
  }
}

async function submitBotFactory() {
  const token = document.getElementById('bfToken').value.trim();
  const name = document.getElementById('bfName').value.trim();
  const username = document.getElementById('bfUsername').value.trim();
  const desc = document.getElementById('bfDesc').value.trim();
  const shortDesc = document.getElementById('bfShortDesc').value.trim();
  const ecosystemId = document.getElementById('bfEcosystem').value;
  const errEl = document.getElementById('bfErr');
  const btn = document.getElementById('bfBtn');
  errEl.textContent = '';
  if (!name) { errEl.textContent = 'Введите имя бота (можно спинтакс)'; return; }

  // РЕЖИМ «создать новых» — реальная операция через @BotFather (как в боте).
  if (_BF_MODE === 'new') {
    const accts = [...document.querySelectorAll('#bfAccts .ca-acc-cb:checked')].map(cb => +cb.value);
    const cnt = Math.max(1, parseInt(document.getElementById('bfCount').value) || 1);
    if (!accts.length) { errEl.textContent = 'Выберите хотя бы один аккаунт для @BotFather'; return; }
    const total = accts.length * cnt;
    if (!await askConfirm(`Создать ${total} бот(ов) через @BotFather на ${accts.length} ${plural(accts.length,'аккаунте','аккаунтах','аккаунтах')}? Операция необратима — боты будут зарегистрированы в Telegram.`)) return;
    btn.disabled = true; btn.textContent = '⏳ Ставлю в очередь...';
    try {
      const bfNameMode = document.getElementById('bfNameMode')?.value || 'keywords';
      const r = await api('/api/miniapp/bot_factory/create_new', {method:'POST', body:JSON.stringify({
        account_ids: accts, count: cnt, name_template: name, uname_template: username, name_mode: bfNameMode,
      })});
      tg.HapticFeedback?.notificationOccurred('success');
      toast(`🏭 Создание ${r.count || total} бот(ов) запущено · операция #${r.op_id}`);
      if (r.op_id) pollOpResult(r.op_id, 'Фабрика ботов');
      openBotFactory();
    } catch(e) {
      errEl.textContent = errRu(e);
      tg.HapticFeedback?.notificationOccurred('error');
    } finally { btn.disabled = false; bfSetMode(_BF_MODE); }
    return;
  }

  // РЕЖИМ «по токену» — регистрация уже созданного бота.
  if (!token) { errEl.textContent = 'Введите токен бота'; return; }
  btn.disabled = true; btn.textContent = '⏳ Создание...';
  try {
    const r = await api('/api/miniapp/bot_factory/create', {method:'POST', body:JSON.stringify({
      token, name, username, description: desc, short_description: shortDesc,
      ecosystem_id: ecosystemId ? +ecosystemId : null,
    })});
    tg.HapticFeedback?.notificationOccurred('success');
    // Честный итог: один бот, синхронно, с перечнем реально применённых настроек.
    const applied = (r && r.applied_settings || []).length;
    toast(r && r.already_exists
      ? 'ℹ️ Бот уже был добавлен'
      : `✅ Бот @${(r && r.username) || ''} добавлен${applied ? ` · настроек применено: ${applied}` : ''}`);
    document.getElementById('bfToken').value = '';
    document.getElementById('bfName').value = '';
    document.getElementById('bfUsername').value = '';
    document.getElementById('bfDesc').value = '';
    document.getElementById('bfShortDesc').value = '';
    openBotFactory();
  } catch(e) {
    errEl.textContent = errRu(e);
    tg.HapticFeedback?.notificationOccurred('error');
  } finally { btn.disabled = false; bfSetMode(_BF_MODE); }
}
