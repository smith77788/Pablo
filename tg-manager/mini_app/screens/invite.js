// ── Инвайтинг (screens/invite.js) ───────────────────────────────────────────
// Вынесено из index.html: монолит был 1.4 МБ одним файлом, и каждая правка в нём
// рискованнее правки в модуле. Инвайт — самая баноопасная операция продукта,
// поэтому вынесен первым.
//
// Границу кластера задают не имена, а данные: сюда входят все функции экрана
// плюс те, что читают его состояние INV_* (uploadInviteFile,
// selectInviteParseRun, startInviteFromParse). Иначе переменная осталась бы в
// одном файле, а её пользователи — в другом.
//
// Файл подключается ПОСЛЕ главного блока, поэтому свободно пользуется его
// глобалями (api, esc, push, txt, toast, num…). Обратные ссылки из главного
// блока идут либо из обработчиков onclick, либо через ленивую проверку
// `typeof openMassInvite === 'function'` — то есть в момент клика, когда файл
// уже загружен.
'use strict';

let INV_AUDIENCE = null;   // null = ещё не знаем / не применимо
let INV_FILE_PHONES = null;// предразобранный список из файла (phones)
let INV_FILE_REFS = null;  // предразобранный список из файла (user_refs)
let INV_IMPORT_OK = 0;     // распознанных целей в текущем списке (для подтверждения)
let INV_PARSE_RUN = null;   // предвыбранный parse_run_id при заходе «из парсера»

function buildInvitePresets() {
  const row = document.getElementById('invitePresetRow');
  if (!row || row.dataset.built) return;
  row.innerHTML = INVITE_PRESETS.map(p =>
    `<span class="preset-chip" onclick="invitePreset('${p.k}')">${esc(p.t)}</span>`).join('');
  row.dataset.built = '1';
}

function startInviteFromParse(runId) {
  const r = (PARSER_RUNS||[]).find(x=>x.id===runId) || {};
  openMassInvite();               // сброс (в т.ч. INV_PARSE_RUN=null)
  INV_PARSE_RUN = runId;
  const s = document.getElementById('massInviteSrc');
  if (s) { s.value = 'parsed'; massInviteSrcToggle(); }
  toast('➕ Инвайт аудитории: '+(r.source||'парсер')+' · '+num(r.total_saved||0)+' — укажите группу');
}

async function loadInviteRetention() {
  try {
    const d = await api('/api/miniapp/invite/retention');
    const el = document.getElementById('invRetention');
    if (!el) return;
    if (d.retention_pct == null) {
      el.innerHTML = '<div style="font-size:13px;color:var(--hint)">Нет вступлений за период — ретеншен появится после инвайтов.</div>';
      return;
    }
    const col = d.health==='green'?'var(--green)':d.health==='amber'?'var(--accent)':'var(--red)';
    el.innerHTML = `<div style="display:flex;gap:8px;flex-wrap:wrap">
      <div class="kpi-chip"><div class="kpi-chip-val" style="color:${col}">${d.retention_pct}%</div><div class="kpi-chip-lbl">остались</div></div>
      <div class="kpi-chip"><div class="kpi-chip-val">${num(d.joined)}</div><div class="kpi-chip-lbl">вступили</div></div>
      <div class="kpi-chip"><div class="kpi-chip-val" style="color:var(--red)">${num(d.left)}</div><div class="kpi-chip-lbl">ушли</div></div>
    </div><div style="font-size:11px;color:var(--hint);padding:4px 2px">Отток считается по чатам под модерацией бота.</div>`;
  } catch(e) {}
}

