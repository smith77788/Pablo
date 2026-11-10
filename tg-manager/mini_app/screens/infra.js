// ── Модуль «Инфраструктура» (screens/infra.js) ─────────────────────────────
// Вкладки «Пулы и аудит», «Топология» и полный журнал аудита.
//
// Что было не так (10.11.2026, скриншоты владельца):
//   • пулы нельзя было создать: пустой экран отправлял «назначить на карточке
//     аккаунта», а такой настройки в мини-аппе не было нигде — только в боте;
//   • журнал показывал латиницу («warmup», «day 8») и красный крестик без
//     причины, десять строк, без фильтра и без перехода к операции;
//   • карта связей печатала все каналы аккаунта одной строкой через запятую.
//
// Файл — классический скрипт: грузится после основного блока index.html и
// делит с ним глобальную область (api, esc, escJs, num, plural, txt, push…).
'use strict';

// В operation_audit.action попадает не только тип операции (его переводит
// opRu), но и собственные действия воркера и прогрева. Сервер присылает
// готовую подпись (action_ru); словарь — запасной путь для старых ответов.
const AUDIT_RU = {
  gp_create_bot:     'создан бот',
  gp_create_group:   'создана группа',
  gp_create_channel: 'создан канал',
  join:              'вступление',
  leave:             'выход',
  publish:           'публикация',
  view:              'просмотры',
  reaction:          'реакции',
  warmup:            'прогрев аккаунта',
  peer_flood:        'спам-ограничение Telegram',
};
function auditRu(a) { return AUDIT_RU[String(a || '').toLowerCase()] || opRu(a); }

const _AUDIT_TONE = {
  ok:   {ico: '✅', bg: '52,211,153'},
  warn: {ico: '⚠️', bg: '251,191,36'},
  fail: {ico: '❌', bg: '248,113,113'},
};
// Тон строки, если сервер старый и tone не прислал.
function _auditTone(a) {
  if (a.tone) return a.tone;
  const r = String(a.result || '').toLowerCase();
  if (['success', 'ok', 'done', 'requested'].includes(r)) return 'ok';
  if (['partial', 'flood_wait', 'skipped'].includes(r)) return 'warn';
  return 'fail';
}

