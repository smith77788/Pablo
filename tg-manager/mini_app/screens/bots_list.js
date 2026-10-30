// Экран «Боты»: список, фильтры, поиск по флоту, массовый выбор и добавление.
//
// Вынесено из index.html без изменения поведения. Карточка бота и её
// подэкраны — в bot_detail.js, фабрика — в bot_factory.js, сетка — в
// bot_mesh.js: разбиение по экранам, а не по размеру файла.
//
// Файл — обычный классический скрипт: он делит глобальную область с
// index.html и грузится ПОСЛЕ основного блока. Поэтому глобальные
// переменные экрана (BOTS, BOT_SEL, фильтры) остаются объявленными в
// index.html — переносить объявления нельзя, их читает код, который
// грузится раньше.

async function loadBots() {
  try {
    BOT_OFFSET = 0;
    const d = await api('/api/miniapp/bots?limit=' + BOT_PAGE + '&offset=0');
    BOTS = d.bots||[];
    BOT_OFFSET = BOTS.length;
    BOT_TOTAL = (d.total!=null?d.total:BOTS.length);
    const cntEl = document.getElementById('bots-cnt');
    if (cntEl) cntEl.textContent = BOT_TOTAL ? BOT_TOTAL+' ботов' : '';
    renderBotFilterChips();
    renderBots();
  } catch(e) {
    const wrap = document.getElementById('botList');
    if (wrap) { wrap.className=''; wrap.innerHTML = errHtml(errRu(e),'loadBots()'); }
  }
}

// «Загрузить ещё» догружает СЛЕДУЮЩУЮ страницу. Раньше эндпоинт не умел offset,
// и кнопка перезапрашивала срез побольше: 500, потом 1000, потом 1500 — те же
// строки заново, и всё это с потолком 5000, за которым боты были недостижимы.
// Выбор хранится по bot_id, догрузка его не рвёт.
async function loadMoreBots() {
  if (BOT_LOADING || BOTS.length >= BOT_TOTAL) return;
  BOT_LOADING = true;
  const box = document.getElementById('botLoadMore');
  if (box) box.innerHTML = '<div class="spin-wrap" style="padding:8px"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/bots?limit=' + BOT_PAGE + '&offset=' + BOT_OFFSET);
    const more = d.bots||[];
    // Пустая страница (ботов удалили между запросами) — дальше идти некуда,
    // иначе кнопка «загрузить ещё» осталась бы вечной.
    if (!more.length) { BOT_TOTAL = BOTS.length; }
    else {
      const known = new Set(BOTS.map(b=>String(b.bot_id)));
      more.forEach(b => { if (!known.has(String(b.bot_id))) BOTS.push(b); });
      BOT_OFFSET += more.length;
      if (d.total!=null) BOT_TOTAL = d.total;
    }
    const cntEl = document.getElementById('bots-cnt');
    if (cntEl) cntEl.textContent = BOT_TOTAL ? BOT_TOTAL+' ботов' : '';
    renderBotFilterChips();
    renderBots();
    if (BOT_SEL_MODE) updateBotSelBar();
  } catch(e) {
    toast('⚠️ '+(errRu(e,'Ошибка').substring(0,60)));
    // Не оставляем застрявшую крутилку: у сценария всегда есть видимый выход.
    if (box) box.innerHTML = '<button class="btn btn-s" onclick="loadMoreBots()" style="width:100%;font-size:13px;padding:10px">↺ Повторить загрузку</button>';
  } finally { BOT_LOADING = false; }
}

function onBotSearch(v) {
  BOT_SEARCH = (v||'').trim().toLowerCase();
  clearTimeout(_botSearchT);
  _botSearchT = setTimeout(()=>{ renderBotFilterChips(); renderBots(); }, 200);
}

function filterBots(mode) {
  BOT_FILTER = mode;
  renderBotFilterChips();
  renderBots();
  try { tg.HapticFeedback?.selectionChanged(); } catch(_) {}
}

