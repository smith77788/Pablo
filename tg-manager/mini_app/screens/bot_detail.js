// Карточка бота и её подэкраны: включение/выключение, воронки, боты аккаунта,
// рассылка для бота, профиль и аватар, подписчики, заметка, команды,
// статистика и SEO-подсказки.
//
// Вынесено из index.html без изменения поведения. Список и фильтры —
// в bots_list.js, фабрика — в bot_factory.js, сетка — в bot_mesh.js.
//
// Обычный классический скрипт: глобальная область общая с index.html, грузится
// ПОСЛЕ основного блока, поэтому объявления глобальных переменных экрана
// остаются в index.html.

async function openBot(botId) {
  const b = BOTS.find(x=>x.bot_id==botId);
  const name = b?(b.username?'@'+b.username:(b.first_name||'Бот #'+botId)):'Бот';
  CUR_BOT = {id:botId, name};
  txt('bd-name', name);
  txt('bd-body', '<div class="spin-wrap"><div class="spin"></div></div>');
  push('s-botdetail');
  try {
    const d = await api('/api/miniapp/bot/'+botId);
    if (CUR_BOT && CUR_BOT.id==botId) CUR_BOT.d = d;
    txt('bd-body', buildBotDetail(d));
    modernizeIcons(document.getElementById('s-botdetail'));
  } catch(e) {
    txt('bd-body', errHtml(errRu(e)));
  }
}

// Пауза и удаление — РАЗНЫЕ действия. Пауза (⏸/▶️) обратима здесь же: бот
// остаётся в списке, перестаёт забирать обновления и не берётся в работу.
// Удаление (🗑) убирает бота из системы: из всех списков и из работы.
// Раньше «убрать» и «выключить» писали один флаг, поэтому удалить бота было
// нельзя вовсе — он всегда оставался в списке с кнопкой «Вернуть в работу».
async function botPowerConfirm(botId, name, isActive) {
  if (isActive) {
    if (!await askConfirm(`Поставить бота ${name} на паузу?\n\nОн перестанет забирать обновления и не будет браться в работу. Подписчики, воронки и история сохранятся — вернуть можно здесь же кнопкой «Вернуть в работу».`)) return;
  }
  try {
    await api('/api/miniapp/bot/'+botId+'/toggle', {method:'PUT', body:JSON.stringify({active:!isActive})});
    toast(isActive ? '⏸ Бот на паузе — данные сохранены' : '🟢 Бот вернулся в работу');
    loadBots();
    if (document.getElementById('s-botdetail')?.classList.contains('show')) openBot(botId);
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,60)); }
}

async function botDeleteConfirm(botId, name) {
  if (!await askConfirm(`Удалить бота ${name} из системы?\n\nОн пропадёт из всех списков и больше не будет браться в работу. Подписчики и история сохраняются в базе: чтобы вернуть бота, добавьте тот же токен заново.`)) return;
  try {
    await api('/api/miniapp/bot/'+botId, {method:'DELETE'});
    toast('🗑 Бот удалён из системы');
    if (document.getElementById('s-botdetail')?.classList.contains('show')) back();
    loadBots();
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,60)); }
}

