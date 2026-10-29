// Экран аккаунтов: список, карточка, действия и выделение.
//
// Вынесено из инлайн-скрипта mini_app/index.html (29744 строки, из них 22275
// в одном <script>; CLAUDE.md держит это как техдолг и просит выносить
// экраны в mini_app/screens/*.js). Поведение при переносе не менялось: тела
// функций перенесены дословно вместе с комментариями.
//
// Что здесь:
//   * список — загрузка, перерисовка, догрузка страниц, обновление;
//   * карточка аккаунта и действия над ним: проверка, переавторизация,
//     включение/выключение, удаление;
//   * выделение — всё на странице, снять, вся отфильтрованная выборка;
//   * массовое: чистка подтверждённо мёртвых, выгрузка метаданных в JSON
//     (без секретов — их отдаёт ручка уже без session_str);
//   * баннеры пула и гео и две русские расшифровки для карточки: accRu
//     (состояние аккаунта) и accReasonHuman (почему пульс держит паузу).
//
// Файл подключается <script src> в конце index.html, то есть ПОСЛЕ
// инлайн-скрипта. Внутри только объявления функций (они поднимаются), а
// общие помощники — api, toast, esc, errRu, ago, empty, anCap — вызываются
// в момент клика или загрузки экрана, когда оба скрипта уже отработали.

