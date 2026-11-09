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
  fleetToolsInit('accounts');
  loadAccTags();
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
  loadAccSurvival();
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
    const hadRows = CUR_ACCS.length > 0;
    CUR_ACCS = CUR_ACCS.concat(more);
    ACC_HAS_MORE = !!(ad.page && ad.page.has_more);
    if (ad.page && ad.page.filtered_total!=null) ACC_FILTERED_TOTAL = ad.page.filtered_total;
    ACC_OFFSET += more.length;
    // Новая страница дописывается в конец, а не перерисовывает весь список:
    // стоимость подгрузки — 100 строк, а не все загруженные.
    const listEl = document.getElementById('accList');
    if (hadRows && more.length && listEl && !fleetIsWide() && listEl.querySelector('.acc-row')) {
      listEl.insertAdjacentHTML('beforeend', more.map(accRowHtml).join(''));
    } else {
      renderAccounts(CUR_ACCS);
    }
    renderLoadMore(ad.page);
  } catch(e){
    toast('⚠️ '+errRu(e,'Ошибка'));
    // Не оставляем застрявшую крутилку — возвращаем кнопку для повтора (сценарий
    // «загрузить ещё» доходит до результата: успех или явная возможность повторить).
    if(box) box.innerHTML='<button class="btn btn-s" onclick="loadMoreAccounts()" style="width:100%;font-size:13px;padding:10px">↺ Повторить загрузку</button>';
  }
  finally { ACC_LOADING=false; }
}

// Состояние аккаунта одним словом — для карточки и для строки таблицы.
function accStatusOf(a) {
  const cd = isOnCooldown(a);
  if (a.acc_status==='session_expired') return {status:'🔑 Релог', cls:'b-or', ava_cls:'ava-orange'};
  if (!a.is_active) return {status:'Выкл', cls:'b-gy', ava_cls:'ava-teal'};
  if (a.acc_status==='banned') return {status:'🚫 Бан', cls:'b-rd', ava_cls:'ava-red'};
  if (cd) return {status:'⏳ Кулдаун', cls:'b-or', ava_cls:'ava-orange'};
  // Риск-пульс: аккаунт «активен и подключён», но иммунная система держит его
  // в карантине/риске и операции его пропускают → показываем РЕАЛЬНОЕ состояние,
  // а не ложное «Активен» (иначе «аккаунты не работают, хотя активны»).
  if (a.health_status==='quarantine') return {status:'🛑 На паузе', cls:'b-rd', ava_cls:'ava-red'};
  if (a.health_status==='at_risk') return {status:'⚠️ Под риском', cls:'b-or', ava_cls:'ava-orange'};
  // Мёртвый прокси: аккаунт цел, но каждая его операция падает по сети. Без
  // этого он показывался «Активен», и человек шёл чинить аккаунт вместо
  // замены прокси. Аккаунты БЕЗ прокси работают с адреса хоста — это норма.
  if (a.has_proxy && a.proxy_alive === false) return {status:'🔌 Прокси', cls:'b-rd', ava_cls:'ava-red'};
  return {status:'Активен', cls:'b-gr', ava_cls:'ava-teal'};
}

function renderAccounts(list) {
  const el = document.getElementById('accList');
  if (!list.length) {
    const filtered = ACC_SEARCH || ACC_STAGE_FILTER || ACC_POOL_FILTER || ACC_GEO_FILTER || ACC_TAG_FILTER || (ACC_FILTER && ACC_FILTER!=='all');
    if (filtered) el.innerHTML=empty('🔍','Ничего не найдено','Измените фильтр, статус или строку поиска');
    else el.innerHTML=empty('📱','Нет аккаунтов','Добавьте аккаунт прямо здесь: по номеру, QR-коду, файлу .session, архиву tdata или строке сессии',{label:'➕ Добавить аккаунт',fn:'openAccImportModal()'});
    return;
  }
  // Широкий экран — плотная таблица с сортировкой по заголовкам (fleet_tools.js).
  if (fleetIsWide()) { el.innerHTML = accTableHtml(list); return; }
  el.innerHTML = list.map(accRowHtml).join('');
}