async function loadInviteAnalytics() {
  try {
    const d = await api('/api/miniapp/invite/analytics');
    if (!d.has_data) {
      txt('invAnalytics', '<div style="font-size:13px;color:var(--hint);padding:4px 2px">Пока нет истории инвайтов — статистика появится после первого запуска.</div>');
      return;
    }
    const t = d.today || {};
    // Конверсия null при нуле попыток: честнее показать «—», чем 0%.
    const conv = (t.conversion_pct === null || t.conversion_pct === undefined) ? '—' : t.conversion_pct + '%';
    const floodCls = (t.floods || 0) > 0 ? 'var(--red)' : 'var(--green)';
    // Стабильные 7 столбцов: недостающие дни дополняем пустыми слева, иначе
    // 1–2 дня с данными растягивались во всю ширину («кривой» график из 2 полос).
    const wk = (d.week || []).slice(-7);
    while (wk.length < 7) wk.unshift({ day: '', ok: 0, floods: 0, empty: true });
    const maxOk = Math.max(1, ...wk.map(w => +w.ok || 0));
    const hasWeek = wk.some(w => (+w.ok || 0) > 0 || (+w.floods || 0) > 0);
    const bars = wk.map(w => {
      const h = w.empty ? 0 : Math.max(3, Math.round((+w.ok || 0) / maxOk * 34));
      const c = (+w.floods || 0) > 0 ? 'var(--red)' : 'var(--accent)';
      const tip = w.empty ? '' : `${esc(w.day)}: ${w.ok} инвайтов, флудов ${w.floods}`;
      return `<div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center" title="${tip}">
        <div style="width:60%;max-width:14px;height:${h}px;background:${c};border-radius:2px 2px 0 0"></div></div>`;
    }).join('');
    txt('invAnalytics', `
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <div class="kpi-card"><div class="kpi-val" style="color:var(--green)">${num(t.ok||0)}</div><div class="kpi-lbl">Успешно</div></div>
        <div class="kpi-card"><div class="kpi-val" style="color:${floodCls}">${num(t.floods||0)}</div><div class="kpi-lbl">Флудов</div></div>
        <div class="kpi-card"><div class="kpi-val">${conv}</div><div class="kpi-lbl">Конверсия</div></div>
      </div>
      ${hasWeek ? `<div style="display:flex;gap:4px;align-items:flex-end;height:38px;margin-bottom:6px">${bars}</div>
      <div style="font-size:11px;color:var(--hint);margin-bottom:8px">7 дней · красный столбец = были флуды</div>` : ''}
      ${d.best ? `<div style="font-size:12px;color:var(--hint)">🏅 Лучший: ${esc(d.best.label)} — ${d.best.ok} инвайтов, флудов ${d.best.floods}</div>` : ''}
      ${d.worst && (+d.worst.floods > 0) ? `<div style="font-size:12px;color:var(--red);margin-top:2px">⚠️ Проблемный: ${esc(d.worst.label)} — флудов ${d.worst.floods}</div>` : ''}
    `);
  } catch(e) {
    txt('invAnalytics', `<div style="font-size:13px;color:var(--hint);padding:4px 2px">Статистика недоступна · <span onclick="loadInviteAnalytics()" style="color:var(--accent);cursor:pointer">повторить</span></div>`);
  }
}

async function loadInviteAdvice() {
  try {
    const d = await api('/api/miniapp/invite/advice');
    const adv = d.advice || [];
    if (!adv.length) {
      // Пустой разбор — тоже ответ, но он должен отличаться от «нет данных».
      txt('invAdvice', `<div style="font-size:13px;color:var(--hint);padding:4px 2px">${
        d.has_data
          ? '✅ Поводов вмешаться нет — проверено: ' + (d.checked || []).join(', ') + '.'
          : 'Разбор появится после первого инвайта — пока не на чем считать.'
      }</div>`);
      return;
    }
    txt('invAdvice', adv.map(a => {
      const [col, bg, ico] = ADV_STYLE[a.severity] || ADV_STYLE.info;
      const accs = (a.accounts || []).map(x =>
        `<span onclick="openInviteAccount(${x.id})" style="color:var(--accent);cursor:pointer">${esc(x.label)}</span>`
      ).join(', ');
      return `<div style="background:${bg};border-left:3px solid ${col};border-radius:8px;padding:8px 10px;margin-bottom:6px">
        <div style="font-size:13px;font-weight:600;color:${col}">${ico} ${esc(a.title)}</div>
        <div style="font-size:12px;color:var(--hint);margin-top:3px;line-height:1.45">${esc(a.detail)}</div>
        ${accs ? `<div style="font-size:12px;margin-top:5px">${accs}</div>` : ''}
      </div>`;
    }).join(''));
  } catch (e) {
    txt('invAdvice', `<div style="font-size:13px;color:var(--hint);padding:4px 2px">Разбор недоступен · <span onclick="loadInviteAdvice()" style="color:var(--accent);cursor:pointer">повторить</span></div>`);
  }
}