async function accAction(id, act, confirmMsg) {
  if (confirmMsg && !await askConfirm(confirmMsg)) return;
  try {
    const r = await api(`/api/miniapp/account/${id}/action/${act}`, {method:'POST'});
    toast(`✅ Поставлено в очередь (#${r.op_id})`);
    if(r.op_id) pollOpResult(r.op_id, `Действие: ${act}`);
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка')); }
}

async function checkAccount(id) {
  try {
    const r = await api(`/api/miniapp/account/${id}/check`, {method:'POST'});
    toast(`🔍 Проверка запущена (#${r.op_id}).`);
    if(r.op_id) pollOpResult(r.op_id, 'Проверка аккаунта');
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка')); }
}

async function toggleAccount(id) {
  try {
    const r = await api(`/api/miniapp/account/${id}/toggle`, {method:'POST'});
    toast(r.is_active ? '▶️ Аккаунт включён' : '⏸ Аккаунт выключен');
    openAccount(id);  // обновить экран
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка')); }
}

async function deleteAccount(id, _el) {
  if (!await confirmDelete('Удалить аккаунт', _el, 'Действие необратимо.')) return;
  try {
    await api(`/api/miniapp/account/${id}`, {method:'DELETE'});
    toast('🗑 Аккаунт удалён');
    back(); loadAccounts();
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка')); }
}

// ── Re-authorization ──────────────────────────────────────
async function reauthAccount(id) {
  try {
    const r = await api(`/api/miniapp/account/${id}/action/reauth`, {method:'POST'});
    // Переавторизация интерактивна (код из Telegram) → показываем инструкцию, ведущую в бота.
    await askConfirm(errRu(r,'Переавторизация выполняется в боте: Аккаунты → 🔄 Переавторизатор → Релог.'));
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка').substring(0,80)); }
}

async function loadAccounts() {
  // Accounts and channels load independently — one failure doesn't break the other
  api(_accQuery(0)).then(ad => {
    CUR_ACCS = ad.accounts||[];
    ACC_HAS_MORE = !!(ad.page && ad.page.has_more);
    if (ad.page && ad.page.filtered_total!=null) ACC_FILTERED_TOTAL = ad.page.filtered_total;
    ACC_OFFSET = CUR_ACCS.length;
    // Истинное число из серверной агрегации (страница ограничена LIMIT)
    const cnt = (ad.stats && ad.stats.total!=null) ? ad.stats.total : CUR_ACCS.length;
    document.getElementById('acc-cnt').textContent = cnt ? cnt+' аккаунтов' : '';
    txt('m-acc-cnt', cnt+' аккаунтов');
    updateAccKpi(CUR_ACCS, ad.stats);
    renderStageChips(ad.stats && ad.stats.by_stage);
    renderAccounts(CUR_ACCS);
    renderLoadMore(ad.page);
  }).catch(e => {
    document.getElementById('accList').innerHTML=errHtml(errRu(e),'loadAccounts()');
  });
  // Превью на вкладке аккаунтов показывает ТРИ канала. Раньше оно тянуло срез в
  // 500 строк и затирало им CUR_CHANNELS — тот самый список, который экран
  // каналов подгрузил постранично. После переключения вкладок «Выбрать все» на
  // экране каналов работал уже по другому набору, чем тот, что видел
  // пользователь. Теперь превью берёт ровно столько, сколько показывает, и
  // только ДОПОЛНЯЕТ кэш, никогда его не укорачивая.
  api('/api/miniapp/channels?limit=20&offset=0').then(cd => {
    const list = cd.channels||[];
    const chScreenOpen = (typeof STACK !== 'undefined' && STACK.indexOf('s-channels') !== -1);
    if (!chScreenOpen) {
      const known = new Set(CUR_CHANNELS.map(c=>String(c.channel_id)));
      list.forEach(c => { if (!known.has(String(c.channel_id))) CUR_CHANNELS.push(c); });
    }
    // Истинное число каналов из cd.total (список ограничен) — иначе подпись врала
    txt('m-ch-cnt', (cd.total!=null?cd.total:list.length)+' каналов');
    renderChPreview(list, cd.total);
  }).catch(() => {
    txt('chPreview', '');
  });
}

// Перезагрузить срез с сервера (offset=0). Серверная фильтрация — срез считается
// по ВСЕЙ таблице, не по загруженным 100.
async function reloadAccounts() {
  try {
    const ad = await api(_accQuery(0));
    CUR_ACCS = ad.accounts||[];
    ACC_HAS_MORE = !!(ad.page && ad.page.has_more);
    if (ad.page && ad.page.filtered_total!=null) ACC_FILTERED_TOTAL = ad.page.filtered_total;
    ACC_OFFSET = CUR_ACCS.length;
    updateAccKpi(CUR_ACCS, ad.stats);
    renderStageChips(ad.stats && ad.stats.by_stage);
    renderAccounts(CUR_ACCS);
    renderLoadMore(ad.page);
    if (ACC_SEL_MODE) updateAccSelBar();
  } catch(e) {
    document.getElementById('accList').innerHTML=errHtml(errRu(e),'reloadAccounts()');
  }
}

async function loadMoreAccounts() {
  if (ACC_LOADING || !ACC_HAS_MORE) return;
  ACC_LOADING = true;
  const box=document.getElementById('accLoadMore');
  if(box) box.innerHTML='<div class="spin-wrap" style="padding:8px"><div class="spin"></div></div>';
  try {
    const ad = await api(_accQuery(ACC_OFFSET));
    const more = ad.accounts||[];
    CUR_ACCS = CUR_ACCS.concat(more);
    ACC_HAS_MORE = !!(ad.page && ad.page.has_more);
    if (ad.page && ad.page.filtered_total!=null) ACC_FILTERED_TOTAL = ad.page.filtered_total;
    ACC_OFFSET += more.length;
    renderAccounts(CUR_ACCS);
    renderLoadMore(ad.page);
  } catch(e){
    toast('⚠️ '+errRu(e,'Ошибка'));
    // Не оставляем застрявшую крутилку — возвращаем кнопку для повтора (сценарий
    // «загрузить ещё» доходит до результата: успех или явная возможность повторить).
    if(box) box.innerHTML='<button class="btn btn-s" onclick="loadMoreAccounts()" style="width:100%;font-size:13px;padding:10px">↺ Повторить загрузку</button>';
  }
  finally { ACC_LOADING=false; }
}

function renderAccounts(list) {
  const el = document.getElementById('accList');
  if (!list.length) {
    const filtered = ACC_SEARCH || ACC_STAGE_FILTER || ACC_POOL_FILTER || ACC_GEO_FILTER || (ACC_FILTER && ACC_FILTER!=='all');
    if (filtered) el.innerHTML=empty('🔍','Ничего не найдено','Измените фильтр, статус или строку поиска');
    else el.innerHTML=empty('📱','Нет аккаунтов','Добавьте аккаунт прямо здесь: по номеру, QR-коду, файлу .session, архиву tdata или строке сессии',{label:'➕ Добавить аккаунт',fn:'openAccImportModal()'});
    return;
  }
  el.innerHTML = list.map(a=>{
    const name = a.first_name||(a.username?'@'+a.username:'#'+a.id);
    const phone = a.phone || '';
    const flag = phoneFlag(phone);
    const cd = isOnCooldown(a);
    const trust = a.trust_score!=null?Math.round(a.trust_score):100;
    const trustColor = trust>=80?'var(--green)':trust>=50?'var(--orange)':'var(--red)';
    let status, cls, ava_cls;
    if (a.acc_status==='session_expired') { status='🔑 Релог'; cls='b-or'; ava_cls='ava-orange'; }
    else if (!a.is_active) { status='Выкл'; cls='b-gy'; ava_cls='ava-teal'; }
    else if (a.acc_status==='banned') { status='🚫 Бан'; cls='b-rd'; ava_cls='ava-red'; }
    else if (cd) { status='⏳ Кулдаун'; cls='b-or'; ava_cls='ava-orange'; }
    // Риск-пульс: аккаунт «активен и подключён», но иммунная система держит его
    // в карантине/риске и операции его пропускают → показываем РЕАЛЬНОЕ состояние,
    // а не ложное «Активен» (иначе «аккаунты не работают, хотя активны»).
    else if (a.health_status==='quarantine') { status='🛑 На паузе'; cls='b-rd'; ava_cls='ava-red'; }
    else if (a.health_status==='at_risk') { status='⚠️ Под риском'; cls='b-or'; ava_cls='ava-orange'; }
    // Мёртвый прокси: аккаунт цел, но каждая его операция падает по сети. Без
    // этого он показывался «Активен», и человек шёл чинить аккаунт вместо
    // замены прокси. Аккаунты БЕЗ прокси работают с адреса хоста — это норма.
    else if (a.has_proxy && a.proxy_alive === false) { status='🔌 Прокси'; cls='b-rd'; ava_cls='ava-red'; }
    else { status='Активен'; cls='b-gr'; ava_cls='ava-teal'; }
    const proxyNote = (a.has_proxy && a.proxy_alive === false)
      ? `<div class="acc-phone" style="color:var(--red)">🔌 прокси не отвечает${a.proxy_label?' · '+esc(a.proxy_label):''} — аккаунт цел, замените прокси</div>`
      : '';
    // «🛑 На паузе» без причины отправляло человека искать объяснение в
    // карточку аккаунта. Причину считает пульс — печатаем её здесь же.
    const pulseNote = (a.health_reason && (a.health_status==='quarantine' || a.health_status==='at_risk'))
      ? `<div class="acc-phone" style="color:var(--${a.health_status==='quarantine'?'red':'orange'})">${esc(a.health_reason)}</div>`
      : '';
    const sel = ACC_SEL.has(a.id);
    const onclick = ACC_SEL_MODE ? `toggleAccSel(${a.id})` : `openAccount(${a.id})`;
    const checkbox = ACC_SEL_MODE ? `<div class="acc-check ${sel?'on':''}">${sel?'✓':''}</div>` : '';
    const right = ACC_SEL_MODE ? '' : `<div class="acc-right">
        <span class="badge ${cls}">${status}</span>
        <span style="font-size:10px;color:var(--hint)">${trust}%</span>
        <span class="chev">›</span>
      </div>`;
    return `<div class="acc-row ${ACC_SEL_MODE&&sel?'sel':''}" onclick="${onclick}">
      ${checkbox}
      <div class="ava ${ava_cls}" style="font-size:15px">${flag}</div>
      <div class="acc-body">
        <div class="acc-name">${esc(name)} ${stageBadge(a.stage)}</div>
        <div class="acc-phone">${esc(phone)}</div>
        ${proxyNote}
        ${pulseNote}
        <div class="acc-trust"><div class="acc-trust-fill" style="width:${trust}%;background:${trustColor}"></div></div>
        ${ACC_SEL_MODE ? '' : _fixBtn(a.fix)}
      </div>
      ${right}
    </div>`;
  }).join('');
}

function accSelAll() {
  ACC_SEL_ALL_FILTERED = false;
  _accFiltered().forEach(a=>ACC_SEL.add(a.id));
  renderAccounts(_accFiltered());
  updateAccSelBar();
}

function accSelClear() {
  ACC_SEL.clear();
  ACC_SEL_ALL_FILTERED = false;
  renderAccounts(_accFiltered());
  updateAccSelBar();
}

// Выбрать ВЕСЬ срез под фильтром (серверно, снимает 100-лимит выделения)
function accSelWholeFilter() {
  ACC_SEL_ALL_FILTERED = !ACC_SEL_ALL_FILTERED;
  updateAccSelBar();
}

// Один тап: удалить ВСЕ невоскрешаемые аккаунты (бан/деактивация/отозванная сессия).
async function purgeDeadAccounts() {
  let cnt = 0;
  try {
    const st = await api('/api/miniapp/accounts?filter=dead&limit=1');
    cnt = (st.stats && st.stats.dead!=null) ? st.stats.dead : (st.filtered_total||0);
  } catch(e) {
    // Не удалось СПРОСИТЬ — значит мы не знаем ответа. Раньше здесь оставался
    // cnt=0, и приложение утверждало «Мёртвых аккаунтов нет 👍»: упавший запрос
    // выдавался за проверенный факт, и владелец переставал чистить парк.
    toast('⚠️ Не удалось проверить: ' + errRu(e,'ошибка связи'));
    return;
  }
  if (!cnt) { toast('✅ Мёртвых аккаунтов нет 👍'); return; }
  if (!await askConfirm(`🗑 Удалить ${cnt} невоскрешаемых аккаунтов (бан / деактивация / отозванная сессия)?\n\nБезвозвратно. Конфликтные (два IP, «Кулдаун») НЕ входят — их лечит перезалив.`)) return;
  try {
    const r = await api('/api/miniapp/accounts/mass', {method:'POST',
      body: JSON.stringify({op:'delete', select_all_filtered:true, filter:'dead'})});
    toast(`🗑 Удалено мёртвых аккаунтов: ${r.count||0}`);
    loadAccounts();
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка удаления')); }
}

// Экспорт метаданных аккаунтов в JSON (без секретов) — скачивание файла
async function exportAccountsJson() {
  try {
    const r = await api('/api/miniapp/accounts/export_json', {method:'GET'});
    const blob = new Blob([JSON.stringify(r.accounts||[], null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'accounts_'+(new Date().toISOString().slice(0,10))+'.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
    toast('📤 Экспортировано аккаунтов: '+(r.count||0));
  } catch(e) { toast('⚠️ '+errRu(e,'Ошибка экспорта')); }
}

// ── Account Detail ─────────────────────────────────────
async function openAccount(accId) {
  const a = CUR_ACCS.find(x=>x.id==accId);
  const name = a?(a.first_name||(a.username?'@'+a.username:a.phone||'Аккаунт #'+accId)):'Аккаунт';
  txt('acd-name', name);
  txt('acd-body', '<div class="spin-wrap"><div class="spin"></div></div>');
  push('s-accdetail');
  try {
    const d = await api('/api/miniapp/account/'+accId);
    window._accDetail = d.account || {};
    txt('acd-body', buildAccDetail(d));
  } catch(e) {
    txt('acd-body', errHtml(errRu(e)));
  }
}

// Машинная причина проблемы (status_reason от op_worker) → человекочитаемо + путь
// к фиксу. Отвечает на «почему аккаунт не работает», когда он вроде подключён.
function accReasonHuman(raw) {
  if (!raw) return '';
  const s = String(raw).toLowerCase();
  if (s.includes('proxy') || s.includes('network') || s.includes('connect') || s.includes('timeout'))
    return '🌐 Проблема с прокси или сетью — проверьте/смените прокси аккаунта.';
  if (s.includes('flood'))
    return '⏳ Слишком частые действия (FloodWait) — дайте аккаунту отдохнуть.';
  if (s.includes('auth') || s.includes('session') || s.includes('unregist') || s.includes('unauthorized'))
    return '🔑 Сессия недействительна — нужна переавторизация (в боте: Аккаунты → Переавторизатор).';
  if (s.includes('ban') || s.includes('block') || s.includes('restrict') || s.includes('spam'))
    return '🚫 Аккаунт ограничен Telegram — снизьте активность, дайте отлежаться.';
  return 'ℹ️ ' + raw;
}

// Срез по пулу приходит с «Инфраструктуры», и на вкладке аккаунтов его ничто
// не показывает: список просто оказывался короче без объяснения. Баннер
// называет пул и даёт его снять.
function accPoolBanner() {
  const host = document.getElementById('accList');
  const old = document.getElementById('accPoolBanner');
  if (old) old.remove();
  if ((!ACC_POOL_FILTER && !ACC_GEO_FILTER) || !host || !host.parentNode) return;
  const el = document.createElement('div');
  el.id = 'accPoolBanner';
  el.style.cssText = 'display:flex;flex-direction:column;gap:6px;margin:0 16px 8px;padding:8px 12px;'
    + 'border-radius:10px;background:var(--bg-blue-16);font-size:13px';
  let rows = '';
  if (ACC_POOL_FILTER) rows += '<div style="display:flex;align-items:center;gap:8px">'
    + '<span style="flex:1">Пул: <b>' + esc(ACC_POOL_FILTER) + '</b></span>'
    + '<span class="icon-tap" onclick="accPoolClear()" role="button" tabindex="0">Снять \u2715</span></div>';
  if (ACC_GEO_FILTER) rows += '<div style="display:flex;align-items:center;gap:8px">'
    + '<span style="flex:1">Страна: <b>' + esc(ACC_GEO_TITLE || ACC_GEO_FILTER) + '</b></span>'
    + '<span class="icon-tap" onclick="accGeoClear()" role="button" tabindex="0">Снять \u2715</span></div>';
  el.innerHTML = rows;
  host.parentNode.insertBefore(el, host);
}

function accPoolClear() {
  ACC_POOL_FILTER = '';
  accPoolBanner();
  reloadAccounts();
}

function accGeoClear() {
  ACC_GEO_FILTER = ''; ACC_GEO_TITLE = '';
  accPoolBanner();
  reloadAccounts();
}

function accRu(v) {
  return {active:'🟢 Активен', ok:'🟢 Активен', banned:'🚫 Забанен',
          deactivated:'⚫ Удалён', session_expired:'🔑 Нужна переавторизация',
          spamblock:'🔇 Спам-блок', limited:'⚠️ Ограничен',
          // cooldown и no_session возвращает check_account_status_full, а в
          // этой таблице их не было — на экране диагностики они уезжали
          // владельцу английским ключом как есть.
          cooldown:'⏸ Отдыхает после лимита', no_session:'🔌 Нет сессии',
          frozen:'🧊 Заморожен', unknown:'— Неизвестно'}[String(v||'').toLowerCase()] || (v || '—');
}