// Одна строка мобильного списка. Вынесена, чтобы подгрузка и выбор строки
// не перерисовывали весь список: при 3 000 загруженных аккаунтов каждая
// подгрузка строила HTML всех строк заново и занимала до 4,5 с на слабом
// телефоне (замер 08.11.2026), а каждое касание в режиме выбора — столько же.
function accRowHtml(a) {
    const name = a.first_name||(a.username?'@'+a.username:'#'+a.id);
    const phone = a.phone || '';
    const flag = phoneFlag(phone);
    const trust = a.trust_score!=null?Math.round(a.trust_score):100;
    const trustColor = trust>=80?'var(--green)':trust>=50?'var(--orange)':'var(--red)';
    const {status, cls, ava_cls} = accStatusOf(a);
    const proxyNote = (a.has_proxy && a.proxy_alive === false)
      ? `<div class="acc-phone" style="color:var(--red)">🔌 прокси не отвечает${a.proxy_label?' · '+esc(a.proxy_label):''} — аккаунт цел, замените прокси</div>`
      : '';
    // «🛑 На паузе» без причины отправляло человека искать объяснение в
    // карточку аккаунта. Причину считает пульс — печатаем её здесь же.
    const pulseNote = (a.health_reason && (a.health_status==='quarantine' || a.health_status==='at_risk'))
      ? `<div class="acc-phone" style="color:var(--${a.health_status==='quarantine'?'red':'orange'})">${esc(a.health_reason)}</div>`
      : '';
    const sel = ACC_SEL.has(a.id);
    const onclick = ACC_SEL_MODE ? `toggleAccSel(${a.id},event)` : `openAccount(${a.id})`;
    const checkbox = ACC_SEL_MODE ? `<div class="acc-check ${sel?'on':''}">${sel?'✓':''}</div>` : '';
    const right = ACC_SEL_MODE ? '' : `<div class="acc-right">
        <span class="badge ${cls}">${status}</span>
        <span style="font-size:10px;color:var(--hint)">${trust}%</span>
        <span class="chev">›</span>
      </div>`;
    return `<div class="acc-row ${ACC_SEL_MODE&&sel?'sel':''}" data-acc-id="${a.id}" onclick="${onclick}">
      ${checkbox}
      <div class="ava ${ava_cls}" style="font-size:15px">${flag}</div>
      <div class="acc-body">
        <div class="acc-name">${esc(name)} ${stageBadge(a.stage)}</div>
        <div class="acc-phone">${esc(phone)}</div>
        ${(a.tags||[]).length ? `<div class="fx-tags">${accTagsHtml(a)}</div>` : ''}
        ${proxyNote}
        ${pulseNote}
        <div class="acc-trust"><div class="acc-trust-fill" style="width:${trust}%;background:${trustColor}"></div></div>
        ${ACC_SEL_MODE ? '' : _fixBtn(a.fix)}
      </div>
      ${right}
    </div>`;
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
  let cnt = null;
  try {
    const st = await api('/api/miniapp/accounts?filter=dead&limit=1');
    // Итог под фильтром лежит в st.page, а не в корне ответа: запасной путь
    // читал st.filtered_total, то есть всегда undefined. И сам запасной путь
    // нужен: stats приходит пустым, когда сводный запрос не сложился, —
    // тогда «0» означает «не знаю», а не «мёртвых нет».
    const _ft = (st.page && st.page.filtered_total != null) ? st.page.filtered_total : null;
    cnt = (st.stats && st.stats.dead != null) ? st.stats.dead : _ft;
  } catch(e) {
    // Не удалось СПРОСИТЬ — значит мы не знаем ответа. Раньше здесь оставался
    // cnt=0, и приложение утверждало «Мёртвых аккаунтов нет 👍»: упавший запрос
    // выдавался за проверенный факт, и владелец переставал чистить парк.
    toast('⚠️ Не удалось проверить: ' + errRu(e,'ошибка связи'));
    return;
  }
  if (cnt == null) {
    // Ни сводки, ни итога под фильтром — ответ есть, а числа в нём нет.
    // Сказать «мёртвых нет» значило бы выдать незнание за проверенный факт.
    toast('⚠️ Не удалось узнать, сколько мёртвых аккаунтов — попробуйте ещё раз');
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
    txt('acd-body', errHtml(errRu(e), retryCall('openAccount', accId)));
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
          deactivated:'⚫ Удалён', deleted:'⚫ Удалён в Telegram',
          session_expired:'🔑 Нужна переавторизация',
          spamblock:'🔇 Спам-блок', limited:'⚠️ Ограничен',
          // cooldown и no_session возвращает check_account_status_full, а в
          // этой таблице их не было — на экране диагностики они уезжали
          // владельцу английским ключом как есть.
          cooldown:'⏸ Отдыхает после лимита', no_session:'🔌 Нет сессии',
          frozen:'🧊 Заморожен', unknown:'— Неизвестно'}[String(v||'').toLowerCase()] || (v || '—');
}