function resetBotFilters() {
  BOT_SEARCH = ''; BOT_FILTER = 'all';
  const i = document.getElementById('botSearch'); if (i) i.value = '';
  renderBotFilterChips(); renderBots();
}

function renderBotFilterChips() {
  const box = document.getElementById('botFilterChips');
  if (!box) return;
  const by = {ok:0, problem:0, off:0};
  BOTS.forEach(b => { const st=_botState(b); if (st==='ok') by.ok++; else if (st==='off') by.off++; else by.problem++; });
  // Пустой срез не показываем: чип, за которым никого нет, — обещание без содержания.
  const defs = [['all','Все',BOTS.length], ['problem','⚠️ Проблема',by.problem],
                ['ok','🟢 Работают',by.ok], ['off','⏸ Выключены',by.off]];
  const vis = defs.filter(([k,,n]) => k==='all' || n>0);
  // Активный срез мог опустеть после перезагрузки — иначе список остался бы
  // пустым, а чипа, которым это снять, на экране уже нет.
  if (!vis.some(([k]) => k === BOT_FILTER)) BOT_FILTER = 'all';
  if (vis.length <= 1) { box.style.display='none'; box.innerHTML=''; return; }
  box.style.display='flex';
  box.innerHTML = vis.map(([k,lbl,n]) =>
    `<span class="lf-chip${BOT_FILTER===k?' on':''}" role="button" tabindex="0" aria-pressed="${BOT_FILTER===k}" onclick="filterBots('${k}')">${lbl} ${n}</span>`).join('');
}

// Остаток списка — честной строкой, а не молчанием.
function renderBotLoadMore(shownCount) {
  const box = document.getElementById('botLoadMore');
  if (!box) return;
  const loaded = BOTS.length;
  if (!loaded || loaded >= BOT_TOTAL) { box.innerHTML=''; return; }
  const filtering = !!(BOT_SEARCH || BOT_FILTER !== 'all');
  // Поиск идёт по загруженным: «ничего не найдено» при неполном списке — не
  // ответ, а половина ответа. Говорим, сколько именно просмотрено.
  //
  // Прежде здесь была и вторая строка — «больше сервер за раз не отдаёт, сузьте
  // поиск», — которая появлялась на потолке 5000. Выполнить этот совет было
  // нельзя: поиск шёл по тем же загруженным строкам, и остальные боты не
  // доставались никак. Потолка больше нет, страницы идут по offset до конца.
  const note = filtering
    ? `Найдено ${shownCount!=null?shownCount:0} среди загруженных ${loaded} из ${BOT_TOTAL}.`
    : '';
  box.innerHTML = (note ? `<div class="lst-more-note">${esc(note)}</div>` : '')
    + `<button class="btn btn-s" onclick="loadMoreBots()" style="width:100%;font-size:13px;padding:10px">Загрузить ещё · показано ${loaded} из ${BOT_TOTAL}</button>`;
}

// Скан флота на ботов: спрашивает @BotFather от лица каждого аккаунта.
// Токены при этом не добываются — это отдельный диалог на каждого бота;
// задача скана быстро показать, ЧТО есть и на каком аккаунте.
async function scanFleetBots() {
  if (!await askConfirm('Спросить @BotFather на каждом аккаунте флота, какие боты им принадлежат?\nЭто займёт время: обращения к BotFather разносятся, чтобы не ловить лимиты.')) return;
  try {
    const r = await api('/api/miniapp/bots/scan_fleet', {method:'POST', body:'{}'});
    toast('🔍 Скан запущен' + (r.op_id ? ' · операция #'+r.op_id : ''));
    if (r.op_id) pollOpResult(r.op_id, 'Скан флота на ботов');
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,120)); }
}

// Подключение найденных пакетом: скан даёт username, а подключить бота можно
// только по токену — его отдаёт сам @BotFather по кнопке «API Token».
// Без этого список найденных был бы тупиком: видно, но сделать нельзя.
async function connectDiscoveredBots() {
  if (!await askConfirm('Подключить найденных ботов?\nInfragram спросит их токены у @BotFather от лица аккаунтов-владельцев. Это займёт время: обращения разносятся, чтобы не ловить лимиты.')) return;
  try {
    const r = await api('/api/miniapp/bots/connect_discovered', {method:'POST', body:'{}'});
    toast('🔌 Подключение запущено' + (r.op_id ? ' · операция #'+r.op_id : ''));
    if (r.op_id) pollOpResult(r.op_id, 'Подключение найденных ботов', loadBots);
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,140)); }
}