// Строка журнала. Нажатие разворачивает подробности: причина сбоя по-русски,
// исходный текст ошибки, аккаунт и операция — с переходом в них.
function infraAuditRowHtml(a, key) {
  const tone = _AUDIT_TONE[_auditTone(a)] || _AUDIT_TONE.fail;
  const when = a.occurred_at ? new Date(a.occurred_at).toLocaleString('ru-RU') : '';
  const title = a.action_ru || auditRu(a.action);
  const sub = [a.account_label, a.target, when].filter(Boolean).map(esc).join(' · ');
  const why = (_auditTone(a) !== 'ok' && a.error_ru)
    ? `<div class="row-val" style="color:var(--${_auditTone(a) === 'warn' ? 'orange' : 'red'})">${esc(a.error_ru)}</div>` : '';
  return `<div class="row tap" onclick="infraAuditToggle('${escJs(key)}')" role="button" tabindex="0">
      <div class="row-ico" style="background:rgba(${tone.bg},.16)">${tone.ico}</div>
      <div class="row-body">
        <div class="row-name">${esc(title)}</div>
        <div class="row-val">${sub}</div>
        ${why}
        <div id="iad-${esc(key)}" style="display:none">${_infraAuditDetail(a)}</div>
      </div>
    </div>`;
}
function _infraAuditDetail(a) {
  const line = (k, v) => v ? `<div style="font-size:12px;color:var(--hint);margin-top:4px"><b>${esc(k)}:</b> ${v}</div>` : '';
  const btns = [];
  if (a.operation_id) btns.push(`<button class="mini-btn" onclick="event.stopPropagation();openOpDetail(${Number(a.operation_id)})">📋 Операция #${Number(a.operation_id)}</button>`);
  if (a.account_id) btns.push(`<button class="mini-btn" onclick="event.stopPropagation();openAccount(${Number(a.account_id)})">👤 Аккаунт</button>`);
  return `<div style="margin-top:6px;padding:8px 10px;border-radius:10px;background:var(--bg)">`
    + line('Итог', esc(a.result_ru || a.result || ''))
    + line('Объект', esc(a.target || ''))
    + line('Аккаунт', esc(a.account_label || '') + (a.account_pool ? ` · пул «${esc(infraPoolLabel(a.account_pool))}»` : ''))
    + line('Пауза Telegram', a.flood_wait_s ? `${num(a.flood_wait_s)} с` : '')
    + line('Длительность', a.duration_ms ? `${num(a.duration_ms)} мс` : '')
    + line('Технические подробности', a.error_raw ? `<code style="word-break:break-word">${esc(a.error_raw)}</code>` : '')
    + (btns.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${btns.join('')}</div>` : '')
    + `</div>`;
}
function infraAuditToggle(key) {
  const el = document.getElementById('iad-' + key);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

// ── Пулы ────────────────────────────────────────────────────────────────────
const INFRA_SYS_POOLS = {primary: 'Основной', monitoring: 'Под наблюдением', cooldown: 'На остывании'};
function infraPoolLabel(p) { return p ? (INFRA_SYS_POOLS[p] || p) : 'Без пула'; }
let INFRA_POOLS = [];          // последняя сводка пулов: для выбора пула в диалогах
let INFRA_UNPOOLED = {total: 0, alive: 0};

function _infraPoolRow(p, i) {
  const bits = [`${num(p.total)} ${plural(p.total, 'аккаунт', 'аккаунта', 'аккаунтов')}`];
  if (p.alive !== p.total) bits.push(`рабочих ${num(p.alive)}`);
  if (p.cooldown) bits.push(`на паузе ${num(p.cooldown)}`);
  if (p.dead) bits.push(`потеряно ${num(p.dead)}`);
  if (p.avg_trust != null) bits.push(`траст ${p.avg_trust}%`);
  const health = p.total ? p.alive / p.total : 0;
  const ico = p.system ? ({primary: '🟢', monitoring: '🟡', cooldown: '🔴'}[p.pool] || '🗂') : '🗂';
  return `<div class="row">
      <div class="row-ico tap" style="background:var(--bg-blue-16)" onclick="infraPoolOpen(${i})">${ico}</div>
      <div class="row-body tap" onclick="infraPoolOpen(${i})" role="button" tabindex="0">
        <div class="row-name">${esc(p.label || infraPoolLabel(p.pool))}</div>
        <div class="row-val">${bits.join(' · ')}</div>
        ${p.hint ? `<div class="row-val" style="opacity:.7">${esc(p.hint)}</div>` : ''}
        <div style="height:4px;border-radius:2px;background:var(--sep);margin-top:6px;overflow:hidden">
          <div style="height:100%;width:${Math.round(health * 100)}%;background:var(--${health >= .8 ? 'green' : health >= .5 ? 'orange' : 'red'})"></div>
        </div>
      </div>
      <div class="row-right"><button class="icon-btn sm" onclick="infraPoolMenu(${i})" aria-label="Действия с пулом" title="Действия с пулом">⋯</button></div>
    </div>`;
}

async function openInfra() {
  push('s-infra');
  txt('infraStats', '<div class="spin-wrap"><div class="spin"></div></div>');
  txt('infraPools', '');
  txt('infraAudit', '');
  try {
    const d = await api('/api/miniapp/infra');
    // Каждое число открывает свой список.
    const iCard = (val, lbl, style, go) =>
      `<div class="kpi-card tap-card" onclick="${go}" role="button" tabindex="0">`
      + `<div class="kpi-val"${style ? ` style="${style}"` : ''}>${num(val)}</div>`
      + `<div class="kpi-lbl">${lbl} ›</div></div>`;
    txt('infraStats',
        iCard(d.active_accounts || 0, 'Активных акк', kpiTone(d.active_accounts, 'green'), "healthGoAccounts('active','')")
      + iCard(d.flood_24h || 0, 'Флуд 24ч', kpiTone(d.flood_24h, 'orange'), 'openHealth()')
      + iCard(d.ops_24h || 0, 'Операций 24ч', '', 'openOps()')
      + iCard(d.warmup_active || 0, 'На прогреве', '', 'openWarmup()')
      + iCard(d.fails_24h || 0, 'Сбоев 24ч', kpiTone(d.fails_24h, 'red'), "openInfraAudit('fail','24h')"));
    txt('m-infra-val', `${d.active_accounts || 0} акк · ${d.flood_24h || 0} флуд`);

    INFRA_POOLS = d.pools_full || (d.pools || []).map(p => ({pool: p.pool, label: infraPoolLabel(p.pool), total: p.cnt, alive: p.cnt}));
    INFRA_UNPOOLED = d.unpooled || {total: 0, alive: 0};
    const actions = `<div style="display:flex;gap:8px;flex-wrap:wrap;padding:4px 16px 10px">
        <button class="mini-btn" onclick="infraPoolCreate()">➕ Новый пул</button>
        <button class="mini-btn" onclick="infraPoolRebalance()">⚖️ Разложить по здоровью</button>
      </div>`;
    if (!INFRA_POOLS.length) {
      // Пул — колонка аккаунта, а не отдельная сущность: «пустого пула» не
      // бывает. Поэтому создание = сразу назначить кого-то.
      txt('infraPools', actions + empty('📦', 'Пулов пока нет',
        'Пул — группа аккаунтов под одну задачу: «Инвайт», «Прогрев», «Резерв». Откройте пул в списке аккаунтов, выберите «Весь срез» — и любое массовое действие пойдёт только по нему',
        {label: '➕ Создать пул', fn: 'infraPoolCreate()'}));
    } else {
      txt('infraPools', actions + INFRA_POOLS.map(_infraPoolRow).join('')
        + (INFRA_UNPOOLED.total ? `<div class="row tap" onclick="healthGoAccounts('all','','')" role="button" tabindex="0">
            <div class="row-ico" style="background:var(--sep)">⚪</div>
            <div class="row-body"><div class="row-name">Без пула</div>
              <div class="row-val">${num(INFRA_UNPOOLED.total)} ${plural(INFRA_UNPOOLED.total, 'аккаунт', 'аккаунта', 'аккаунтов')} не входят ни в один пул</div></div>
            <div class="row-right"><button class="mini-btn" onclick="event.stopPropagation();infraPoolCreate()">В пул</button></div>
          </div>` : ''));
    }
    const audit = d.audit || [];
    txt('infraAudit', audit.length
      ? audit.map((a, i) => infraAuditRowHtml(a, 'h' + i)).join('')
        + `<div class="row tap" onclick="openInfraAudit()" role="button" tabindex="0"><div class="row-body"><div class="row-name" style="color:var(--blue)">Весь журнал с фильтрами ›</div></div></div>`
      : empty('📋', 'Записей пока нет', 'Здесь появятся последние действия над аккаунтами и операциями'));
  } catch (e) {
    txt('infraStats', '');
    txt('infraAudit', errHtml(errRu(e), 'openInfra()'));
  }
}

function infraPoolOpen(i) {
  const p = INFRA_POOLS[i];
  if (p) healthGoAccounts('all', '', p.pool);
}

async function infraPoolMenu(i) {
  const p = INFRA_POOLS[i];
  if (!p) return;
  const lbl = p.label || infraPoolLabel(p.pool);
  const choice = await askChoice(`Пул «${lbl}»`, [
    {value: 'open', label: '📱 Открыть аккаунты пула', sub: 'список, где можно выбрать «Весь срез» и запустить массовое действие'},
    {value: 'add', label: '➕ Добавить аккаунты без пула', sub: INFRA_UNPOOLED.total ? `сейчас без пула: ${num(INFRA_UNPOOLED.total)}` : 'аккаунтов без пула нет'},
    {value: 'pick', label: '☑️ Добавить выбранные аккаунты', sub: 'откроется список аккаунтов в режиме выбора'},
    {value: 'journal', label: '📋 Журнал этого пула'},
    {value: 'rename', label: '✏️ Переименовать', sub: 'имя существующего пула — объединит два пула в один'},
    {value: 'dissolve', label: '🗑 Распустить пул', sub: 'аккаунты останутся, у них снимется пул'},
  ]);
  if (!choice) return;
  if (choice === 'open') return infraPoolOpen(i);
  if (choice === 'journal') return openInfraAudit('all', '7d', p.pool);
  if (choice === 'pick') return infraPickAccounts();
  try {
    if (choice === 'add') {
      if (!INFRA_UNPOOLED.total) { toast('Все аккаунты уже в пулах'); return; }
      if (!await askConfirm(`Добавить в пул «${lbl}» все аккаунты без пула (${num(INFRA_UNPOOLED.total)})?`)) return;
      const r = await api('/api/miniapp/infra/pools/assign', {method: 'POST',
        body: JSON.stringify({pool: p.pool, mode: 'unpooled'})});
      toast(`✅ В пул «${lbl}» добавлено: ${num(r.count || 0)}`);
    } else if (choice === 'rename') {
      const name = await askPrompt(`Новое название пула «${lbl}»`, {value: lbl, placeholder: 'Например: Инвайт'});
      if (!name || !name.trim() || name.trim() === lbl) return;
      const r = await api('/api/miniapp/infra/pools/rename', {method: 'POST',
        body: JSON.stringify({from: p.pool, to: name.trim()})});
      toast(`✅ Пул переименован в «${r.label}» · аккаунтов: ${num(r.count || 0)}`);
    } else if (choice === 'dissolve') {
      if (!await askConfirm(`Распустить пул «${lbl}»? У ${num(p.total)} ${plural(p.total, 'аккаунта', 'аккаунтов', 'аккаунтов')} снимется пул, сами аккаунты останутся.`)) return;
      const r = await api('/api/miniapp/infra/pools/dissolve', {method: 'POST',
        body: JSON.stringify({pool: p.pool})});
      toast(`✅ Пул распущен · аккаунтов: ${num(r.count || 0)}`);
    }
    openInfra();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}

// Создать пул. Пул живёт на аккаунтах, поэтому создать его — значит сразу
// назначить: всем без пула или выбранным в списке.
async function infraPoolCreate() {
  const name = await askPrompt('Название нового пула', {placeholder: 'Например: Инвайт, Прогрев, Резерв'});
  if (!name || !name.trim()) return;
  const how = await askChoice(`Кого положить в пул «${name.trim()}»?`, [
    {value: 'unpooled', label: `Все аккаунты без пула (${num(INFRA_UNPOOLED.total || 0)})`,
     sub: 'быстрый способ разложить новые аккаунты'},
    {value: 'pick', label: 'Выбрать аккаунты в списке', sub: 'отметьте нужные и нажмите «🗂 В пул»'},
  ]);
  if (!how) return;
  if (how === 'pick') { INFRA_PENDING_POOL = name.trim(); return infraPickAccounts(); }
  if (!INFRA_UNPOOLED.total) { toast('Аккаунтов без пула нет — выберите аккаунты в списке'); return; }
  try {
    const r = await api('/api/miniapp/infra/pools/assign', {method: 'POST',
      body: JSON.stringify({pool: name.trim(), mode: 'unpooled'})});
    toast(`✅ Пул «${r.label}» создан · аккаунтов: ${num(r.count || 0)}`);
    openInfra();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}
let INFRA_PENDING_POOL = '';   // имя пула, которое создают через выбор в списке
function infraPickAccounts() {
  goTab('accounts');
  setTimeout(() => {
    try { if (!ACC_SEL_MODE) toggleAccSelMode(); } catch (_) {}
    toast('Отметьте аккаунты и нажмите «🗂 В пул»');
  }, 150);
}

// Автораспределение по здоровью: сначала показать, что изменится.
async function infraPoolRebalance() {
  try {
    const d = await api('/api/miniapp/infra/pools/rebalance');
    if (!d.total) { await showInfo('✅ Все аккаунты уже лежат в подходящих пулах — менять нечего.'); return; }
    const by = {};
    (d.changes || []).forEach(c => { by[c.to_label] = (by[c.to_label] || 0) + 1; });
    const lines = Object.keys(by).map(k => `• «${k}»: ${num(by[k])}`).join('\n');
    const ex = (d.changes || []).slice(0, 5).map(c => `${c.label}: ${c.from_label} → ${c.to_label}`).join('\n');
    const ok = await askConfirm(
      `Разложить по здоровью ${num(d.total)} ${plural(d.total, 'аккаунт', 'аккаунта', 'аккаунтов')}?\n\n${lines}\n\nНапример:\n${ex}\n\n`
      + 'Правило: траст от 75% без предупреждений — «Основной»; траст ниже 30% или предупреждения — «Под наблюдением»; на паузе после флуда — «На остывании». Остальные не трогаются.');
    if (!ok) return;
    const r = await api('/api/miniapp/infra/pools/rebalance', {method: 'POST', body: '{}'});
    toast(`✅ Разложено: ${num(r.changed || 0)}`);
    openInfra();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}

// Пул выбранным аккаунтам — кнопка «🗂 В пул» на панели выбора аккаунтов.
async function runAccPool() {
  const n = _massSelCount();
  if (!n) { toast('❗ Выберите хотя бы один аккаунт'); return; }
  let target = INFRA_PENDING_POOL;
  if (!target) {
    try {
      const d = await api('/api/miniapp/infra/pools');
      INFRA_POOLS = d.pools || [];
    } catch (_) {}
    const opts = INFRA_POOLS.map(p => ({value: 'p:' + p.pool, label: '🗂 ' + (p.label || infraPoolLabel(p.pool)),
      sub: `${num(p.total)} ${plural(p.total, 'аккаунт', 'аккаунта', 'аккаунтов')}`}));
    opts.push({value: 'new', label: '➕ Новый пул…'});
    opts.push({value: 'none', label: '✖ Убрать из пула'});
    const ch = await askChoice(`Пул для ${num(n)} ${plural(n, 'аккаунта', 'аккаунтов', 'аккаунтов')}`, opts);
    if (!ch) return;
    if (ch === 'new') {
      const name = await askPrompt('Название нового пула', {placeholder: 'Например: Инвайт'});
      if (!name || !name.trim()) return;
      target = name.trim();
    } else if (ch === 'none') {
      target = '';
    } else {
      target = ch.slice(2);
    }
  }
  try {
    const r = await api('/api/miniapp/accounts/mass', {method: 'POST',
      body: JSON.stringify(Object.assign({op: 'set_pool', value: target}, _massSel()))});
    INFRA_PENDING_POOL = '';
    toast((target ? `🗂 В пул «${r.label}»: ` : '🗂 Пул снят: ') + num(r.count || 0)
      + (r.capped ? ` · не вошло сверх ${num(FLEET_MASS_CAP)}: ${num(r.capped)}` : ''));
    toggleAccSelMode();
    reloadAccounts();
  } catch (e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}

// Строка «Пул» в карточке аккаунта: раньше пул нельзя было ни увидеть, ни
// сменить на карточке, хотя экран пулов отправлял именно туда.
function accPoolRowHtml(a) {
  if (!a || !a.id) return '';
  return `<div class="sec" style="margin-top:8px">Пул</div>
    <div class="lst"><div class="row tap" onclick="accSetPool(${Number(a.id)})" role="button" tabindex="0">
      <div class="row-ico" style="background:var(--bg-blue-16)">🗂</div>
      <div class="row-body"><div class="row-name">${esc(infraPoolLabel(a.pool))}</div>
        <div class="row-val">Группа аккаунтов под одну задачу — по ней фильтруются массовые действия</div></div>
      <div class="row-right">Сменить ›</div></div></div>`;
}
async function accSetPool(accId) {
  let pools = [];
  try { pools = (await api('/api/miniapp/infra/pools')).pools || []; } catch (_) {}
  const opts = pools.map(p => ({value: 'p:' + p.pool, label: '🗂 ' + (p.label || infraPoolLabel(p.pool))}));
  opts.push({value: 'new', label: '➕ Новый пул…'});
  opts.push({value: 'none', label: '✖ Без пула'});
  const ch = await askChoice('Пул аккаунта', opts);
  if (!ch) return;
  let target = '';
  if (ch === 'new') {
    const name = await askPrompt('Название нового пула', {placeholder: 'Например: Инвайт'});
    if (!name || !name.trim()) return;
    target = name.trim();
  } else if (ch !== 'none') target = ch.slice(2);
  try {
    const r = await api('/api/miniapp/infra/pools/assign', {method: 'POST',
      body: JSON.stringify({pool: target, mode: 'ids', account_ids: [accId]})});
    toast(target ? `🗂 Пул: «${r.label}»` : '🗂 Пул снят');
    openAccount(accId);
  } catch (e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}

// ── Полный журнал аудита ────────────────────────────────────────────────────
let IA = {result: 'all', period: '7d', pool: '', action: '', q: '', rows: [], total: 0, offset: 0};
const IA_RESULTS = [['all', 'Все'], ['fail', '❌ Сбои'], ['warn', '⚠️ Предупреждения'], ['ok', '✅ Успешные']];
const IA_PERIODS = [['24h', '24 часа'], ['7d', '7 дней'], ['30d', '30 дней'], ['all', 'Всё время']];

function openInfraAudit(result, period, poolName) {
  push('s-infraaudit');
  IA = {result: result || 'all', period: period || '7d', pool: poolName || '', action: '', q: '',
        rows: [], total: 0, offset: 0};
  const qi = document.getElementById('iaSearch');
  if (qi) qi.value = '';
  loadInfraAudit(true);
}
function iaSet(key, val) {
  IA[key] = IA[key] === val && key === 'action' ? '' : val;
  loadInfraAudit(true);
}
let _iaSearchT = null;
function iaSearch(v) {
  clearTimeout(_iaSearchT);
  _iaSearchT = setTimeout(() => { IA.q = String(v || '').trim(); loadInfraAudit(true); }, 350);
}
function _iaChips() {
  const chip = (key, v, lbl) => `<span class="chip${IA[key] === v ? ' on' : ''}" role="button" tabindex="0" onclick="iaSet('${key}','${v}')">${lbl}</span>`;
  return `<div class="filter-chips" style="padding:4px 16px">${IA_RESULTS.map(([v, l]) => chip('result', v, l)).join('')}</div>`
    + `<div class="filter-chips" style="padding:4px 16px">${IA_PERIODS.map(([v, l]) => chip('period', v, l)).join('')}</div>`
    + (IA.pool ? `<div style="padding:4px 16px;font-size:13px">Пул: <b>${esc(infraPoolLabel(IA.pool))}</b> <span class="sel-bar-link" onclick="iaSet('pool','')">убрать</span></div>` : '')
    + (IA.action ? `<div style="padding:4px 16px;font-size:13px">Действие: <b>${esc(auditRu(IA.action))}</b> <span class="sel-bar-link" onclick="iaSet('action','')">убрать</span></div>` : '');
}
async function loadInfraAudit(reset) {
  if (reset) { IA.rows = []; IA.offset = 0; }
  txt('iaFilters', _iaChips());
  if (reset) {
    txt('iaSummary', '<div class="spin-wrap"><div class="spin"></div></div>');
    txt('iaList', '');
  }
  const p = new URLSearchParams({result: IA.result, period: IA.period, offset: String(IA.offset), limit: '30'});
  if (IA.pool) p.set('pool', IA.pool);
  if (IA.action) p.set('action', IA.action);
  if (IA.q) p.set('q', IA.q);
  if (reset) p.set('summary', '1');
  try {
    const d = await api('/api/miniapp/infra/audit?' + p.toString());
    IA.total = d.total || 0;
    IA.rows = IA.rows.concat(d.rows || []);
    IA.offset = IA.rows.length;
    if (d.summary) txt('iaSummary', _iaSummaryHtml(d.summary));
    _iaRender();
  } catch (e) {
    txt('iaSummary', '');
    txt('iaList', errHtml(errRu(e), 'loadInfraAudit(true)'));
  }
}
function _iaSummaryHtml(s) {
  const card = (v, lbl, tone, res) => `<div class="kpi-card tap-card" onclick="iaSet('result','${res}')" role="button" tabindex="0">`
    + `<div class="kpi-val"${tone ? ` style="color:var(--${tone})"` : ''}>${num(v)}</div><div class="kpi-lbl">${lbl}</div></div>`;
  let html = `<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;padding:8px 16px 4px">`
    + card(s.total, 'Всего записей', '', 'all') + card(s.ok, 'Успешно', 'green', 'ok')
    + card(s.warn, 'Предупреждений', 'orange', 'warn') + card(s.fail, 'Сбоев', 'red', 'fail') + `</div>`;
  if (s.total) {
    const rate = Math.round(100 * s.ok / s.total);
    html += `<div style="padding:4px 16px 8px;font-size:13px;color:var(--hint)">Доля успешных за период: <b style="color:var(--${rate >= 80 ? 'green' : rate >= 50 ? 'orange' : 'red'})">${rate}%</b></div>`;
  }
  if ((s.top_errors || []).length) {
    html += `<div class="sec">Частые причины сбоев</div><div class="lst">`
      + s.top_errors.map(g => `<div class="row"><div class="row-ico" style="background:var(--bg-red-16)">⚠️</div>
          <div class="row-body"><div class="row-name">${esc(g.reason)}</div>
          <div class="row-val">${esc(g.action_ru)} · ${num(g.count)} ${plural(g.count, 'раз', 'раза', 'раз')}</div></div></div>`).join('')
      + `</div>`;
  }
  if ((s.actions || []).length) {
    html += `<div class="sec">Что делалось чаще всего · нажмите, чтобы отфильтровать</div>`
      + `<div class="filter-chips" style="padding:4px 16px 8px;flex-wrap:wrap">`
      + s.actions.map(a => `<span class="chip${IA.action === a.action ? ' on' : ''}" role="button" tabindex="0" onclick="iaSet('action','${escJs(a.action)}')">${esc(a.action_ru)} · ${num(a.count)}${a.fail ? ` <span style="color:var(--red)">(${num(a.fail)} ❌)</span>` : ''}</span>`).join('')
      + `</div>`;
  }
  return html;
}
function _iaRender() {
  if (!IA.rows.length) {
    txt('iaList', empty('📋', 'Под этим фильтром записей нет',
      IA.result === 'fail' ? 'Сбоев за выбранный период не было' : 'Попробуйте другой период или уберите фильтр',
      {label: 'Показать всё за 30 дней', fn: "iaReset()"}));
    return;
  }
  txt('iaList', IA.rows.map((a, i) => infraAuditRowHtml(a, 'a' + i)).join('')
    + (IA.rows.length < IA.total
      ? `<button class="btn btn-s" style="width:calc(100% - 32px);margin:8px 16px" onclick="loadInfraAudit(false)">Показать ещё (${num(IA.total - IA.rows.length)})</button>`
      : `<div style="text-align:center;font-size:12px;color:var(--hint);padding:10px">Показаны все ${num(IA.total)}</div>`));
}
function iaReset() {
  IA.result = 'all'; IA.period = '30d'; IA.action = ''; IA.pool = ''; IA.q = '';
  const qi = document.getElementById('iaSearch');
  if (qi) qi.value = '';
  loadInfraAudit(true);
}

// ── Топология ───────────────────────────────────────────────────────────────
let TOPO = {accounts: [], bots: [], q: '', view: 'accounts', open: {}};

async function openTopology() {
  push('s-topology');
  txt('topoStats', '<div class="spin-wrap"><div class="spin"></div></div>');
  txt('topoBody', '');
  try {
    const d = await api('/api/miniapp/topology');
    // «Связи» и «Подписчики» сводного списка не имеют — остаются числами,
    // а не притворяются кнопками.
    txt('topoStats', `
      <div class="kpi-card tap-card" onclick="healthGoAccounts('all','')" role="button" tabindex="0"><div class="kpi-val">${num(d.accounts || 0)}</div><div class="kpi-lbl">Аккаунтов ›</div></div>
      <div class="kpi-card tap-card" onclick="goTab('channels')" role="button" tabindex="0"><div class="kpi-val">${num(d.channels || 0)}</div><div class="kpi-lbl">Каналов ›</div></div>
      <div class="kpi-card tap-card" onclick="goTab('bots')" role="button" tabindex="0"><div class="kpi-val">${num(d.bots || 0)}</div><div class="kpi-lbl">Ботов ›</div></div>
      <div class="kpi-card"><div class="kpi-val">${num(d.bot_users_total || 0)}</div><div class="kpi-lbl">Людей в ботах</div></div>
    `);
    txt('topoBody', `<div class="spin-wrap"><div class="spin"></div></div>`);
    const links = await api('/api/miniapp/topology/links');
    TOPO.accounts = links.accounts || [];
    TOPO.bots = links.bots || [];
    TOPO.orphans = links.orphan_channels || 0;
    TOPO.channelLinks = d.channel_links || 0;
    if (!TOPO.accounts.some(a => (a.channels || []).length) && !TOPO.bots.length) {
      txt('topoBody', empty('🗺', 'Связей пока нет',
        'Связь появляется, когда аккаунт становится админом канала или группы, или когда человек приходит в вашего бота',
        {label: '📢 К каналам', fn: "goTab('channels')"}));
      return;
    }
    txt('topoBody', `
      <div id="topoInsights"></div>
      <div class="filter-chips" style="padding:8px 16px 4px" role="tablist">
        <span class="chip" data-v="accounts" role="tab" tabindex="0" onclick="topoView('accounts')">📱 Аккаунты и каналы</span>
        <span class="chip" data-v="bots" role="tab" tabindex="0" onclick="topoView('bots')">🤖 Боты</span>
      </div>
      <div style="padding:4px 16px 8px"><input class="input-l" id="topoSearch" placeholder="Поиск: аккаунт, канал, @ник" oninput="topoSearch(this.value)"></div>
      <div class="lst" id="topoLinks"></div>`);
    _topoInsights();
    topoView(TOPO.view);
  } catch (e) {
    txt('topoStats', '');
    txt('topoBody', empty('⚠️', 'Ошибка', errRu(e), {label: '↺ Повторить', fn: retryCall('openTopology')}));
  }
}
function _topoInsights() {
  const accs = TOPO.accounts;
  const withCh = accs.filter(a => (a.channels || []).length);
  const noCh = accs.length - withCh.length;
  const totalCh = withCh.reduce((s, a) => s + a.channels.length, 0);
  const avg = withCh.length ? (totalCh / withCh.length) : 0;
  const top = withCh.slice().sort((a, b) => b.channels.length - a.channels.length)[0];
  const share = top && totalCh ? Math.round(100 * top.channels.length / totalCh) : 0;
  const row = (ico, name, val, warn, go) => `<div class="row${go ? ' tap' : ''}"${go ? ` onclick="${go}" role="button" tabindex="0"` : ''}>
      <div class="row-ico" style="background:var(--bg-${warn ? 'red' : 'blue'}-16)">${ico}</div>
      <div class="row-body"><div class="row-name">${name}</div><div class="row-val">${val}</div></div>${go ? '<div class="row-right">›</div>' : ''}</div>`;
  let html = '<div class="lst">';
  html += row('📊', 'Как устроена сеть',
    `${num(withCh.length)} из ${num(accs.length)} аккаунтов держат каналы · в среднем ${avg.toFixed(1).replace('.', ',')} на аккаунт`);
  // Риск концентрации: потеря одного аккаунта — потеря доли сети.
  if (top && share >= 40 && withCh.length > 1) {
    html += row('⚠️', 'Слишком много на одном аккаунте',
      `${esc(top.label)} держит ${num(top.channels.length)} ${plural(top.channels.length, 'канал', 'канала', 'каналов')} (${share}% сети) — потеря этого аккаунта ударит по всей сети. Добавьте других админов`,
      true, `openAccount(${Number(top.acc_id)})`);
  }
  if (noCh > 0) html += row('💤', 'Аккаунты без каналов', `${num(noCh)} — ими можно расширить сеть или сделать резервными админами`);
  if (TOPO.orphans > 0) html += row('🧩', 'Каналы без аккаунта-владельца',
    `${num(TOPO.orphans)} — их аккаунт удалён из флота, управлять ими сейчас нечем`, true, "goTab('channels')");
  html += '</div>';
  txt('topoInsights', html);
}
function topoView(v) {
  TOPO.view = v;
  document.querySelectorAll('#topoBody .filter-chips .chip').forEach(c => c.classList.toggle('on', c.dataset.v === v));
  _topoRender();
}
let _topoT = null;
function topoSearch(v) {
  clearTimeout(_topoT);
  _topoT = setTimeout(() => { TOPO.q = String(v || '').trim().toLowerCase(); _topoRender(); }, 200);
}
function topoToggle(accId) {
  TOPO.open[accId] = !TOPO.open[accId];
  _topoRender();
}
function _chName(c) { return c.title || (c.username ? '@' + c.username : '#' + c.channel_id); }
function _topoRender() {
  const q = TOPO.q;
  if (TOPO.view === 'bots') {
    const bots = TOPO.bots.filter(b => !q || String(b.label || '').toLowerCase().includes(q));
    txt('topoLinks', bots.length ? bots.map(b => `
      <div class="row tap" onclick="openBot(${Number(b.bot_id)})" role="button" tabindex="0">
        <div class="row-ico" style="background:var(--bg-purple-16)">🤖</div>
        <div class="row-body"><div class="row-name">${esc(b.label)}</div>
          <div class="row-val">${num(b.subscribers || 0)} ${plural(b.subscribers || 0, 'активный подписчик', 'активных подписчика', 'активных подписчиков')}</div></div>
        <div class="row-right">›</div>
      </div>`).join('') : empty('🤖', q ? 'Ничего не найдено' : 'Ботов нет', ''));
    return;
  }
  const accs = TOPO.accounts.map(a => {
    const chs = a.channels || [];
    if (!q) return {a, chs, hit: false};
    const self = String(a.label || '').toLowerCase().includes(q);
    const m = chs.filter(c => _chName(c).toLowerCase().includes(q) || String(c.username || '').toLowerCase().includes(q));
    return (self || m.length) ? {a, chs: self ? chs : m, hit: true} : null;
  }).filter(x => x && (x.chs.length || x.hit))
    .sort((x, y) => y.chs.length - x.chs.length);
  if (!accs.length) { txt('topoLinks', empty('🔍', 'Ничего не найдено', 'Поиск идёт по имени аккаунта, названию канала и @нику')); return; }
  txt('topoLinks', accs.map(({a, chs, hit}) => {
    const open = hit || TOPO.open[a.acc_id];
    const nCh = chs.filter(c => c.kind !== 'group').length;
    const nGr = chs.length - nCh;
    const parts = [];
    if (nCh) parts.push(`${num(nCh)} ${plural(nCh, 'канал', 'канала', 'каналов')}`);
    if (nGr) parts.push(`${num(nGr)} ${plural(nGr, 'группа', 'группы', 'групп')}`);
    if (!parts.length) parts.push('нет каналов');
    if (a.pool) parts.push(`пул «${esc(infraPoolLabel(a.pool))}»`);
    const list = open && chs.length ? `<div style="display:flex;flex-direction:column;gap:4px;margin-top:8px">` + chs.map(c => `
        <div class="tap" onclick="event.stopPropagation();openChannel(${Number(c.channel_id)})" role="button" tabindex="0"
             style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;background:var(--bg)">
          <span>${c.kind === 'group' ? '👥' : '📢'}</span>
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(_chName(c))}</span>
          ${c.members != null ? `<span style="font-size:12px;color:var(--hint)">${num(c.members)}</span>` : ''}
        </div>`).join('') + `</div>` : '';
    return `<div class="row" style="align-items:flex-start">
        <div class="row-ico tap" style="background:var(--bg-blue-16)" onclick="openAccount(${Number(a.acc_id)})" role="button" tabindex="0" title="Карточка аккаунта">📱</div>
        <div class="row-body tap" onclick="topoToggle(${Number(a.acc_id)})" role="button" tabindex="0">
          <div class="row-name">${esc(a.label)}${a.is_active === false ? ' <span class="badge b-gy">выключен</span>' : ''}</div>
          <div class="row-val">${parts.join(' · ')}</div>
          ${list}
        </div>
        ${chs.length ? `<div class="row-right" style="cursor:pointer" onclick="topoToggle(${Number(a.acc_id)})">${open ? '▾' : '▸'}</div>` : ''}
      </div>`;
  }).join(''));
}