async function openInviteAccount(accId) {
  push('s-invacc');
  txt('invAccBody', '<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const d = await api('/api/miniapp/invite/account/' + accId);
    const w = d.week || {};
    const kpi = (v, l, c) => `<div class="kpi-card"><div class="kpi-val"${c?` style="color:${c}"`:''}>${v}</div><div class="kpi-lbl">${l}</div></div>`;
    const age = d.age_days == null ? '—' : num(d.age_days) + ' дн.';
    const lim = d.recommended_limit == null ? '—' : num(d.recommended_limit);
    const rem = d.remaining_today == null ? '—' : num(d.remaining_today);
    const pause = d.recommended_pause_s == null ? '—' : d.recommended_pause_s + ' с';
    const notes = ((d.limit_factors || {}).notes) || [];
    txt('invAccBody', `
      <div style="font-size:16px;font-weight:600;margin-bottom:8px">${esc(d.label || ('#'+accId))}</div>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        ${kpi(lim, 'Лимит/сутки')}
        ${kpi(rem, 'Осталось', (d.remaining_today === 0 ? '#f59e0b' : ''))}
        ${kpi(pause, 'Пауза')}
      </div>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        ${kpi(num(w.ok || 0), 'Успешно / 7дн', '#22c55e')}
        ${kpi(num(w.floods || 0), 'Флудов / 7дн', (w.floods ? '#ef4444' : ''))}
        ${kpi(age, 'Возраст')}
      </div>
      ${d.limit_basis ? `<div style="background:var(--bg-select);border-radius:8px;padding:9px 10px;margin-bottom:8px">
        <div style="font-size:12px;color:var(--hint);margin-bottom:3px">Почему такой лимит</div>
        <div style="font-size:13px;line-height:1.45">${esc(d.limit_basis)}</div>
      </div>` : ''}
      ${notes.length ? `<div style="background:var(--bg-select);border-radius:8px;padding:9px 10px;margin-bottom:8px">
        <div style="font-size:12px;color:var(--hint);margin-bottom:4px">Что учтено в аккаунте</div>
        ${notes.map(n=>`<div style="font-size:13px;line-height:1.5">• ${esc(n)}</div>`).join('')}
      </div>` : ''}
      <div style="font-size:12px;color:var(--hint);line-height:1.5;padding:2px">
        Лимит и пауза считаются движком по фактам этого аккаунта и не задаются
        вручную: ручное число одно на всю операцию и не знает, что один аккаунт
        год работает чисто, а другой словил флуд вчера.
      </div>
    `);
  } catch (e) {
    txt('invAccBody', empty('⚠️','Не удалось загрузить карточку', (e.message||'').substring(0,80)));
  }
}