// Включить режим bot-to-bot у сети — нужно для координации ботов (Bot Mesh).
// Первый прогон делаем канарейкой на 2 ботах: разбор меню BotFather зависит от
// локали и на живой сессии не выверен, лучше проверить на паре.
async function enableBotMesh() {
  const canary = await askConfirm(
    'Включить режим bot-to-bot у сети ботов?\n\n'
    + 'Это нужно, чтобы боты координировали задачи между собой. Включает '
    + 'аккаунт-владелец через @BotFather.\n\n'
    + 'РЕКОМЕНДУЕМ начать с проверки на 2 ботах: разбор меню BotFather зависит '
    + 'от языка интерфейса. Нажмите ОК для канарейки (2 бота) или Отмена.');
  if (!canary) return;
  try {
    const r = await api('/api/miniapp/bots/enable_b2b',
      {method:'POST', body: JSON.stringify({limit: 2})});
    toast('🕸 Включение запущено на 2 ботах' + (r.op_id ? ' · #'+r.op_id : ''));
    if (r.op_id) pollOpResult(r.op_id, 'Включение bot-to-bot (канарейка)', loadBots);
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,140)); }
}

// Полноценный экран, а не всплывающее окно: из списка найденных надо уметь
// ДЕЙСТВОВАТЬ — подключить конкретного бота или открыть управление уже
// подключённым. Раньше это был текст без единого действия.
async function showDiscoveredBots() {
  push('s-foundbots');
  txt('foundBotsStats','<div class="spin-wrap"><div class="spin"></div></div>');
  txt('foundBotsList','');
  try {
    const d = await api('/api/miniapp/bots/discovered');
    const bots = d.bots||[];
    const pending = (d.total||0) - (d.connected||0);
    txt('foundBotsStats', `
      <div class="kpi-card"><div class="kpi-val">${d.total||0}</div><div class="kpi-lbl">Найдено</div></div>
      <div class="kpi-card"><div class="kpi-val" style="${kpiTone(d.connected,'green')}">${d.connected||0}</div><div class="kpi-lbl">Подключено</div></div>
      <div class="kpi-card"><div class="kpi-val" style="color:${pending?'var(--orange,#f59e0b)':'var(--hint)'}">${pending}</div><div class="kpi-lbl">Осталось</div></div>`);
    if (!bots.length) {
      txt('foundBotsList', empty('🤖','Найденных ботов пока нет','Запустите скан флота — Infragram спросит @BotFather на каждом аккаунте',{label:'🔍 Найти на флоте',fn:'scanFleetBots()'}));
      return;
    }
    txt('foundBotsList', bots.map(b=>`
      <div class="row" style="border-left:3px solid ${b.connected?'var(--green)':'var(--orange,#f59e0b)'}">
        <div class="row-ico" style="background:var(--bg2)">${b.connected?'✅':'🤖'}</div>
        <div class="row-body">
          <div class="row-name">@${esc(b.username)}</div>
          <div class="row-val">${b.connected?'подключён — доступно управление':'не подключён'}${b.account?' · '+esc(b.account):''}</div>
        </div>
        <div class="row-right">
          ${b.connected
            ? `<button class="btn btn-s" style="padding:5px 10px;font-size:12px;min-width:0" onclick="openBotByUsername('${esc(b.username)}')">⚙️ Управление</button>`
            : `<button class="btn btn-p" style="padding:5px 10px;font-size:12px;min-width:0" onclick="connectOneBot('${esc(b.username)}')">🔌 Подключить</button>`}
        </div>
      </div>`).join(''));
  } catch(e) {
    txt('foundBotsList', empty('⚠️','Ошибка',errRu(e)));
    txt('foundBotsStats', '');
  }
}