function buildBotDetail(d) {
  const b = d.bot;
  const bname = b.username ? '@'+b.username : (b.first_name || 'Бот #'+b.bot_id);
  const initials = (b.first_name||bname).replace('@','').slice(0,2).toUpperCase();
  const grad = BOT_GRADS[b.bot_id % BOT_GRADS.length];
  const bid = b.bot_id;
  const bn = esc(CUR_BOT.name);

  // Почему бот молчит — и что с этим делать. Раньше карточка молчащего бота
  // выглядела как карточка рабочего: «🟢 Активен», подписчики пишут, ответа
  // нет, и объяснения нигде. Восстановление тоже не подсказывалось, а
  // единственный доступный путь (удалить и добавить заново) стирал аудиторию.
  const bh = b.health || {};
  const bhBanner = (bh.state && bh.state!=='ok' && bh.hint)
    ? `<div style="margin:8px 0;padding:11px 13px;background:var(--bg2);border-left:3px solid ${bh.attention?'var(--red,#ef4444)':'var(--orange,#f59e0b)'};border-radius:8px">
         <div style="font-weight:600;font-size:14px">${esc(bh.label)}</div>
         <div style="font-size:12px;color:var(--hint);margin-top:5px;line-height:1.45">${esc(bh.hint)}</div>
         ${bh.kind==='unauthorized'?`<button class="btn btn-p" style="width:100%;margin-top:9px;font-size:13px;padding:9px" onclick="openBotTokenFix()">🔑 Вставить новый токен</button>`:''}
       </div>`
    : '';

  // Management sections as tile grids
  const mgmtSections = [
    { title:'Рассылки и воронки', tiles:[
      {ico:'📢',lbl:'Рассылка',       fn:`openBcastForBot(${bid},'${bn}',${d.active_subscribers})`},
      {ico:'🔄',lbl:'Воронки',        fn:`openBotFunnels(${bid},'${bn}')`},
      {ico:'💬',lbl:'Авто-ответы',    fn:`openArScreen()`},
      {ico:'🧑‍💼',lbl:'Менеджер',      fn:`openSalesManager(${bid})`},
      {ico:'⏰',lbl:'Расписание',     fn:`openSchedule(${bid},'${bn}')`},
      {ico:'🔀',lbl:'Авто-воронки',  fn:`openAutoFunnels()`},
      {ico:'🎯',lbl:'Аудитория',      fn:`openAudience(${bid},'${bn}')`},
      {ico:'🛡',lbl:'Защита от накрутки', fn:`openFloodGuard(${bid})`},
    ]},
    { title:'Аналитика', tiles:[
      {ico:'📊',lbl:'Статистика',     fn:`openBotStats(${bid},'${bn}')`},
      {ico:'👥',lbl:'Подписчики',     fn:`openBotSubs(${bid},'${bn}')`},
      {ico:'🔥',lbl:'Активность',     fn:`openEngagement(${bid})`},
      {ico:'🔍',lbl:'Позиции',        fn:`openKeywordsScreen()`},
      {ico:'🧬',lbl:'ДНК аудит.',     fn:`openAudienceDna()`},
      {ico:'🧠',lbl:'ИИ Память',      fn:`openAiMemory()`},
    ]},
    { title:'Инструменты', tiles:[
      {ico:'🌐',lbl:'Глобал. поиск',  fn:`openGlobalSearch()`},
      {ico:'💬',lbl:'Входящие',       fn:`openRelay(${bid},'${bn}')`},
      {ico:'🔗',lbl:'Диплинки',       fn:`openDeeplinks(${bid})`},
      {ico:'⌨️',lbl:'Команды',        fn:`openBotCommands(${bid})`},
      {ico:'🔗',lbl:'Вебхук',        fn:`openBotWebhook(${bid},'${bn}')`},
      {ico:'🌍',lbl:'Мультигео',      fn:`openMultigeo()`},
      {ico:'📝',lbl:'Заметка',        fn:`openBotNote(${bid})`},
      {ico:'🏷',lbl:'Доп. username',  fn:`openBotExtraUsername(${bid})`},
    ]},
  ];

  const bcasts = (d.recent_broadcasts||[]).length
    ? `<div class="bc-table">
        <div class="bc-head"><span>Рассылка</span><span style="text-align:center">✓</span><span style="text-align:center">✕</span><span>Статус</span></div>
        ${d.recent_broadcasts.slice(0,4).map(bc=>`<div class="bc-row">
          <div class="bc-cell">${esc((bc.message_text||'').substring(0,28))}${(bc.message_text||'').length>28?'…':''}</div>
          <div class="bc-num" style="color:var(--green)">${num(bc.sent_count)}</div>
          <div class="bc-num" style="color:var(--red)">${num(bc.failed_count)}</div>
          <div class="bc-cell"><span class="badge ${stb(bc.status)[0]}">${stb(bc.status)[1]}</span></div>
        </div>`).join('')}
      </div>`
    : `<div class="lst"><div style="padding:14px;font-size:14px;color:var(--hint)">Рассылок ещё не было</div></div>`;

  const subs = (d.recent_users||[]).length
    ? `<div class="lst">${d.recent_users.slice(0,5).map(u=>`<div class="li" style="cursor:default">
        <div class="ava ava-teal">👤</div>
        <div class="li-body">
          <div class="li-name">${esc(u.first_name||(u.username?'@'+u.username:'Пользователь '+u.user_id))}</div>
          <div class="li-sub">${u.is_active?'🟢 Активен':'🔴 Неактивен'} · ${ago(u.last_seen)}</div>
        </div>
      </div>`).join('')}</div>`
    : `<div class="lst"><div style="padding:14px;font-size:14px;color:var(--hint)">Подписчиков нет</div></div>`;

  return `
    <div class="bot-phdr">
      <div class="bot-phdr-ava" style="background:${grad}">${initials}</div>
      <div class="bot-phdr-body">
        <div class="bot-phdr-name">${esc(bname)}</div>
        ${b.extra_username?`<div class="bot-phdr-sub" title="${b.extra_username_verified?'Проверено: ведёт на этого бота':'Ещё не подтверждено'}">также @${esc(b.extra_username)}${b.extra_username_verified?' ✓':' · не проверен'}</div>`:''}
        <div class="bot-phdr-sub">${num(d.active_subscribers)} активных · ${num(d.total_subscribers)} всего</div>
        ${(bh.state&&bh.state!=='ok')?`<span class="badge b-gy" style="background:var(--bg2);color:${bh.attention?'var(--red,#ef4444)':'var(--orange,#f59e0b)'}">${esc(bh.label)}</span>`
          : (b.is_active?'<span class="badge b-gr">🟢 Активен</span>':'<span class="badge b-gy">Выключен</span>')}
      </div>
    </div>
    ${bhBanner}
    <div class="kpi-bar">
      <div class="kpi-col"><div class="kpi-val" style="color:var(--green)">${num(d.active_subscribers)}</div><div class="kpi-lbl">Активных</div></div>
      <div class="kpi-col"><div class="kpi-val">${num(d.total_subscribers)}</div><div class="kpi-lbl">Всего подп.</div></div>
      <div class="kpi-col"><div class="kpi-val" style="color:var(--orange)">${num(d.active_funnels)}</div><div class="kpi-lbl">Воронок</div></div>
      <div class="kpi-col"><div class="kpi-val" style="color:var(--blue)">${num(d.auto_replies)}</div><div class="kpi-lbl">Авто-отв.</div></div>
    </div>
    <div style="display:flex;gap:8px;padding:4px 0 4px">
      <button class="btn btn-s" style="flex:1 1 auto;font-size:13px;padding:10px 6px" onclick="botPowerConfirm(${bid},'${bn}',${!!b.is_active})">${b.is_active?'⏸ На паузу':'▶️ Вернуть в работу'}</button>
      <button class="btn btn-s" style="flex:1 1 auto;font-size:13px;padding:10px 6px" onclick="openBotProfile(${bid})">⚙️ Профиль</button>
      <button class="btn btn-s" style="flex:0 0 auto;font-size:13px;padding:10px 10px;color:var(--red)" onclick="botDeleteConfirm(${bid},'${bn}')" aria-label="Удалить бота">🗑 Удалить</button>
    </div>
    ${mgmtSections.map(s=>`
      <div class="sec">${s.title}</div>
      <div class="mgmt-grid">
        ${s.tiles.map(t=>`<div class="mgmt-tile" onclick="${t.fn}">
          <div class="mgmt-tile-ico">${t.ico}</div>
          <div class="mgmt-tile-lbl">${t.lbl}</div>
        </div>`).join('')}
      </div>`).join('')}
    <div class="sec" style="margin-top:8px">Последние рассылки</div>
    ${bcasts}
    <div class="sec" style="margin-top:8px">Последние подписчики</div>
    ${subs}
    <div style="height:16px"></div>`;
}