async function openMassInvite() {
  push('s-massinvite');
  INV_PARSE_RUN = null;
  loadGovernorBar();
  loadInviteAnalytics();
  loadInviteRetention();
  loadInviteAdvice();
  buildInvitePresets();
  txt('massInviteHistory','<div class="spin-wrap"><div class="spin"></div></div>');
  ['massInviteImport','massInviteBatch','massInviteMax','massInvitePerAcc','massInviteWelcome'].forEach(id=>{const e=document.getElementById(id); if(e) e.value='';});
  ['massInviteRights','massInviteListPreview','massInviteReadiness'].forEach(id=>{const e=document.getElementById(id); if(e) e.innerHTML='';});
  INV_IMPORT_OK = 0;
  INV_FILE_REFS = null; INV_FILE_PHONES = null;
  massInviteSrcToggle();
  // Заглушка «Загрузка…» лежит ВНУТРИ massInviteAccsWrap, и ниже по функции
  // wrap.innerHTML затирает её насовсем. Поэтому при втором открытии экрана
  // getElementById возвращал null, присваивание падало — а падало оно ДО
  // await, то есть аккаунты-инвайтеры и история дальше не грузились вообще.
  // Пересоздаём заглушку целиком: состояние одинаково на каждом открытии.
  const accsWrapEl = document.getElementById('massInviteAccsWrap');
  if (accsWrapEl) accsWrapEl.innerHTML =
    '<div style="font-size:12px;color:var(--hint)" id="massInviteAccsLoad">Загрузка аккаунтов…</div>';
  const [opsD, accsD] = await Promise.allSettled([
    api('/api/miniapp/operations'),
    api('/api/miniapp/accounts'),
  ]);
  // Render account checkboxes
  const accs = (accsD.status==='fulfilled' ? (accsD.value.accounts||[]) : []).filter(a=>a.is_active);
  const wrap = document.getElementById('massInviteAccsWrap');
  if (accs.length) {
    // Проактивный риск-пульс: помечаем аккаунты, которые исполнитель пропустит
    // для защиты от бана (карантин/под риском) — чтобы пользователь видел ДО запуска.
    const riskBadge = a => a.health_status==='quarantine'
        ? ' <span title="На паузе (риск-пульс) — будет пропущен для защиты" style="color:#ef4444">🛑</span>'
        : a.health_status==='at_risk'
        ? ' <span title="Под риском — будет пропущен для защиты" style="color:#f59e0b">⚠️</span>' : '';
    // «ⓘ» открывает карточку: почему движок даёт этому аккаунту такой лимит и
    // такую паузу. Кнопка вынесена из <label>, иначе тап по ней переключал бы
    // чекбокс вместо перехода.
    wrap.innerHTML = accs.map(a=>`<div style="display:flex;align-items:center;gap:6px;padding:3px 4px;font-size:13px">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer;flex:1;min-width:0">
        <input type="checkbox" value="${a.id}" style="accent-color:var(--btn)">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.first_name||a.phone)} <span style="color:var(--hint)">${a.username?'@'+esc(a.username):''}</span>${riskBadge(a)}</span>
      </label>
      <span onclick="openInviteAccount(${a.id})" title="Лимит, пауза и риск этого аккаунта" style="color:var(--accent);cursor:pointer;padding:0 4px;flex:none">ⓘ</span>
    </div>`).join('');
    const flagged = accs.filter(a=>a.health_status==='quarantine'||a.health_status==='at_risk').length;
    if (flagged) {
      wrap.insertAdjacentHTML('afterbegin', `<div style="font-size:12px;color:var(--hint);padding:2px 4px 6px;border-bottom:1px solid var(--sep);margin-bottom:4px">🛡 ${flagged} аккаунт(ов) под риск-пульсом — их пропустят для защиты от бана</div>`);
    }
  } else {
    wrap.innerHTML = '<div style="font-size:12px;color:var(--hint);padding:4px">Нет активных аккаунтов</div>';
  }
  // Render history
  if (opsD.status==='fulfilled') {
    const ops = (opsD.value.operations||[]).filter(o=>o.op_type==='mass_invite').slice(0,10);
    if (!ops.length) { txt('massInviteHistory', empty('📨','Нет истории инвайтов','')); return; }
    txt('massInviteHistory', ops.map(o=>{
      const pct = o.total_items>0?Math.round((o.done_items||0)/o.total_items*100):0;
      const [bc,bl]=stb(o.status);
      return `<div class="row">
        <div class="row-ico" style="background:var(--bg-blue-16)">📨</div>
        <div class="row-body">
          <div class="row-name">${esc(o.label||'mass_invite')}</div>
          <div class="row-val"><span class="badge ${bc}">${bl}</span> · ${o.done_items||0}/${o.total_items||0}</div>
          ${o.status==='running'?`<div class="prog" style="margin-top:4px"><div class="prog-fill" style="width:${pct}%"></div></div>`:''}
        </div>
      </div>`;
    }).join(''));
  } else {
    txt('massInviteHistory', empty('⚠️','Ошибка',opsD.reason?.message||''));
  }
}

function massInviteMethodToggle() {
  // Поле текста нужно только методу «ссылка в ЛС»; остальным оно ничего не
  // значит, а лишнее поле на экране запуска читается как настройка, которая
  // на что-то влияет.
  const m = document.getElementById('massInviteMethod')?.value;
  const f = document.getElementById('massInviteLinkMsgField');
  if (f) f.style.display = (m === 'link') ? '' : 'none';
}

function massInviteSrcToggle() {
  const src = document.getElementById('massInviteSrc').value;
  const f = document.getElementById('massInviteImportField');
  if (f) f.style.display = (src==='import_list') ? 'block' : 'none';
  const sf = document.getElementById('massInviteSegmentField');
  if (sf) sf.style.display = (src==='segment') ? 'block' : 'none';
  if (src==='segment') loadInviteSegments();
  const pf = document.getElementById('massInviteParseRunField');
  if (pf) pf.style.display = (src==='parsed') ? 'block' : 'none';
  if (src==='parsed') loadInviteParseRuns();
  loadInviteAudienceSize();
  if (src==='import_list') massInviteListPreview();
}

