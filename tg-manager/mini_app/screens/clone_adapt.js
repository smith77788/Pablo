// «Клон и адаптация»: оформление одного бота → на другие, с переработкой.
//
// Классический скрипт: грузится после основного блока index.html и делит с
// ним глобальную область. Состояние (CA_ROWS, CA_OFFSET, CA_FILTER, CLA)
// объявлено в index.html. Сервер — services/mini_app_clone_adapt.py, движок —
// services/clone_adapt_engine.py.
//
// Было: окно с одной целью, пятью галочками (из них «Фото» не делала ничего)
// и историей «Поля: name,desc · ✓ Готово». Стало: много целей, адаптация
// текста, языки, предпросмотр «было → станет», откат и повтор из истории.

const CLA_FIELDS = [
  ['name', 'Имя'], ['desc', 'Описание'], ['short', 'Краткое описание'],
  ['commands', 'Команды'], ['photo', 'Фото'], ['menu', 'Кнопка меню'],
  ['rights', 'Права по умолчанию'],
];
const CLA_ST = {
  ok: ['b-bl', '✓ Изменено'], same: ['b-gy', '= Уже совпадало'],
  partial: ['b-or', '⚠ Частично'], error: ['b-rd', '✕ Ошибка'],
};

function claBotLabel(b) {
  return b.username ? '@' + b.username : (b.first_name || ('бот ' + b.bot_id));
}

// ── История ────────────────────────────────────────────────────────────────

function claSetFilter(f) {
  CA_FILTER = f || '';
  document.querySelectorAll('#claFilter .chip').forEach(el =>
    el.classList.toggle('on', (el.dataset.f || '') === CA_FILTER));
  openCloneAdapt(false, true);
}

async function openCloneAdaptMore() { CA_OFFSET = CA_ROWS.length; await openCloneAdapt(true); }

async function openCloneAdapt(keep, stay) {
  if (!keep) {
    CA_ROWS = []; CA_OFFSET = 0;
    if (!stay) { CA_FILTER = ''; push('s-cloneadapt');
      document.querySelectorAll('#claFilter .chip').forEach(el =>
        el.classList.toggle('on', !(el.dataset.f || ''))); }
    txt('caHistory', '<div class="spin-wrap"><div class="spin"></div></div>');
  }
  try {
    const q = 'limit=30&offset=' + CA_OFFSET + (CA_FILTER ? '&status=' + encodeURIComponent(CA_FILTER) : '');
    const d = await api('/api/miniapp/clone_adapt/history?' + q);
    const page = listOf(d, 'items');
    CA_ROWS = CA_OFFSET ? CA_ROWS.concat(page) : page;
    const rows = CA_ROWS;
    const total = totalOf(d, rows);
    const st = d.stats || {};
    txt('claStats', st.total ? `<div class="mgmt-grid" style="grid-template-columns:repeat(3,1fr)">
        <div class="mgmt-tile"><div class="mgmt-tile-val">${num(st.runs || 0)}</div><div class="mgmt-tile-lbl">${plural(st.runs || 0, 'запуск', 'запуска', 'запусков')}</div></div>
        <div class="mgmt-tile" onclick="claSetFilter('')"><div class="mgmt-tile-val">${num(st.ok || 0)}</div><div class="mgmt-tile-lbl">ботов приведено к образцу</div></div>
        <div class="mgmt-tile" onclick="claSetFilter('problem')"><div class="mgmt-tile-val" style="${st.problem ? 'color:var(--red)' : ''}">${num(st.problem || 0)}</div><div class="mgmt-tile-lbl">с ошибками</div></div>
      </div>` : '');
    if (!rows.length) {
      txt('caHistory', CA_FILTER
        ? empty('🔎', 'В этом срезе записей нет', 'Снимите фильтр, чтобы увидеть всю историю.', {label: 'Показать все', fn: "claSetFilter('')"})
        : empty('🔄', 'Клонов ещё не было',
          'Выберите бота-образец и боты, которым нужно такое же оформление. До запуска покажем, что изменится у каждого.',
          {label: '+ Новый клон', fn: 'openCloneModal()'}));
      return;
    }
    // Группируем по запуску (операции): один клон на 20 ботов — одна карточка.
    const groups = [];
    const byKey = {};
    rows.forEach(r => {
      const key = r.op_id ? 'op' + r.op_id : 'row' + r.id;
      if (!byKey[key]) { byKey[key] = {key, op_id: r.op_id, rows: []}; groups.push(byKey[key]); }
      byKey[key].rows.push(r);
    });
    txt('caHistory', groups.map(claGroupHtml).join('') + moreBtn(rows.length, total, 'openCloneAdaptMore()'));
  } catch (e) { txt('caHistory', errHtml(errRu(e), 'openCloneAdapt()')); }
}