// ── Bot Funnels ────────────────────────────────────────
async function openBotFunnels(botId, botName) {
  txt('bf-name', 'Воронки · '+botName);
  txt('bfList', '<div class="spin-wrap"><div class="spin"></div></div>');
  push('s-botfunnels');
  try {
    const d = await api('/api/miniapp/bot/'+botId+'/funnels');
    const fns = d.funnels||[];
    if (!fns.length) { txt('bfList', empty('🔄','Нет воронок','Воронка ведёт подписчика по шагам автоматически',
      {label:'➕ Создать воронку', fn:'openFunnelModal()'})); return; }
    txt('bfList', fns.map(f=>`<div class="li tap" onclick="openFunnelDetail(${f.id},'${esc(f.name||'Воронка')}')">
      <div class="ava ava-orange">🔄</div>
      <div class="li-body">
        <div class="li-name">${esc(f.name||'Воронка')}</div>
        <div class="li-sub">Активных: ${num(f.active_subs||0)} · Завершено: ${num(f.total_subs||0)}</div>
      </div>
      <div class="li-right">
        <div class="toggle ${f.is_active?'on':'off'}" onclick="event.stopPropagation();toggleFunnel(${f.id},this)"></div>
        <span class="chev">›</span>
      </div>
    </div>`).join(''));
  } catch(e) { txt('bfList', errHtml(errRu(e))); }
}