async function loadInviteParseRuns() {
  const sel = document.getElementById('massInviteParseRun');
  if (!sel) return;
  try {
    const d = await api('/api/miniapp/parser/runs');
    const runs = (d.runs||[]).filter(r=>(r.total_saved||0) > 0);
    const cur = INV_PARSE_RUN ? String(INV_PARSE_RUN) : '';
    sel.innerHTML = '<option value="">— вся аудитория парсера —</option>' +
      runs.map(r=>`<option value="${r.id}"${String(r.id)===cur?' selected':''}>${esc(r.source||('запуск #'+r.id))} (${num(r.total_saved)})</option>`).join('');
  } catch(e) {}
}

function selectInviteParseRun() {
  const v = document.getElementById('massInviteParseRun')?.value;
  INV_PARSE_RUN = v ? parseInt(v) : null;
  loadInviteAudienceSize();
}

async function loadInviteSegments() {
  const sel = document.getElementById('massInviteSegment');
  if (!sel || sel.dataset.loaded) return;
  let html = '<option value="">— весь список контактов —</option>';
  try {
    const o = await api('/api/miniapp/invite/segment_options');
    if (o && o.favorites) html += `<option value="fav">⭐ Избранные (${o.favorites})</option>`;
    (o && o.tags || []).forEach(t => {
      html += `<option value="tag:${esc(t.tag)}">🏷 ${esc(t.tag)} (${t.count})</option>`;
    });
  } catch(e) {}
  try {
    const d = await api('/api/miniapp/uch/segments');
    const segs = d.segments||d||[];
    html += (Array.isArray(segs)?segs:[]).map(s=>`<option value="${s.id}">${esc(s.name)}${s.count!=null?` (${s.count})`:''}</option>`).join('');
  } catch(e) {}
  sel.innerHTML = html;
  sel.dataset.loaded = '1';
}

function massInviteListPreview() {
  const ta = document.getElementById('massInviteImport');
  const el = document.getElementById('massInviteListPreview');
  if (!ta || !el) return;
  // Ручной ввод отменяет ранее загруженный файл.
  INV_FILE_REFS = null; INV_FILE_PHONES = null;
  const raw = ta.value.trim();
  if (!raw) { el.textContent=''; INV_IMPORT_OK = 0; return; }
  clearTimeout(_invPreviewT);
  _invPreviewT = setTimeout(async () => {
    try {
      const d = await api('/api/miniapp/invite/parse_list', {method:'POST', body:JSON.stringify({import_list: raw, group: _invGroup()})});
      INV_IMPORT_OK = d.total || 0;
      const parts = [];
      if (d.phones) parts.push(`📞 номеров: <b>${num(d.phones)}</b>`);
      if (d.user_refs) parts.push(`👤 @username/ID: <b>${num(d.user_refs)}</b>`);
      let html = parts.length
        ? `<span style="color:var(--green)">✓ распознано ${d.total}</span> · ${parts.join(' · ')}`
        : `<span style="color:var(--red)">Ничего не распознано</span>`;
      html += _invDedupLine(d);
      if (d.unrecognized_count) {
        const sample = (d.unrecognized||[]).slice(0,5).map(esc).join(', ');
        html += `<div style="color:var(--orange,#e6a100);margin-top:2px">⚠️ не распознано строк: ${d.unrecognized_count}${sample?` (${sample}${d.unrecognized_count>5?'…':''})`:''}</div>`;
      }
      el.innerHTML = html;
    } catch(e) { el.textContent=''; }
  }, 350);
}

async function uploadInviteFile() {
  const inp = document.getElementById('massInviteFile');
  const el = document.getElementById('massInviteListPreview');
  if (!inp || !inp.files || !inp.files[0]) return;
  const f = inp.files[0];
  if (el) el.innerHTML = '<span style="color:var(--hint)">Читаю файл…</span>';
  try {
    const fd = new FormData();
    fd.append('file', f);
    fd.append('group', _invGroup());
    const d = await api('/api/miniapp/invite/parse_file', {method:'POST', body: fd});
    INV_FILE_REFS = d.user_refs || [];
    INV_FILE_PHONES = d.phones || [];
    INV_IMPORT_OK = d.total || 0;
    const parts = [];
    if (INV_FILE_PHONES.length) parts.push(`📞 номеров: <b>${num(INV_FILE_PHONES.length)}</b>`);
    if (INV_FILE_REFS.length) parts.push(`👤 @username/ID: <b>${num(INV_FILE_REFS.length)}</b>`);
    let html = `<span style="color:var(--green)">📎 из файла «${esc(f.name)}»: ${d.total}</span>${parts.length?' · '+parts.join(' · '):''}`;
    html += _invDedupLine(d);
    if (el) el.innerHTML = html;
    const ta = document.getElementById('massInviteImport');
    if (ta) ta.value = '';   // файл заменяет ручной ввод
    toast('Файл загружен: '+d.total);
  } catch(e) {
    INV_FILE_REFS = null; INV_FILE_PHONES = null;
    if (el) el.innerHTML = '<span style="color:var(--red)">'+esc(e.message||'Ошибка чтения файла')+'</span>';
  } finally { inp.value=''; }
}