// Подключить одного конкретного бота из найденных.
async function connectOneBot(uname) {
  try {
    const r = await api('/api/miniapp/bots/connect_discovered',
                        {method:'POST', body:JSON.stringify({usernames:[uname]})});
    toast('🔌 Подключаю @'+uname + (r.op_id ? ' · #'+r.op_id : ''));
    if (r.op_id) pollOpResult(r.op_id, 'Подключение @'+uname, showDiscoveredBots);
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,140)); }
}

// Открыть меню управления уже подключённым ботом по его username.
async function openBotByUsername(uname) {
  try {
    // Наполняем тот же кэш, что и экран ботов, вместе с его счётчиками: иначе
    // следующая «загрузить ещё» пошла бы с нулевого offset и запросила бы
    // первую страницу заново.
    if (!BOTS || !BOTS.length) {
      const d = await api('/api/miniapp/bots?limit=' + BOT_PAGE + '&offset=0');
      BOTS = d.bots||[];
      BOT_OFFSET = BOTS.length;
      BOT_TOTAL = (d.total!=null?d.total:BOTS.length);
    }
    const lu = String(uname).toLowerCase();
    const b = (BOTS||[]).find(x => String(x.username||'').toLowerCase() === lu
                                || String(x.extra_username||'').toLowerCase() === lu);
    if (!b) { toast('⚠️ Бот ещё не появился в списке — обновите раздел «Боты»'); return; }
    openBot(b.bot_id);
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,120)); }
}