// Боты, СОЗДАННЫЕ через этот аккаунт (managed_bots.acc_id). Боты, добавленные по
// токену вручную, к аккаунту не привязаны и сюда не попадают — только в общий
// раздел. Выбор → карточка бота (openBot) с управлением.
async function openAccountBots(accId) {
  ACC_CONSOLE_ID = accId;
  push('s-accbots');
  txt('accbotList', '<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const d = await api('/api/miniapp/account/'+accId+'/bots');
    const list = d.bots || [];
    if (!list.length) {
      txt('accbotList', empty('🤖','У аккаунта нет ботов',
        'Боты, созданные через этот аккаунт в фабрике, появятся здесь. Добавленные по токену вручную — в общем разделе «Боты».'));
      return;
    }
    txt('accbotList', list.map(b => {
      const name = esc(b.first_name || (b.username ? '@'+b.username : 'Бот #'+b.bot_id));
      const sub = [b.username?'@'+esc(b.username):'', b.is_active?'🟢 активен':'⚪ выключен'].filter(Boolean).join(' · ');
      return `<div class="li tap" onclick="openBot(${b.bot_id})">
        <div class="row-ico" style="background:var(--bg-green-16)">🤖</div>
        <div class="li-body"><div class="li-name">${name}</div><div class="li-sub">${sub}</div></div>
        <div class="li-right"><span class="chev">›</span></div></div>`;
    // Потолок выборки — 500; если ботов больше, список обрывается молча —
    // сервер присылает честный total, и строка ниже сообщает об остатке.
    }).join('') + anCap(list.length, d.total, 'бот', 'бота', 'ботов'));
  } catch(e) { txt('accbotList', errHtml(errRu(e),'openAccountBots('+accId+')')); }
}

function openBcastForBot(botId, botName, recipCount) {
  if (!BOTS.length) BOTS = [{bot_id:botId, username:botName.replace('@',''), subscriber_count:recipCount}];
  fillBcastBots(botId);
  document.getElementById('bcastText').value='';
  { const _bb=document.getElementById('bcastButtons'); if(_bb) _bb.value=''; }
  { const _bs=document.getElementById('bcastSilent'); if(_bs) _bs.checked=false; }
  { const _sg=document.getElementById('bcastSegment'); if(_sg) _sg.value='all'; }
  { const _sc=document.getElementById('bcastSchedule'); if(_sc) _sc.value='0'; }
  document.getElementById('bcastLen').textContent='0';
  document.getElementById('bcastErr').style.display='none';
  document.getElementById('bcastBtn').disabled=false;
  document.getElementById('bcastBtn').textContent='📢 Отправить';
  updateBcastRecip();
  _restoreBcastForm();
  document.getElementById('bcastMbg').classList.add('show');
  tg.HapticFeedback?.impactOccurred('medium');
}

function fillBcastBots(selectedId) {
  const sel = document.getElementById('bcastBot');
  sel.innerHTML = BOTS.map(b=>{
    const name = b.username?'@'+b.username:(b.first_name||'Бот #'+b.bot_id);
    return `<option value="${b.bot_id}" data-subs="${b.subscriber_count||0}" ${b.bot_id==selectedId?'selected':''}>${esc(name)} (${num(b.subscriber_count||0)} подп.)</option>`;
  }).join('');
  sel.addEventListener('change', updateBcastRecip);
}