async function loadInviteAudienceSize() {
  const el = document.getElementById('massInviteAudience');
  if (!el) return;
  const src = document.getElementById('massInviteSrc').value;
  if (src === 'import_list') { INV_AUDIENCE = null; el.textContent=''; return; }
  el.textContent = 'Считаю аудиторию…';
  try {
    let url = '/api/miniapp/invite/audience?source=' + encodeURIComponent(src);
    if (src === 'parsed' && INV_PARSE_RUN) url += '&parse_run_id=' + INV_PARSE_RUN;
    if (src === 'segment') {
      const s = _inviteSegSel();
      if (s.saved_segment_id) url += '&saved_segment_id=' + s.saved_segment_id;
      else if (s.segment_filters) url += '&segment_filters=' + encodeURIComponent(JSON.stringify(s.segment_filters));
    }
    const d = await api(url);
    INV_AUDIENCE = (d.total === null || d.total === undefined) ? null : +d.total;
    if (INV_AUDIENCE === null) { el.textContent = ''; return; }
    if (!INV_AUDIENCE) {
      // Пустой источник — не тупик: говорим, откуда взять аудиторию.
      el.innerHTML = `<span style="color:#f59e0b">⚠️ Источник пуст — приглашать некого.</span> ${esc(d.hint||'')}`;
      return;
    }
    const capped = d.capped_at && INV_AUDIENCE > d.capped_at
      ? ` · за прогон возьмём ${num(d.capped_at)}` : '';
    el.innerHTML = `👥 Доступно целей: <b>${num(INV_AUDIENCE)}</b>${capped}`;
    loadInvitePreflight(INV_AUDIENCE);
  } catch(e) {
    // Не знаем — молчим. Ноль здесь означал бы «пусто» и оттолкнул бы от запуска
    // рабочей операции.
    INV_AUDIENCE = null; el.textContent = '';
  }
}

async function massInviteGrantAdmin() {
  const group = document.getElementById('massInviteGroup')?.value.trim();
  if (!group) { toast('Укажите группу'); return; }
  if (!await askConfirm('Выдать всем вашим аккаунтам право приглашать в «'+group+'»?\n\nНужен аккаунт-админ чата с правом «Назначать администраторов» — он назначит остальных.')) return;
  try {
    const r = await api('/api/miniapp/invite/grant_admin', {method:'POST', body:JSON.stringify({group})});
    if (r.no_promoter) { toast('⚠️ '+(r.message||'Нет аккаунта-админа чата')); return; }
    toast('🛡 Выдаю права инвайтерам (операция #'+r.op_id+')');
    setTimeout(checkInviteRights, 1500);  // обновим баннер после запуска
  } catch(e) { toast('⚠️ '+(e.message||'Ошибка')); }
}

async function loadInvitePreflight(audience) {
  const el = document.getElementById('massInvitePreflight');
  if (!el) return;
  el.innerHTML = '<span style="color:var(--hint)">Проверяю флот…</span>';
  try {
    const _op = document.getElementById('massInviteOnePass')?.checked ? '&one_pass=1' : '';
    const d = await api('/api/miniapp/invite/preflight?audience=' + (audience||0) + _op);
    const a = d.accounts || {};
    const passLine = (audience && d.est_passes)
      ? (d.one_pass
          ? '<span style="color:var(--green)">✅ Уйдёт за один проход</span>'
          : `<span style="color:var(--orange,#e6a100)">🔁 ~${d.est_passes} прохода(ов)</span> (суточный лимит флота ~${num(d.per_pass||0)})`)
      : '';
    const parts = [];
    parts.push(`🛡 Пригодно аккаунтов: <b>${num(d.usable||0)}</b> из ${num(a.total||0)}`);
    if (a.with_proxy) parts.push(`🔒 с прокси: ${num(a.with_proxy)}`);
    if (a.direct) parts.push(`🌐 без прокси: ${num(a.direct)}`);
    if (a.proxy_broken) parts.push(`🚫 прокси битый: ${num(a.proxy_broken)}`);
    if (a.quarantined) parts.push(`⏸ на отдыхе: ${num(a.quarantined)}`);
    const warns = (d.warnings||[]).map(w =>
      `<div style="margin-top:4px;padding:6px 8px;background:var(--card-2,rgba(255,180,0,.08));border-radius:8px;line-height:1.4">${esc(w)}</div>`
    ).join('');
    el.innerHTML =
      `<div style="line-height:1.5">${parts.join(' · ')}` + (passLine?` · ${passLine}`:'') + `</div>` + warns;
  } catch(e) {
    el.innerHTML = '';  // не мешаем запуску, если проверка недоступна
  }
}