function renderBots() {
  const wrap = document.getElementById('botList');
  if (!wrap) return;
  // Пустой список ≠ «ботов нет»: боты могли быть созданы с аккаунтов флота, а
  // скан ресурсов их никогда не находил (бот — не канал). Даём найти их, а не
  // только вставлять токены руками по одному.
  if (!BOTS.length) { wrap.className=''; wrap.innerHTML = empty('🤖','Нет подключённых ботов','Боты, созданные с ваших аккаунтов, можно найти сканом флота — или подключить по токену от @BotFather',{label:'🔍 Найти ботов на флоте',fn:'scanFleetBots()'}); renderBotLoadMore(0); return; }
  const _list = _botsFiltered();
  // Пустой результат поиска ≠ «ботов нет»: разные причины, разный выход.
  if (!_list.length) {
    wrap.className='';
    wrap.innerHTML = empty('🔍','Ничего не найдено',
      'Ни один бот не подходит под поиск и выбранный срез',
      {label:'✖️ Сбросить поиск и срез', fn:'resetBotFilters()'});
    renderBotLoadMore(0);
    return;
  }
  wrap.className = 'bot-cards';
  wrap.innerHTML = _list.map(b=>{
    // Показываем ИМЯ бота (first_name) как основную подпись, @username — второй
    // строкой (раньше выводился только @username, имя не было видно вовсе).
    const name = b.first_name || (b.username?'@'+b.username:'Бот #'+b.bot_id);
    // Второй username на app/ai/agent/game (Telegram, 2026) — рядом с основным.
    const handle = [(b.first_name && b.username) ? '@'+b.username : '',
                    b.extra_username ? '@'+b.extra_username : ''].filter(Boolean).join(' · ');
    const initials = (b.first_name||b.username||'Бот').replace('@','').slice(0,2).toUpperCase();
    const grad = BOT_GRADS[b.bot_id % BOT_GRADS.length];
    const active = b.is_active;
    const subs = b.subscriber_count||0;
    const sel = BOT_SEL.has(b.bot_id);
    const handleHtml = handle ? `<div class="bot-card-lbl" style="margin-top:-2px">${esc(handle)}</div>` : '';
    if (BOT_SEL_MODE) {
      return `<div class="bot-card ${sel?'sel':''}" onclick="toggleBotSel(${b.bot_id})">
        <div class="bot-card-ava" style="background:${grad}">${initials}</div>
        <div class="bot-card-name">${esc(name)}</div>
        ${handleHtml}
        <div class="bot-card-subs">${num(subs)}</div>
        <div class="bot-card-lbl">подписчиков</div>
        <span class="badge ${sel?'b-gr':'b-gy'}" style="margin-top:4px;display:inline-flex">${sel?'✓ Выбран':'Выбрать'}</span>
      </div>`;
    }
    // Честное состояние: бот с отозванным токеном выглядел здесь ровно как
    // рабочий — «🟢 Активен», — пока подписчики писали ему и не получали
    // ответа. Знал об этом только серверный лог.
    const h = b.health || {};
    const badge = (h.label && h.state && h.state!=='ok' && (active || h.state==='dead'))
      ? `<span class="badge b-gy" style="margin-top:4px;display:inline-flex;background:var(--bg2);color:${h.attention?'var(--red,#ef4444)':'var(--orange,#f59e0b)'}">${esc(h.label)}</span>`
      : `<span class="badge ${active?'b-gr':'b-gy'}" style="margin-top:4px;display:inline-flex">${active?'🟢 Активен':'⏸ Убран из работы'}</span>`;
    // Бейдж последней проверки живости (checkAllBots): мёртвый = токен отозван.
    const chk = BOT_CHECK[b.bot_id];
    const chkBadge = chk===BOT_STATUS_DEAD
        ? `<span class="badge b-gy" style="margin-top:4px;display:inline-flex;background:var(--bg2);color:var(--red,#ef4444)">🔴 Токен отозван</span>`
      : chk===BOT_STATUS_UNKNOWN
        ? `<span class="badge b-gy" style="margin-top:4px;display:inline-flex">⚪ Не ответил</span>`
      : chk===BOT_STATUS_ALIVE
        ? `<span class="badge b-gr" style="margin-top:4px;display:inline-flex">✅ Жив</span>`
      : '';
    // На карточке — только пауза (⏻/▶️). Удаление из системы — на экране
    // бота, кнопкой «🗑 Удалить», с подтверждением.
    return `<div class="bot-card" ${h.attention?'style="border-color:var(--red,#ef4444)"':''} onclick="openBot(${b.bot_id})">
      <div class="bot-card-del" title="${active?'Поставить на паузу':'Вернуть в работу'}" onclick="event.stopPropagation();botPowerConfirm(${b.bot_id},'${esc(name)}',${!!active})">${active?'⏻':'▶️'}</div>
      <div class="bot-card-ava" style="background:${grad}">${initials}</div>
      <div class="bot-card-name">${esc(name)}</div>
      ${handleHtml}
      <div class="bot-card-subs">${num(subs)}</div>
      <div class="bot-card-lbl">подписчиков</div>
      ${badge}
      ${chkBadge}
    </div>`;
  }).join('');
  renderBotLoadMore(_list.length);
}

function toggleBotSelMode() {
  BOT_SEL_MODE = !BOT_SEL_MODE;
  BOT_SEL.clear();
  const btn = document.getElementById('botSelToggle');
  if (btn) btn.textContent = BOT_SEL_MODE ? '✖️ Отмена' : '☑️ Выбрать';
  document.getElementById('botSelBar').style.display = BOT_SEL_MODE ? '' : 'none';
  syncSelBarGap('botSelBar');
  renderBots();
  updateBotSelBar();
}

function toggleBotSel(id) {
  if (BOT_SEL.has(id)) BOT_SEL.delete(id); else BOT_SEL.add(id);
  renderBots();
  updateBotSelBar();
}

// «Все» — это все, кто сейчас в списке после поиска и среза. Раньше сюда
// попадали все ЗАГРУЖЕННЫЕ боты, тогда как в шапке стояло полное число: при 700
// ботах и срезе в 500 рассылка молча уходила по неполному списку.
function botSelAll() {
  const list = _botsFiltered();
  list.forEach(b=>BOT_SEL.add(b.bot_id));
  renderBots(); updateBotSelBar();
  if (BOTS.length < BOT_TOTAL) {
    toast(`Выбрано ${list.length} · загружено ${BOTS.length} из ${BOT_TOTAL}`);
  }
}