function openBotProfile(botId) {
  BP_BOT_ID = botId;
  ['bpoName','bpoDesc','bpoShort','bpoAvatar'].forEach(id=>{ const el=document.getElementById(id); if(el) el.value=''; });
  const e=document.getElementById('bpoErr'); if(e) e.textContent='';
  showModal('mo-bot-profile-one');
}

async function setBotAvatar() {
  const url=document.getElementById('bpoAvatar').value.trim();
  const errEl=document.getElementById('bpoErr'); errEl.textContent='';
  if(!url){ errEl.textContent='Вставьте https URL картинки'; return; }
  toast('🖼 Загружаю аватар…');
  try {
    const r=await api('/api/miniapp/bot/'+BP_BOT_ID+'/avatar',{method:'POST',body:JSON.stringify({photo_url:url})});
    if(r.ok) toast('✅ Аватар обновлён'); else errEl.textContent='Не удалось поставить аватар';
  } catch(e){ errEl.textContent=errRu(e,'Ошибка'); }
}

async function deleteBotAvatar() {
  if(!await askConfirm('Удалить аватар бота?')) return;
  try {
    const r=await api('/api/miniapp/bot/'+BP_BOT_ID+'/avatar',{method:'DELETE'});
    if(r.ok) toast('✅ Аватар удалён');
  } catch(e){ toast('⚠️ '+errRu(e,'Ошибка')); }
}

async function submitBotProfile() {
  const errEl=document.getElementById('bpoErr'); errEl.textContent='';
  const name=document.getElementById('bpoName').value.trim();
  const description=document.getElementById('bpoDesc').value.trim();
  const short_description=document.getElementById('bpoShort').value.trim();
  if(!name && !description && !short_description){ errEl.textContent='Заполните хотя бы одно поле'; return; }
  const btn=document.getElementById('bpoBtn'); btn.disabled=true; btn.textContent='Сохраняю…';
  try {
    const payload={}; if(name)payload.name=name; if(description)payload.description=description; if(short_description)payload.short_description=short_description;
    const r=await api('/api/miniapp/bot/'+BP_BOT_ID+'/profile',{method:'POST',body:JSON.stringify(payload)});
    if(r.ok){ toast('✅ Профиль бота обновлён'); closeModal('mo-bot-profile-one'); }
    else { errEl.textContent='Telegram отклонил изменение (проверьте текст/лимиты)'; }
  } catch(e){ errEl.textContent=errRu(e,'Ошибка'); }
  finally { btn.disabled=false; btn.textContent='Сохранить'; }
}

async function openBotSubs(botId, botName) {
  BS_BOT_ID = botId;
  BS_OFFSET = 0;
  txt('bs-name', 'Подписчики · '+botName);
  txt('bs-cnt', '');
  txt('bsList', '<div class="spin-wrap"><div class="spin"></div></div>');
  document.getElementById('bs-more').style.display='none';
  push('s-botsubs');
  await loadBotSubs(false);
}

async function loadBotSubs(append) {
  try {
    const d = await api('/api/miniapp/bot/'+BS_BOT_ID+'/subscribers?offset='+BS_OFFSET);
    const subs = d.subscribers||[];
    BS_TOTAL = d.total||0;
    txt('bs-cnt', BS_TOTAL+' подп.');
    if (!subs.length && !append) {
      txt('bsList', empty('👥','Нет подписчиков','Бот ещё не получил подписчиков'));
      return;
    }
    const html = subs.map(u=>{
      const name = u.first_name||(u.username?'@'+u.username:'Пользователь '+u.user_id);
      return `<div class="li" style="cursor:default">
        <div class="ava ava-teal">👤</div>
        <div class="li-body">
          <div class="li-name">${esc(name)}${u.username?' <span style="color:var(--hint)">@'+esc(u.username)+'</span>':''}</div>
          <div class="li-sub">${u.is_active?'🟢 Активен':'🔴 Неактивен'} · ${ago(u.last_seen)}</div>
        </div>
        <span class="badge ${u.is_active?'b-gr':'b-gy'}">#${u.user_id}</span>
      </div>`;
    }).join('');
    if (append) {
      document.getElementById('bsList').insertAdjacentHTML('beforeend', html);
    } else {
      txt('bsList', html);
    }
    BS_OFFSET += subs.length;
    const moreEl = document.getElementById('bs-more');
    moreEl.style.display = (BS_OFFSET < BS_TOTAL) ? 'block' : 'none';
  } catch(e) {
    if (!append) txt('bsList', errHtml(errRu(e)));
    else toast('⚠️ '+errRu(e,'Ошибка').substring(0,60));
  }
}