function claGroupHtml(g) {
  const first = g.rows[0];
  const src = first.source_uname ? '@' + first.source_uname : (first.source_name || ('бот ' + first.source_bot_id));
  const cnt = {ok: 0, same: 0, problem: 0};
  g.rows.forEach(r => {
    if (r.status === 'ok') cnt.ok++; else if (r.status === 'same') cnt.same++; else cnt.problem++;
  });
  const parts = [];
  if (cnt.ok) parts.push('изменено ' + num(cnt.ok));
  if (cnt.same) parts.push('совпадало ' + num(cnt.same));
  if (cnt.problem) parts.push('<span style="color:var(--red)">с ошибками ' + num(cnt.problem) + '</span>');
  const s = first.settings || {};
  const adapt = [];
  if (s.name_template && s.name_template !== '{name}') adapt.push('имя «' + esc(s.name_template) + '»');
  if ((s.replacements || []).length) adapt.push('замен: ' + num(s.replacements.length));
  if (s.langs === 'all') adapt.push('все языки'); else if ((s.langs || []).length) adapt.push('языки: ' + esc(s.langs.join(', ')));
  const btns = [];
  if (first.op_id && first.settings) btns.push(`<button class="btn btn-s" style="padding:6px 12px;font-size:12px" onclick="claRepeat(${Number(first.op_id)})">🔁 Повторить</button>`);
  if (first.can_rollback) btns.push(`<button class="btn btn-d" style="padding:6px 12px;font-size:12px" onclick="claRollback(${Number(first.op_id)})">↩️ Вернуть как было</button>`);
  if (first.rolled_back) btns.push('<span class="badge b-gy">↩️ Откачено</span>');
  return `<div class="card" style="margin-bottom:10px">
    <div class="row-name">${esc(src)} → ${num(g.rows.length)} ${plural(g.rows.length, 'бот', 'бота', 'ботов')}</div>
    <div class="row-val" style="color:var(--hint)">${ago(first.created_at)} · ${esc(first.fields_label || '—')}${adapt.length ? ' · ' + adapt.join(' · ') : ''}</div>
    <div class="row-val">${parts.join(' · ')}</div>
    <div class="lst" style="margin-top:6px">${g.rows.map(claRowHtml).join('')}</div>
    ${btns.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${btns.join('')}</div>` : ''}
  </div>`;
}

function claRowHtml(r) {
  const tgt = r.target_uname ? '@' + r.target_uname : (r.target_name || ('бот ' + r.target_bot_id));
  const [cls, lbl] = CLA_ST[r.status] || stb(r.status);
  const ch = Array.isArray(r.changes) ? r.changes : [];
  const det = ch.length ? `<div id="claDet${Number(r.id)}" style="display:none;margin-top:6px">${ch.map(c => {
      const mark = c.result === 'ok' ? '✅' : c.result === 'same' ? '＝' : '❌';
      const body = c.result === 'same'
        ? `<span style="color:var(--hint)">${c.note ? esc(c.note) : 'уже было «' + esc(c.after) + '»'}</span>`
        : `«${esc(c.before)}» → «${esc(c.after)}»${c.truncated ? ' <span style="color:var(--orange)">(обрезано по лимиту Telegram)</span>' : ''}`;
      return `<div style="font-size:12px;margin:3px 0">${mark} <b>${esc(c.label)}</b>: ${body}${c.reason ? `<div style="color:var(--red)">${esc(c.reason)}</div>` : ''}</div>`;
    }).join('')}</div>` : (r.details ? `<div style="font-size:12px;color:var(--hint)">${esc(r.details)}</div>` : '');
  const tap = ch.length ? ` onclick="claToggleDet(${Number(r.id)})" role="button" tabindex="0" style="cursor:pointer"` : '';
  return `<div class="row"${tap}><div class="row-body">
      <div class="row-name" style="font-size:13px">${esc(tgt)} <span class="badge ${cls}">${lbl}</span>${ch.length ? ' <span style="font-size:11px;color:var(--hint)">подробнее ▾</span>' : ''}</div>
      ${det}
    </div></div>`;
}

function claToggleDet(id) {
  const el = document.getElementById('claDet' + id);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function claRollback(opId) {
  const ok = await askConfirm('Вернуть ботам из этого клона прежние имя, описания, команды, кнопку меню и права? Фото Telegram вернуть не позволяет — оно останется новым.');
  if (!ok) return;
  try {
    const r = await api('/api/miniapp/clone_adapt/rollback', {method: 'POST', body: JSON.stringify({op_id: opId})});
    toast(`↩️ Откат поставлен в очередь: ${num(r.count)} ${plural(r.count, 'бот', 'бота', 'ботов')}`);
    openCloneAdapt(false, true);
  } catch (e) { toast(errRu(e, 'Откат не поставлен')); }
}

// ── Мастер ─────────────────────────────────────────────────────────────────

async function openCloneModal(prefill) {
  const errEl = document.getElementById('cl-err');
  if (errEl) errEl.textContent = '';
  CLA.previewOk = false;
  txt('claPreview', '');
  const p = prefill || {};
  CLA.targets = new Set((p.target_bot_ids || []).map(Number));
  CLA.fields = new Set(p.fields && p.fields.length ? p.fields : ['name', 'desc', 'short', 'commands']);
  const s = p.settings || {};
  CLA.reps = (s.replacements || []).map(x => [x[0], x[1]]);
  CLA.langs = new Set(Array.isArray(s.langs) ? s.langs : []);
  document.getElementById('claNameTpl').value = s.name_template || '{name}';
  document.getElementById('claSwap').checked = s.swap_mentions !== false;
  document.getElementById('claSkipSame').checked = s.skip_same !== false;
  document.getElementById('claLangMode').value = s.langs === 'all' ? 'all' : (CLA.langs.size ? 'pick' : 'base');
  document.getElementById('claTgtSearch').value = '';
  claRenderFields(); claRenderReps();
  const srcEl = document.getElementById('clSrc');
  srcEl.innerHTML = '<option value="">Загрузка…</option>';
  txt('claTargets', '<div class="spin-wrap"><div class="spin"></div></div>');
  showModal('mo-clone');
  try {
    const d = await api('/api/miniapp/bots');
    CLA.bots = (d.bots || d || []).filter(b => b.bot_id);
    if (CLA.bots.length < 2) {
      srcEl.innerHTML = '<option value="">Нужно минимум два бота</option>';
      txt('claTargets', empty('🤖', 'Клонировать не на кого', 'Добавьте ещё хотя бы одного бота — образец и цель должны быть разными.'));
      return;
    }
    srcEl.innerHTML = '<option value="">— выберите образец —</option>' + CLA.bots.map(b =>
      `<option value="${Number(b.bot_id)}">${esc(claBotLabel(b))}</option>`).join('');
    if (p.source_bot_id) srcEl.value = String(p.source_bot_id);
    claRenderTargets();
    if (srcEl.value) claSourceChanged(true);
    else txt('claSrcCard', 'Образец — бот, оформление которого станет у остальных.');
  } catch (e) {
    srcEl.innerHTML = '<option value="">Ошибка загрузки</option>';
    txt('claTargets', errHtml(errRu(e), 'openCloneModal()'));
  }
}

function claSrcId() { return Number(document.getElementById('clSrc').value || 0); }

async function claSourceChanged(keepLangs) {
  const id = claSrcId();
  CLA.targets.delete(id);
  CLA.profile = null;
  claPreviewStale();
  claRenderTargets();
  if (!id) { txt('claSrcCard', ''); return; }
  txt('claSrcCard', 'Читаю профиль образца…');
  try {
    const p = await api('/api/miniapp/clone_adapt/profile?bot_id=' + id);
    if (claSrcId() !== id) return;
    CLA.profile = p;
    if (!keepLangs) CLA.langs = new Set();
    const cmds = (p.commands || []).map(c => '/' + c.command).join(', ');
    txt('claSrcCard', `<div class="card" style="padding:10px">
      <div><b>${esc(p.name || 'без имени')}</b>${p.has_photo ? ' · 🖼 есть фото' : ' · без фото'}</div>
      ${p.short_description ? `<div>Кратко: ${esc(p.short_description)}</div>` : ''}
      ${p.description ? `<div>Описание: ${esc(p.description.length > 160 ? p.description.slice(0, 160) + '…' : p.description)}</div>` : ''}
      <div>Команды: ${cmds ? esc(cmds) : 'нет'}</div>
      <div>Кнопка меню: ${esc(p.menu || 'по умолчанию')}</div>
      <div>Переводы: ${(p.langs || []).length ? esc(p.langs.map(l => l.label).join(', ')) : 'нет'}</div>
    </div>`);
    claLangModeChanged();
  } catch (e) { txt('claSrcCard', `<span style="color:var(--red)">${esc(errRu(e, 'Профиль не прочитан'))}</span>`); }
}

function claRenderTargets() {
  const src = claSrcId();
  const q = (document.getElementById('claTgtSearch').value || '').trim().toLowerCase();
  const list = CLA.bots.filter(b => Number(b.bot_id) !== src)
    .filter(b => !q || (String(b.username || '') + ' ' + String(b.first_name || '')).toLowerCase().includes(q));
  txt('claTargets', list.length ? list.map(b => `
    <label class="chk-row" style="display:flex;align-items:center;gap:8px;font-size:13px;padding:5px 0">
      <input type="checkbox" ${CLA.targets.has(Number(b.bot_id)) ? 'checked' : ''} onchange="claToggleTarget(${Number(b.bot_id)}, this.checked)">
      <span>${esc(claBotLabel(b))}${b.username && b.first_name ? ` <span style="color:var(--hint)">${esc(b.first_name)}</span>` : ''}</span>
    </label>`).join('') : '<div style="font-size:12px;color:var(--hint)">Никого не нашлось</div>');
  claTargetCount();
}

function claTargetCount() {
  const n = CLA.targets.size;
  txt('claTgtCount', n ? 'выбрано ' + num(n) : 'не выбрано ни одного');
  const btn = document.getElementById('claRunBtn');
  if (btn) btn.textContent = n ? `Запустить на ${num(n)} ${plural(n, 'бот', 'бота', 'ботов')}` : 'Запустить';
}

function claToggleTarget(id, on) {
  if (on) CLA.targets.add(Number(id)); else CLA.targets.delete(Number(id));
  claTargetCount(); claPreviewStale();
}

function claTargetsAll(on) {
  const src = claSrcId();
  const q = (document.getElementById('claTgtSearch').value || '').trim().toLowerCase();
  CLA.bots.filter(b => Number(b.bot_id) !== src)
    .filter(b => !q || (String(b.username || '') + ' ' + String(b.first_name || '')).toLowerCase().includes(q))
    .forEach(b => { if (on) CLA.targets.add(Number(b.bot_id)); else CLA.targets.delete(Number(b.bot_id)); });
  claRenderTargets(); claPreviewStale();
}

function claRenderFields() {
  txt('claFields', CLA_FIELDS.map(([k, l]) =>
    `<span class="chip${CLA.fields.has(k) ? ' on' : ''}" onclick="claToggleField('${k}')">${CLA.fields.has(k) ? '✓ ' : ''}${esc(l)}</span>`).join(''));
  const nameBox = document.getElementById('claNameBox');
  if (nameBox) nameBox.style.display = CLA.fields.has('name') ? '' : 'none';
}

function claToggleField(k) {
  if (CLA.fields.has(k)) CLA.fields.delete(k); else CLA.fields.add(k);
  claRenderFields(); claPreviewStale();
}

function claNamePreset(tpl) {
  const el = document.getElementById('claNameTpl');
  el.value = tpl; el.focus();
  claPreviewStale();
}

function claRenderReps() {
  txt('claReps', CLA.reps.map((r, i) => `
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
      <input type="text" class="inp-sm" style="flex:1 1 0;min-width:0" placeholder="что" value="${esc(r[0])}" oninput="claRepSet(${i},0,this.value)">
      <span>→</span>
      <input type="text" class="inp-sm" style="flex:1 1 0;min-width:0" placeholder="на что" value="${esc(r[1])}" oninput="claRepSet(${i},1,this.value)">
      <span class="chip" onclick="claRepDel(${i})" aria-label="Убрать замену">✕</span>
    </div>`).join(''));
}
function claAddRep() { if (CLA.reps.length >= 20) { toast('Не больше 20 замен'); return; } CLA.reps.push(['', '']); claRenderReps(); }
function claRepSet(i, j, v) { if (CLA.reps[i]) { CLA.reps[i][j] = v; claPreviewStale(); } }
function claRepDel(i) { CLA.reps.splice(i, 1); claRenderReps(); claPreviewStale(); }

function claLangModeChanged() {
  const mode = document.getElementById('claLangMode').value;
  const box = document.getElementById('claLangs');
  claPreviewStale();
  if (mode !== 'pick') { box.style.display = 'none'; return; }
  box.style.display = 'flex';
  const have = (CLA.profile && CLA.profile.langs) || [];
  if (!have.length) {
    box.innerHTML = '<span style="font-size:12px;color:var(--hint)">У образца нет переводов — будет перенесён только основной профиль.</span>';
    return;
  }
  box.innerHTML = have.map(l =>
    `<span class="chip${CLA.langs.has(l.code) ? ' on' : ''}" onclick="claToggleLang('${escJs(l.code)}')">${CLA.langs.has(l.code) ? '✓ ' : ''}${esc(l.label)}</span>`).join('');
}
function claToggleLang(code) {
  if (CLA.langs.has(code)) CLA.langs.delete(code); else CLA.langs.add(code);
  claLangModeChanged();
}

function claPreviewStale() {
  if (CLA.previewOk) txt('claPreview', '<div style="font-size:12px;color:var(--hint);margin-top:6px">Настройки изменились — нажмите «Что изменится», чтобы обновить предпросмотр.</div>');
  CLA.previewOk = false;
}

function claRequest() {
  const mode = document.getElementById('claLangMode').value;
  return {
    source_bot_id: claSrcId(),
    target_bot_ids: Array.from(CLA.targets),
    fields: CLA_FIELDS.map(f => f[0]).filter(k => CLA.fields.has(k)),
    settings: {
      name_template: document.getElementById('claNameTpl').value || '{name}',
      replacements: CLA.reps.filter(r => r[0]),
      swap_mentions: document.getElementById('claSwap').checked,
      skip_same: document.getElementById('claSkipSame').checked,
      langs: mode === 'all' ? 'all' : (mode === 'pick' ? Array.from(CLA.langs) : []),
    },
  };
}

function claValidate(req) {
  if (!req.source_bot_id) return 'Выберите бота-образец';
  if (!req.target_bot_ids.length) return 'Отметьте хотя бы одного бота, куда переносить';
  if (!req.fields.length) return 'Выберите, что копировать';
  return '';
}

async function claPreview() {
  const errEl = document.getElementById('cl-err');
  errEl.textContent = '';
  const req = claRequest();
  const bad = claValidate(req);
  if (bad) { errEl.textContent = bad; return; }
  txt('claPreview', '<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const d = await api('/api/miniapp/clone_adapt/preview', {method: 'POST', body: JSON.stringify(req)});
    const tg = d.targets || [];
    const changes = tg.reduce((a, t) => a + (t.counts.change || 0), 0);
    const head = `<div class="card" style="padding:10px;margin-top:8px">
      <div><b>${esc(d.source)} → ${num(d.total)} ${plural(d.total, 'бот', 'бота', 'ботов')}</b></div>
      <div style="font-size:12px;color:var(--hint)">${esc(d.summary || '')}</div>
      ${d.shown < d.total ? `<div style="font-size:12px;color:var(--hint)">Показаны первые ${num(d.shown)} — остальные пройдут по тем же правилам.</div>` : ''}
      ${!changes ? '<div style="font-size:12px;color:var(--green)">У показанных ботов всё уже как у образца — менять нечего.</div>' : ''}
    </div>`;
    txt('claPreview', head + tg.map(t => `
      <div class="card" style="padding:10px;margin-top:6px">
        <div class="row-name" style="font-size:13px">${esc(t.label)} · изменится ${num(t.counts.change)}${t.counts.same ? ', совпадает ' + num(t.counts.same) : ''}</div>
        ${t.steps.filter(s => !s.same).map(s => `<div style="font-size:12px;margin:3px 0"><b>${esc(s.label)}</b>: «${esc(s.before)}» → «${esc(s.after)}»${s.truncated ? ' <span style="color:var(--orange)">(обрезано по лимиту Telegram)</span>' : ''}</div>`).join('')}
        ${t.steps.filter(s => s.same && s.note).map(s => `<div style="font-size:12px;color:var(--hint)">${esc(s.label)}: ${esc(s.note)}</div>`).join('')}
      </div>`).join(''));
    CLA.previewOk = true;
  } catch (e) { txt('claPreview', `<div style="color:var(--red);font-size:13px;margin-top:6px">${esc(errRu(e, 'Предпросмотр не получился'))}</div>`); }
}

async function submitCloneAdapt() {
  const errEl = document.getElementById('cl-err');
  errEl.textContent = '';
  const req = claRequest();
  const bad = claValidate(req);
  if (bad) { errEl.textContent = bad; return; }
  if (!CLA.previewOk) {
    const n = req.target_bot_ids.length;
    const ok = await askConfirm(`Перенести оформление на ${n} ${plural(n, 'бота', 'бота', 'ботов')} без предпросмотра? Прежние значения сохранятся — клон можно будет откатить.`);
    if (!ok) return;
  }
  try {
    const r = await api('/api/miniapp/clone_adapt/submit', {method: 'POST', body: JSON.stringify(req)});
    closeModal('mo-clone');
    toast(`✅ Клон поставлен в очередь: ${num(r.targets || req.target_bot_ids.length)} ${plural(r.targets || req.target_bot_ids.length, 'бот', 'бота', 'ботов')}`);
    openCloneAdapt(false, true);
  } catch (e) { errEl.textContent = errRu(e, 'Ошибка'); }
}

// «Повторить» из истории: мастер с теми же образцом, целями и настройками.
function claRepeat(opId) {
  const rows = CA_ROWS.filter(r => Number(r.op_id) === Number(opId));
  if (!rows.length) return;
  const s = rows[0].settings || {};
  openCloneModal({
    source_bot_id: rows[0].source_bot_id,
    target_bot_ids: rows.map(r => r.target_bot_id),
    fields: s.fields || String(rows[0].fields || '').split(','),
    settings: s,
  });
}