async function submitMassInvite() {
  const group = document.getElementById('massInviteGroup').value.trim();
  const source = document.getElementById('massInviteSrc').value;
  const errEl = document.getElementById('massInviteErr');
  if (!group) { errEl.textContent='Укажите группу'; return; }
  if (INV_AUDIENCE === 0 && source !== 'import_list') {
    errEl.textContent = 'Источник пуст — приглашать некого. Смените источник или соберите аудиторию.';
    return;
  }
  const body = {group, source};
  if (document.getElementById('invSafeMode')?.checked) body.safe_mode = true;
  if (document.getElementById('invDaughterGroups')?.checked) body.use_daughter_groups = true;
  // Витрина осмысленна только вместе с дочерними группами: сама по себе она
  // добавляет звено, но не темп. Исполнитель требует обе, здесь не мешаем
  // отправить — он же и рассудит, чтобы правило жило в одном месте.
  if (document.getElementById('invShowcase')?.checked) body.use_showcase = true;
  // Пришли «из парсера» — инвайтим ИМЕННО тот запуск (backend фильтрует по
  // parse_run_id в исполнителе). Иначе source=parsed брал бы всю аудиторию.
  if (source==='parsed' && INV_PARSE_RUN) body.parse_run_id = INV_PARSE_RUN;
  if (source==='segment') {
    // Тот же разбор, что и в превью аудитории: весь список / избранные / тег /
    // сохранённый сегмент. Иначе показанное число разойдётся с приглашёнными.
    const s = _inviteSegSel();
    if (s.saved_segment_id) body.saved_segment_id = s.saved_segment_id;
    else if (s.segment_filters) body.segment_filters = s.segment_filters;
  }
  if (source==='import_list') {
    // Список из файла (уже разобран) имеет приоритет над текстом.
    if ((INV_FILE_REFS && INV_FILE_REFS.length) || (INV_FILE_PHONES && INV_FILE_PHONES.length)) {
      if (INV_FILE_REFS && INV_FILE_REFS.length) body.user_refs = INV_FILE_REFS;
      if (INV_FILE_PHONES && INV_FILE_PHONES.length) body.phones = INV_FILE_PHONES;
      INV_IMPORT_OK = (INV_FILE_REFS?.length||0) + (INV_FILE_PHONES?.length||0);
    } else {
      const imp = document.getElementById('massInviteImport').value.trim();
      if (!imp) { errEl.textContent='Вставьте список или загрузите файл для инвайта'; return; }
      body.import_list = imp;
      // Свежий разбор перед запуском: не даём стартовать по нераспознанному списку.
      try {
        const chk = await api('/api/miniapp/invite/parse_list', {method:'POST', body:JSON.stringify({import_list: imp})});
        if (!chk.total) { errEl.textContent='В списке не распознано ни одной цели — проверьте формат (номер с «+» или 11+ цифр, @username или ID).'; return; }
        INV_IMPORT_OK = chk.total;
      } catch(e) {}
    }
  }
  const checked = [...document.querySelectorAll('#massInviteAccsWrap input[type=checkbox]:checked')].map(c=>parseInt(c.value));
  if (checked.length) body.account_ids = checked;
  body.pace = document.getElementById('massInvitePace').value;
  const _b = document.getElementById('massInviteBatch').value.trim();
  const _m = document.getElementById('massInviteMax').value.trim();
  const _pa = document.getElementById('massInvitePerAcc').value.trim();
  if (_b) body.batch_size = parseInt(_b);
  if (_m) body.max_invites = parseInt(_m);
  if (_pa) body.per_account_limit = parseInt(_pa);
  const _onePass = document.getElementById('massInviteOnePass')?.checked;
  if (_onePass) body.one_pass = true;
  // Паритет с ботом: способ инвайта + режим объёма.
  body.invite_method = document.getElementById('massInviteMethod')?.value || 'direct';
  // Свой текст для «ссылки в ЛС»: без него весь флот шлёт одну и ту же фразу.
  if (body.invite_method === 'link') {
    const _lm = (document.getElementById('massInviteLinkMsg')?.value||'').trim();
    if (_lm) body.link_message = _lm;
  }
  // Повторный инвайт: итог операции прямо советует «отключите дедуп» —
  // до этого контрола для такого не было ни на одной поверхности.
  if (document.getElementById('massInviteReinvite')?.checked) body.skip_invited = false;
  // Обе механики МЕНЯЮТ состав админов чата и включены по умолчанию — отправляем
  // явно, чтобы снятая галочка действительно выключала их, а не молча теряла.
  if (document.getElementById('massInviteAutoPromote') &&
      !document.getElementById('massInviteAutoPromote').checked) body.auto_promote = false;
  if (document.getElementById('massInvitePromoteTrick') &&
      !document.getElementById('massInvitePromoteTrick').checked) body.promote_trick = false;
  const _vm = document.getElementById('massInviteVolMode')?.value;
  if (_vm === 'progressive') body.volume_mode = 'progressive';
  // Шов «Инвайт → Welcome»: приветствие вступившим одной транзакцией.
  const _wm = (document.getElementById('massInviteWelcome')?.value||'').trim();
  if (_wm) body.welcome_message = _wm;
  // A/B приветствие: собрать варианты (A = основное поле, B/C — доп.).
  const _wab = document.getElementById('miWelcomeAbOn');
  if (_wab && _wab.checked) {
    const _wv = [_wm,
      (document.getElementById('miWelcomeVarB').value||'').trim(),
      (document.getElementById('miWelcomeVarC').value||'').trim()].filter(Boolean);
    if (_wv.length >= 2) body.welcome_variants = _wv;
  }
  // Фильтры аудитории (только с username / не бот / premium / активные) —
  // применяются в исполнителе теми же условиями, что парсер-вью.
  const _af = {};
  if (document.getElementById('invFltUsername')?.checked) _af.with_username = true;
  if (document.getElementById('invFltNotBot')?.checked) _af.not_bot = true;
  if (document.getElementById('invFltPremium')?.checked) _af.premium = true;
  if (document.getElementById('invFltActive')?.checked) _af.active = true;
  if (Object.keys(_af).length) body.aud_filters = _af;
  // Инвайт — самая баноопасная операция → подтверждение перед запуском.
  // Без лимита на аккаунт риск бана максимальный — предупреждаем явно.
  const _paceLbl = {auto:'🤖 авто (по состоянию флота)', slow:'🐢 медленно', normal:'⚖️ обычный', fast:'⚡ быстро'}[body.pace] || body.pace;
  const _warn = _pa ? '' : '\n\n⚠️ Лимит на аккаунт не задан — риск бана выше. Задайте его в «⚙️ Лимиты».';
  const _opWarn = _onePass ? '\n\n🚀 Режим «один проход»: аккаунты работают до потолка ёмкости (≈50/аккаунт) по живым сигналам флуда, без предсказанного суточного лимита. Это ЗАМЕТНО выше риск бана — используйте на прогретых аккаунтах с прокси.' : '';
  const _tgtLine = (source==='import_list' && INV_IMPORT_OK) ? '\nЦелей в списке: '+INV_IMPORT_OK+'.' : '';
  if (!(await askConfirm('Запустить массовый инвайт в «'+group+'»?\nТемп: '+_paceLbl+'.'+_tgtLine+_warn+_opWarn+'\n\nСовет: начните с малого лимита на аккаунт, проверьте результат в истории, затем масштабируйте.'))) return;
  const btn = document.getElementById('massInviteBtn');
  btn.disabled=true; btn.textContent='Запускаю…';
  try {
    const r = await api('/api/miniapp/mass_invite',{method:'POST',body:JSON.stringify(body)});
    toast(`📨 Инвайт запущен (#${r.op_id})`);
    errEl.textContent='';
    openMassInvite();
  } catch(e) { errEl.textContent=e.message||'Ошибка'; }
  finally { btn.disabled=false; btn.textContent='🚀 Запустить инвайт'; }
}