function updateCmpTargetBot() {
  const type = document.getElementById('cmpTargetType').value;
  const bf = document.getElementById('cmpBotField');
  const cf = document.getElementById('cmpCohortField');
  const imf = document.getElementById('cmpImportField');
  const sf = document.getElementById('cmpSegmentField');
  const needBot = (type==='bot_users' || type==='cohort');
  bf.style.display = needBot ? 'block' : 'none';
  if (cf) cf.style.display = (type==='cohort') ? 'block' : 'none';
  if (imf) imf.style.display = (type==='import_list') ? 'block' : 'none';
  if (sf) {
    sf.style.display = (type==='segment') ? 'block' : 'none';
    if (type==='segment') loadCmpSegments();
  }
  if (needBot) {
    document.getElementById('cmpBot').innerHTML = BOTS.length ? BOTS.map(b=>{
      const name = b.username?'@'+b.username:(b.first_name||'Бот #'+b.bot_id);
      return `<option value="${b.bot_id}">${esc(name)} (${num(b.subscriber_count||0)} подп.)</option>`;
    }).join('') : '<option value="">Нет ботов — добавьте бота</option>';
  }
}

// Дополнительный @username (…app/ai/agent/game). Ставится у @BotFather — у
// Bot API метода нет, — здесь только привязка с проверкой на сервере.
const BOT_EXTRA_SUFFIXES = ['app','ai','agent','game'];
let BEU_BOT_ID = 0;
function _beuBase(v) {
  let u = (v||'').trim().replace(/^@/,'').replace(/^(https?:\/\/)?(t|telegram)\.me\//i,'');
  const low = u.toLowerCase();
  for (const suf of ['agent','game','app','bot','ai']) {
    if (low.endsWith(suf) && low.length > suf.length) return u.slice(0, -suf.length);
  }
  return u;
}
function _beuChips() {
  const base = _beuBase(document.getElementById('beuInput').value) || (CUR_BOT && CUR_BOT.name || '').replace('@','').replace(/_?bot$/i,'');
  document.getElementById('beuChips').innerHTML = BOT_EXTRA_SUFFIXES.map(s=>
    `<button class="btn" style="padding:5px 10px;font-size:12px" onclick="document.getElementById('beuInput').value='${esc(base)}'+'${s}';_beuChips()">…${s}</button>`).join('');
}
function openBotExtraUsername(botId) {
  BEU_BOT_ID = botId;
  const b = (CUR_BOT && CUR_BOT.d && CUR_BOT.d.bot) || {};
  const inp = document.getElementById('beuInput');
  inp.value = b.extra_username || '';
  inp.oninput = _beuChips;
  document.getElementById('beuErr').textContent = '';
  document.getElementById('beuClear').style.display = b.extra_username ? '' : 'none';
  _beuChips();
  showModal('mo-bot-extra-un');
}
async function saveBotExtraUsername(clear) {
  const inp = document.getElementById('beuInput');
  const err = document.getElementById('beuErr');
  const btn = document.getElementById(clear ? 'beuClear' : 'beuBtn');
  const username = clear ? '' : inp.value.trim().replace(/^@/,'');
  err.textContent = '';
  if (!clear) {
    const low = username.toLowerCase();
    if (!/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(username)) { err.textContent = 'Латиница, цифры и «_», 5–32 символа, начинается с буквы'; return; }
    if (low.endsWith('bot')) { err.textContent = 'Это основной формат «…bot». Дополнительный оканчивается на app, ai, agent или game'; return; }
    if (!['app','ai','agent','game','gay'].some(s=>low.endsWith(s))) { err.textContent = 'Должен оканчиваться на app, ai, agent или game'; return; }
  }
  btn.disabled = true;
  try {
    const r = await api('/api/miniapp/bot/'+BEU_BOT_ID+'/extra_username', {method:'PUT', body:JSON.stringify({username})});
    const d = CUR_BOT && CUR_BOT.id==BEU_BOT_ID && CUR_BOT.d;
    if (d) {
      d.bot.extra_username = r.extra_username; d.bot.extra_username_verified = r.verified;
      txt('bd-body', buildBotDetail(d));
      modernizeIcons(document.getElementById('s-botdetail'));
    }
    closeModal('mo-bot-extra-un');
    toast(!r.extra_username ? '🗑 Дополнительный username отвязан'
      : (r.verified ? '✅ @'+r.extra_username+' привязан и проверен' : '⚠️ @'+r.extra_username+' сохранён, но не проверен: '+(r.hint||'повторим позже')));
  } catch(e) { err.textContent = errRu(e, 'Не удалось сохранить'); }
  finally { btn.disabled = false; }
}

async function openBotNote(botId) {
  push('s-botnote');
  _botNoteLoad(botId);
}

async function saveBotNote() {
  const ta = document.getElementById('noteText');
  const errEl = document.getElementById('noteErr');
  // Сохранять то, чего не видели, нельзя: прошлая заметка ушла бы под запись.
  if (!ta.dataset.loaded) {
    errEl.textContent = 'Заметка ещё не загрузилась — сохранение затёрло бы прошлую.';
    return;
  }
  const note = ta.value.trim();
  errEl.textContent='';
  try {
    await api('/api/miniapp/bot/'+NOTE_BOT_ID+'/note', {method:'PUT', body:JSON.stringify({note})});
    toast(note ? '✅ Заметка сохранена' : '🗑 Заметка удалена');
    back();
  } catch(e) { errEl.textContent=errRu(e,'Ошибка'); }
}

async function openBotCommands(botId) {
  CMD_BOT_ID = botId;
  push('s-botcommands');
  loadBotCommands();
}

async function loadBotCommands() {
  txt('cmdList', '<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const d = await api('/api/miniapp/bot/'+CMD_BOT_ID+'/commands');
    CMD_LIST = d.commands||[];
    if (!CMD_LIST.length) { txt('cmdList', empty('⌨️','Нет команд','Добавьте первую команду')); return; }
    txt('cmdList', CMD_LIST.map((c,i)=>`
      <div class="row">
        <div class="row-body">
          <div class="row-name">/${esc(c.command)}</div>
          <div class="row-val">${esc(c.description)}</div>
        </div>
        <div class="row-right"><button class="btn btn-d" style="padding:5px 10px;min-width:0;font-size:12px" onclick="deleteCmd(${i})" aria-label="Удалить">✕</button></div>
      </div>
    `).join(''));
  } catch(e) { txt('cmdList', empty('⚠️','Ошибка',errRu(e))); }
}

async function clearBotCommands() {
  if (!await askConfirm('Удалить все команды бота?')) return;
  try {
    await api('/api/miniapp/bot/'+CMD_BOT_ID+'/commands', {method:'PUT', body:JSON.stringify({commands:[]})});
    toast('🗑 Все команды удалены');
    loadBotCommands();
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,60)); }
}