function botSelClear() { BOT_SEL.clear(); renderBots(); updateBotSelBar(); }

function updateBotSelBar() {
  const el = document.getElementById('botSelCount');
  if (el) el.textContent = 'Выбрано: '+BOT_SEL.size;
}

// ── Проверка живости и полное удаление ботов ───────────────────────────────
// «Полное удаление» ≠ «убрать из работы» (is_active=FALSE): здесь удаляется
// сама строка бота. «Мёртвый» = отозванный токен (getMe → 401). Статусы
// обязаны совпадать со services/bot_api.py.
const BOT_STATUS_ALIVE = 'alive', BOT_STATUS_DEAD = 'dead', BOT_STATUS_UNKNOWN = 'unknown';

async function checkAllBots() {
  const btn = document.getElementById('botCheckBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Проверяю…'; }
  try {
    const r = await api('/api/miniapp/bots/check_all', {method:'POST', body:'{}'});
    BOT_CHECK = {};
    (r.bots||[]).forEach(b => { BOT_CHECK[b.bot_id] = b.status; });
    renderBots();
    toast(`🩺 Жив: ${r.alive||0} · отозван токен: ${r.dead||0} · не ответил: ${r.unknown||0}`);
  } catch(e) { toast('⚠️ '+errRu(e,'Не удалось проверить')); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '🩺 Проверить все'; } }
}

async function bulkDeleteBots() {
  const ids = [...BOT_SEL];
  if (!ids.length) { toast('❗ Выберите хотя бы одного бота'); return; }
  if (!await askConfirm(`Удалить ПОЛНОСТЬЮ ${ids.length} ${plural(ids.length,'бота','ботов','ботов')}?\n\nЭто не «убрать из работы»: боты и их данные удаляются из системы без возврата.`)) return;
  try {
    const r = await api('/api/miniapp/bots/bulk_delete', {method:'POST', body:JSON.stringify({bot_ids:ids})});
    toast(`🗑 Удалено: ${r.deleted||0} из ${r.requested||ids.length}`);
    toggleBotSelMode();
    await loadBots();
  } catch(e) { toast('⚠️ '+errRu(e,'Не удалось удалить')); }
}

async function cleanupDeadBots() {
  if (!await askConfirm('Проверить всех ботов и удалить тех, у кого отозван токен?\n\nБоты, до которых Telegram сейчас не достучался, затронуты не будут.')) return;
  const btn = document.getElementById('botCleanupBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Проверяю…'; }
  try {
    const r = await api('/api/miniapp/bots/cleanup_dead', {method:'POST', body:'{}'});
    toast(`🧹 Проверено: ${r.checked||0} · удалено мёртвых: ${r.deleted||0} · не ответили: ${r.unknown||0}`);
    BOT_CHECK = {};
    await loadBots();
  } catch(e) { toast('⚠️ '+errRu(e,'Не удалось очистить')); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '🧹 Удалить мёртвых'; } }
}

function openBotBroadcast() {
  if (!BOT_SEL.size) { toast('❗ Выберите хотя бы одного бота'); return; }
  document.getElementById('bbText').value='';
  { const _b=document.getElementById('bbButtons'); if(_b) _b.value=''; }
  document.getElementById('bbErr').textContent='';
  document.getElementById('bbHint').textContent = `Сообщение получат подписчики ${BOT_SEL.size} ${plural(BOT_SEL.size,'выбранному боту','выбранным ботам','выбранным ботам')}.`;
  showModal('mo-bot-broadcast');
}