// Выживаемость флота — главная цифра здоровья: сколько аккаунтов из ста ушло
// за неделю и месяц, лучше ли это прошлой недели и что убивает флот. Карточка
// второстепенна: любая ошибка просто прячет её, список аккаунтов не трогает.
async function loadAccSurvival() {
  const box = document.getElementById('accSurvival');
  if (!box) return;
  try {
    const d = await api('/api/miniapp/accounts/survival');
    box.innerHTML = renderAccSurvival(d);
    box.style.display = box.innerHTML ? '' : 'none';
  } catch(e) { box.style.display = 'none'; }
}

function renderAccSurvival(d) {
  if (!d || !d.windows || !d.windows['30'] || !d.windows['30'].base) return '';
  const w7 = d.windows['7'], w30 = d.windows['30'];
  const v = d.verdict || {};
  const tone = v.level === 'bad' ? 'var(--red)' : (v.level === 'warn' ? 'var(--orange)' : 'var(--green)');
  const tr = d.trend || {};
  const trTxt = tr.direction === 'better' ? '↓ лучше прошлой недели (' + tr.prev_week + ')'
              : tr.direction === 'worse' ? '↑ хуже прошлой недели (' + tr.prev_week + ')'
              : 'как на прошлой неделе';
  const lines = [
    '<div style="font-weight:700;margin-bottom:2px">🛡 Выживаемость за 30 дней: <span style="color:' + tone + '">' + w30.survival_pct + '%</span></div>',
    '<div>Потеряно ' + w30.lost + ' из ' + w30.base + ' (' + w30.lost_pct + '%) · ограничено ' + w30.restricted +
      ' · вернулось в строй ' + w30.recovered + '</div>',
    '<div style="color:var(--hint)">За 7 дней: потеряно ' + w7.lost + ', ' + trTxt + '</div>',
  ];
  if (w30.lost) {
    const causes = (d.causes || []).map(c => esc(c.label) + ' — ' + c.count).join(', ');
    const ages = (d.by_age || []).filter(a => a.count).map(a => esc(a.label) + ' — ' + a.count).join(', ');
    const px = d.by_proxy || {};
    lines.push('<div style="color:var(--hint)">Причины: ' + causes + '</div>');
    lines.push('<div style="color:var(--hint)">Возраст при потере: ' + ages + '</div>');
    lines.push('<div style="color:var(--hint)">С прокси — ' + (px.with_proxy || 0) + ', без прокси — ' + (px.no_proxy || 0) +
      (px.unknown ? ', неизвестно — ' + px.unknown : '') + '</div>');
  }
  if (v.text) lines.push('<div style="margin-top:4px;color:' + tone + '">' + esc(v.text) + '</div>');
  return lines.join('');
}