async function openBotStats(botId, botName) {
  BSTATS_BOT_ID = botId;
  BSTATS_BOT_NAME = botName || '';
  txt('bstats-title', '📊 ' + (botName || 'Статистика бота'));
  push('s-botstats');
  txt('bstatsGrid', '<div class="spin-wrap" style="grid-column:1/-1"><div class="spin"></div></div>');
  txt('bstatsChart', '');
  txt('bstatsDetails', '');
  try {
    const d = await api('/api/miniapp/bot/'+botId+'/stats');
    BSTATS_SUBS = d.aud_total||0;
    // Четырнадцать чисел, из которых не нажималось ни одно: «Воронок 3» — а
    // каких, «Рассылок 12» — а чем кончились. Каждое число ведёт туда, где
    // стоит то, что оно посчитало.
    const kpis = [
      {v:num(d.aud_total),l:'Аудитория',go:'bstatsGo("subs")'},
      {v:'+'+num(d.aud_today),l:'Новых за 24ч',go:'bstatsGo("subs")'},
      {v:num(d.broadcasts_total),l:'Рассылок',go:'bstatsGo("bcast")'},
      {v:num(d.broadcasts_sent),l:'Отправлено',go:'bstatsGo("bcast")'},
      {v:num(d.active_replies),l:'Авто-ответов',go:'bstatsGo("ar")'},
      {v:num(d.active_funnels),l:'Воронок',go:'bstatsGo("funnels")'},
      {v:num(d.relay_sessions),l:'Диалогов',go:'bstatsGo("relay")'},
      {v:num(d.funnel_users),l:'В воронках',go:'bstatsGo("funnels")'},
    ];
    txt('bstatsGrid', kpis.map(k=>`<div class="card" style="text-align:center;cursor:pointer" onclick='${k.go}' role="button" tabindex="0"><div style="font-size:22px;font-weight:700">${k.v}</div><div style="font-size:11px;color:var(--hint);margin-top:2px">${k.l} ›</div></div>`).join(''));
    // Draw simple bar chart for daily growth
    const daily = d.daily_growth||[];
    if (daily.length) {
      const maxC = Math.max(...daily.map(d2=>d2.count),1);
      const bars = daily.map(d2=>{
        const h = Math.max(4, Math.round(d2.count/maxC*60));
        // ММ-ДД читалось как «10-05»: в русской записи это пятое октября,
        // а выглядело как десятое мая. Переворачиваем в ДД.ММ.
        const date = d2.date.slice(8,10) + '.' + d2.date.slice(5,7);
        return `<div style="display:flex;flex-direction:column;align-items:center;gap:3px;flex:1">
          <div style="font-size:10px;color:var(--hint)">${num(d2.count)}</div>
          <div style="width:100%;height:${h}px;background:#7c3aed;border-radius:3px"></div>
          <div style="font-size:10px;color:var(--hint)">${date}</div>
        </div>`;
      }).join('');
      txt('bstatsChart', `<div style="display:flex;align-items:flex-end;gap:4px;height:80px">${bars}</div>`);
    } else {
      txt('bstatsChart', '<div class="empty-inline">Пока не из чего строить график: нужны хотя бы сутки с новыми подписчиками</div>');
    }
    const compl = d.funnel_total_subs ? Math.round(d.funnel_completed/d.funnel_total_subs*100) : 0;
    const dRow = (name, val, go) => `<div class="li tap" onclick='${go}' role="button" tabindex="0">
      <div class="li-body"><div class="li-name">${name}</div></div>
      <div class="li-right">${val}</div><span class="chev">›</span></div>`;
    txt('bstatsDetails',
      dRow('Входящих сообщений', num(d.msg_in), 'bstatsGo("relay")')
      + dRow('Исходящих сообщений', num(d.msg_out), 'bstatsGo("relay")')
      + dRow('Новых диалогов за 24ч', num(d.relay_today), 'bstatsGo("relay")')
      + dRow('Завершили воронку', num(d.funnel_completed)+' ('+compl+'%)', 'bstatsGo("funnels")')
      + dRow('Покинули воронку', num(d.funnel_dropped||0), 'bstatsGo("funnels")')
      + dRow('Рост за 7 дней', '+'+num(d.aud_week), 'bstatsGo("subs")'));
  } catch(e) {
    txt('bstatsChart', ''); txt('bstatsDetails', '');
    txt('bstatsGrid', `<div style="grid-column:1/-1">${errHtml(errRu(e), `openBotStats(${Number(botId)}, ${JSON.stringify(String(botName||''))})`)}</div>`);
  }
}

// Применить рекомендацию по переоптимизации бота (имя/краткое описание) в один клик
async function applyBotSeo(botId, btn) {
  if (!await askConfirm('Применить рекомендацию (имя/краткое описание) к боту?')) return;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await api('/api/miniapp/seo/apply_bot', {method:'POST', body:JSON.stringify({bot_id:botId})});
    const parts = r.applied||[];
    const errs = (r.errors||[]).join('; ');
    toast(parts.length ? ('✅ Применено: '+parts.join(', ')+(errs?(' · ошибки: '+errs):'')) : ('⚠️ '+(errs||'без изменений')));
    openSeo();
  } catch(e) {
    toast('⚠️ '+errRu(e,'Ошибка'));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Применить'; }
  }
}
