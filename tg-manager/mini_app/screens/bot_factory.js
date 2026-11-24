// Фабрика ботов: сборка бота по шаблону и её отправка.
//
// Вынесено из index.html без изменения поведения. Обычный классический
// скрипт, грузится после основного блока и делит с ним глобальную область.

// Фабрика забывала всё между запусками: режим («создать новых» / «по
// токену»), сколько ботов на аккаунт и как их называть. Владелец, который
// всегда добавляет по токену, каждый раз переключал режим заново.
function _bfRestorePrefs() {
  const p = (typeof loadFormPrefs === 'function') ? (loadFormPrefs('bot_factory') || {}) : {};
  if (p.mode === 'new' || p.mode === 'token') _BF_MODE = p.mode;
  const cnt = document.getElementById('bfCount');
  const c = parseInt(p.count, 10);
  if (cnt && c >= 1 && c <= 10) cnt.value = String(c);
  const nm = document.getElementById('bfNameMode');
  if (nm && (p.name_mode === 'keywords' || p.name_mode === 'num')) nm.value = p.name_mode;
}
function _bfSavePrefs(extra) {
  if (typeof saveFormPrefs === 'function') saveFormPrefs('bot_factory', Object.assign({ mode: _BF_MODE }, extra || {}));
}
function _bfBotsWord(n) { return plural(n, 'бот', 'бота', 'ботов'); }

async function openBotFactory() {
  // Повторный вызов после создания — обновление на месте: настройки, что
  // владелец только что выставил, не перетираем сохранёнными.
  const refresh = STACK.length && STACK[STACK.length - 1] === 's-botfactory';
  push('s-botfactory');
  if (!refresh) _bfRestorePrefs();
  txt('botFactoryStats','<div class="spin-wrap"><div class="spin"></div></div>');
  txt('botFactoryRecent','');
  bfSetMode(_BF_MODE);
  // Аккаунты для режима «создать новых»: каждый выбранный пишет @BotFather.
  _renderContactAccPicker('bfAccts', [], {botLimit: true});
  // Пока идёт запрос, в выпадашке висели экосистемы с ПРОШЛОГО открытия — и
  // бот привязывался к той, которую человек выбрал из устаревшего списка.
  { const _bfE = document.getElementById('bfEcosystem');
    if (_bfE) _bfE.innerHTML = '<option value="">Загрузка экосистем…</option>'; }
  // Load ecosystems
  try {
    const ecos = await api('/api/miniapp/ecosystems?limit=300');
    const sel = document.getElementById('bfEcosystem');
    // /api/miniapp/ecosystems отдаёт МАССИВ (экран «Экосистемы» так его и
    // читает: rows.length, rows.map). Здесь читали ecos.ecosystems — у массива
    // такого поля нет, выражение всегда давало [], и в выпадашке навсегда
    // оставалось одно «Не привязывать». То есть привязать к экосистеме было
    // нельзя, сколько бы их владелец ни создал.
    const ecoList = listOf(ecos, 'ecosystems');
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
    { const _v=`${d.total||0} ${_bfBotsWord(d.total||0)} · ${d.active||0} ${plural(d.active||0,'активный','активных','активных')}`;
      txt('m-botfactory-val',_v); txt('m-botfactory-val-2',_v); }
    const recent = d.recent||[];
    // Созданный бот открывается тапом: строка была просто текстом, и к
    // только что собранному боту шли через общий список ботов.
    txt('botFactoryRecent', recent.length ? recent.map(b=>`
      <div class="row tap" onclick="openBot(${Number(b.bot_id)})" role="button" tabindex="0">
        <div class="row-ico" style="background:var(--bg-green-16)">${b.is_active?'🟢':'🔴'}</div>
        <div class="row-body">
          <div class="row-name">@${esc(b.username||b.first_name||b.bot_id)}</div>
          <div class="row-val">${(b.added_at||b.created_at)?new Date(b.added_at||b.created_at).toLocaleDateString('ru-RU'):''}</div>
        </div>
        <span class="chev">›</span>
      </div>
    `).join('') : empty('🤖','Нет ботов','Добавьте первого бота'));
  } catch(e) {
    txt('botFactoryStats','');
    txt('botFactoryRecent', empty('⚠️','Ошибка',errRu(e), {label:'↺ Повторить',fn:retryCall('openBotFactory')}));
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
    if (!await askConfirm(`Создать ${total} ${_bfBotsWord(total)} через @BotFather на ${accts.length} ${plural(accts.length,'аккаунте','аккаунтах','аккаунтах')}? Операция необратима — боты будут зарегистрированы в Telegram.`)) return;
    btn.disabled = true; btn.textContent = '⏳ Ставлю в очередь...';
    try {
      const bfNameMode = document.getElementById('bfNameMode')?.value || 'keywords';
      _bfSavePrefs({ count: cnt, name_mode: bfNameMode });
      const r = await api('/api/miniapp/bot_factory/create_new', {method:'POST', body:JSON.stringify({
        account_ids: accts, count: cnt, name_template: name, uname_template: username, name_mode: bfNameMode,
      })});
      tg.HapticFeedback?.notificationOccurred('success');
      toast(`🏭 Создание ${r.count || total} ${_bfBotsWord(r.count || total)} запущено · операция #${r.op_id}`);
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