async function submitBotBroadcast() {
  const errEl=document.getElementById('bbErr'); errEl.textContent='';
  const text=document.getElementById('bbText').value.trim();
  if(!text){ errEl.textContent='Введите текст рассылки'; return; }
  const buttons = parseInlineButtons(document.getElementById('bbButtons')?.value||'');
  const preview = `Отправить рассылку по ${BOT_SEL.size} ${plural(BOT_SEL.size,'боту','ботам','ботам')}?\n\n«${text.slice(0,160)}${text.length>160?'…':''}»`
    + (buttons.length?`\n\n🔘 Кнопок: ${buttons.length}`:'');
  if (!await askConfirm(preview)) return;
  const btn=document.getElementById('bbBtn'); btn.disabled=true; btn.textContent='Запускаю…';
  try {
    const _p={text, bot_ids:[...BOT_SEL]}; if(buttons.length) _p.buttons=buttons;
    const r=await api('/api/miniapp/network_broadcast',{method:'POST',body:JSON.stringify(_p)});
    toast(`📢 Рассылка запущена: ${r.broadcasts_created||BOT_SEL.size} ботов, ${r.total_recipients||0} получателей`);
    closeModal('mo-bot-broadcast'); toggleBotSelMode();
  } catch(e){ errEl.textContent=errRu(e,'Ошибка'); }
  finally { btn.disabled=false; btn.textContent='📢 Отправить'; }
}

// ── Массовое изменение профиля (имя/описание/приветствие) ──────────────────
function openBotProfileEdit() {
  if (!BOT_SEL.size) { toast('❗ Выберите хотя бы одного бота'); return; }
  ['bpName','bpDesc','bpShort','bpGreet'].forEach(id=>{ const e=document.getElementById(id); if(e) e.value=''; });
  document.getElementById('bpErr').textContent='';
  document.getElementById('bpHint').textContent = `Изменения применятся к ${BOT_SEL.size} ${plural(BOT_SEL.size,'выбранному боту','выбранным ботам','выбранным ботам')}.`;
  showModal('mo-bot-profile');
}

async function submitBotProfileEdit() {
  const errEl=document.getElementById('bpErr'); errEl.textContent='';
  const name=document.getElementById('bpName').value.trim();
  const description=document.getElementById('bpDesc').value.trim();
  const short_description=document.getElementById('bpShort').value.trim();
  const greeting=document.getElementById('bpGreet').value.trim();
  if(!name && !description && !short_description && !greeting){
    errEl.textContent='Заполните хотя бы одно поле'; return;
  }
  const _p={bot_ids:[...BOT_SEL]};
  if(name) _p.name=name;
  if(description) _p.description=description;
  if(short_description) _p.short_description=short_description;
  if(greeting) _p.greeting=greeting;
  const btn=document.getElementById('bpBtn'); btn.disabled=true;
  // 1) Предпросмотр «было → станет» ДО применения.
  let pv;
  btn.textContent='Готовлю предпросмотр…';
  try { pv=await api('/api/miniapp/bots/profile_preview',{method:'POST',body:JSON.stringify(_p)}); }
  catch(e){ errEl.textContent=errRu(e,'Не удалось получить предпросмотр'); btn.disabled=false; btn.textContent='Применить к выбранным'; return; }
  btn.disabled=false; btn.textContent='Применить к выбранным';
  const _fl=(s,m)=>{ s=String(s==null?'':s).replace(/\n/g,' '); return s.length>m?s.slice(0,m)+'…':(s||'—'); };
  const _lab={name:'имя',description:'описание',short_description:'короткое описание',greeting:'приветствие'};
  const _keys=Object.keys(pv.after||{});
  let txt=`Изменить у ${pv.total} ${plural(pv.total,'бота','ботов','ботов')}.\n\nСтанет:\n`;
  _keys.forEach(k=>{ txt+=`• ${_lab[k]}: «${_fl(pv.after[k],60)}»\n`; });
  if(pv.targets && pv.targets.length){
    txt+=`\nПримеры (было → станет):\n`;
    pv.targets.forEach(t=>{
      const ch=_keys.map(k=>`${_lab[k]}: «${_fl((t.before||{})[k],28)}» → «${_fl(pv.after[k],28)}»`).join('; ');
      txt+=`• ${_fl(t.title,22)}: ${ch}\n`;
    });
    if(pv.total>pv.shown) txt+=`…и ещё ${pv.total-pv.shown} ${plural(pv.total-pv.shown,'бот','бота','ботов')} (изменятся так же).\n`;
  }
  txt+=`\nПрименить? Это можно будет откатить («↩️ Откатить профиль» в итоге операции).`;
  if(!await askConfirm(txt)) return;
  // 2) Применение.
  btn.disabled=true; btn.textContent='Запускаю…';
  try {
    const r=await api('/api/miniapp/bots/bulk_profile',{method:'POST',body:JSON.stringify(_p)});
    toast(`✏️ Изменение профиля запущено: ${r.count||pv.total} ${plural(r.count||pv.total,'бот','бота','ботов')}`);
    closeModal('mo-bot-profile'); toggleBotSelMode();
  } catch(e){ errEl.textContent=errRu(e,'Ошибка'); }
  finally { btn.disabled=false; btn.textContent='Применить к выбранным'; }
}

// Откат массового изменения профиля — «вернуть как было». Вызывается кнопкой
// «↩️ Откатить профиль» в быстрых действиях по итогу операции (op_id применения).
async function rollbackBulkProfile(opId) {
  if(!await askConfirm('Вернуть прежние имя/описание/приветствие у изменённых ботов?')) return;
  try {
    const r=await api('/api/miniapp/bots/bulk_profile_rollback',{method:'POST',body:JSON.stringify({op_id:opId})});
    toast(`↩️ Откат запущен: ${r.count||''} ${plural(r.count||0,'бот','бота','ботов')}`);
  } catch(e){ toast('⚠️ '+errRu(e,'Не удалось откатить')); }
}

async function submitQuickBot() {
  const token=document.getElementById('quickBotToken').value.trim();
  const errEl=document.getElementById('quickBotErr'); errEl.textContent='';
  if(!token){ errEl.textContent='Введите токен бота'; return; }
  try {
    const r=await api('/api/miniapp/bot/add',{method:'POST',body:JSON.stringify({token})});
    // Вставленный токен уже существующего бота — это замена отозванного.
    // Говорим об этом прямо и подчёркиваем, что аудитория цела: раньше в этом
    // месте отвечали «уже добавлен», токен не менялся, и бот молчал дальше.
    toast(!r.already_exists ? `✅ Бот @${r.username} добавлен!`
          : (r.token_replaced ? `🔑 Токен @${r.username} заменён — подписчики и воронки сохранены`
                              : `✅ Бот @${r.username} уже добавлен`));
    document.getElementById('quickBotToken').value='';
    loadBots();
  } catch(e){ errEl.textContent=errRu(e,'Ошибка'); }
}

// Починка молчащего бота: ведём туда, где вставляется токен. Отдельной формы
// не заводим — «добавить бота» теперь и есть замена токена, и раздваивать
// путь значило бы держать две двери в одну комнату.
function openBotTokenFix(){
  back();
  const inp=document.getElementById('quickBotToken');
  if(inp){ inp.focus(); inp.scrollIntoView({behavior:'smooth', block:'center'}); }
  toast('🔑 Получите новый токен у @BotFather и вставьте его сюда');
}

// ── Bot Add Modal ──────────────────────────────────────
function openBotAddModal() {
  document.getElementById('botAddToken').value = '';
  document.getElementById('botAddErr').textContent = '';
  showModal('mo-bot-add');
}

async function submitBotAdd() {
  const token = document.getElementById('botAddToken').value.trim();
  const errEl = document.getElementById('botAddErr');
  const btn = document.getElementById('botAddBtn');
  errEl.textContent = '';
  if (!token) { errEl.textContent = 'Введите токен'; return; }
  btn.disabled = true; btn.textContent = 'Проверяем...';
  try {
    const r = await api('/api/miniapp/bot/add', {method:'POST', body:JSON.stringify({token})});
    const name = r.username ? '@'+r.username : r.first_name || '';
    toast(r.already_exists ? '✅ Бот '+name+' уже в вашем аккаунте' : '✅ Бот '+name+' добавлен!');
    closeModal('mo-bot-add');
    loadBots();
  } catch(e) { errEl.textContent = errRu(e); }
  finally { btn.disabled = false; btn.textContent = '➕ Добавить бота'; }
}
