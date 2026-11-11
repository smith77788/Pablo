// «🎯 План роста» — цели по каналам и ботам, прогресс и рычаги на своих данных.
//
// Сервер: services/mini_app_growth_plan.py (логика — services/growth_plan.py).
// Все числа, вердикты и подписи приходят с сервера готовыми по-русски; экран
// их только показывает. Состояние — объект GP, объявлен в index.html.

const _gpNum = n => Number(n || 0).toLocaleString('ru-RU');
const _GP_STATE_COLOR = {done: 'var(--green)', ahead: 'var(--green)', on_track: 'var(--accent)',
  behind: 'var(--orange, #e6a23c)', stalled: 'var(--red)', overdue: 'var(--red)'};

async function gpLoad() {
  const box = document.getElementById('gpGoals');
  if (!box) return;
  box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  gpCloseNew();
  try {
    GP.data = await api('/api/miniapp/growth/goals');
  } catch (e) {
    box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить цели'), 'gpLoad()');
    return;
  }
  const goals = GP.data.goals || [];
  if (!goals.length) {
    const a = GP.data.assets || {};
    const none = !(a.channels || []).length && !(a.bots || []).length;
    box.innerHTML = none
      ? empty('🎯', 'Пока нечего растить', 'Добавьте канал или бота — и поставьте ему цель.')
      : empty('🎯', 'Целей пока нет', 'Поставьте цель каналу или боту: план покажет, успеваете ли вы и чем расти.', {label: '+ Поставить цель', fn: 'gpOpenNew()'});
    return;
  }
  box.innerHTML = goals.map(_gpGoalRow).join('');
}

function _gpBar(p) {
  const color = _GP_STATE_COLOR[p.state] || 'var(--accent)';
  return `<div style="height:8px;border-radius:4px;background:var(--bg3);overflow:hidden;margin:6px 0 4px">
    <div style="height:100%;width:${Number(p.pct) || 0}%;background:${color};border-radius:4px"></div></div>`;
}

function _gpGoalRow(g) {
  const p = g.progress, color = _GP_STATE_COLOR[p.state] || 'var(--accent)';
  return `<div class="row tap" onclick="gpOpenGoal(${Number(g.id)})" role="button" tabindex="0">
    <div class="row-ico" style="background:var(--bg-purple-16)">${g.asset_kind === 'bot' ? '🤖' : '📡'}</div>
    <div class="row-body">
      <div class="row-name">${esc(g.name)} <span style="color:${color};font-weight:600;font-size:12px">${esc(g.state_ru)}</span></div>
      ${_gpBar(p)}
      <div class="row-val">${esc(_gpNum(p.current))} из ${esc(_gpNum(p.target))} · ${esc(String(p.pct))}% · до ${esc(g.deadline_ru)}</div>
    </div><span class="chev">›</span>
  </div>`;
}

// ── Новая цель и правка ────────────────────────────────────────────────────

function gpOpenNew() {
  if (!GP.data) return;
  GP.edit = null;
  const a = GP.data.assets || {}, sel = document.getElementById('gpAsset');
  const busy = new Set((GP.data.goals || []).filter(g => g.status === 'active').map(g => g.asset_kind + ':' + g.asset_id));
  sel.innerHTML = '';
  for (const c of (a.channels || [])) if (!busy.has('channel:' + c.id)) sel.add(new Option(`📡 ${c.name} · ${_gpNum(c.current)}`, 'channel:' + c.id));
  for (const b of (a.bots || [])) if (!busy.has('bot:' + b.id)) sel.add(new Option(`🤖 ${b.name} · ${_gpNum(b.current)}`, 'bot:' + b.id));
  if (!sel.options.length) { toast('У всех ваших каналов и ботов уже есть цель'); return; }
  document.getElementById('gpAssetWrap').style.display = 'block';
  txt('gpNewSec', 'Новая цель');
  txt('gpNewBtn', '🎯 Поставить цель');
  document.getElementById('gpDays').value = '30';
  document.getElementById('gpNewErr').style.display = 'none';
  document.getElementById('gpNewWrap').style.display = 'block';
  gpAssetChanged();
  document.getElementById('gpNewWrap').scrollIntoView({behavior: 'smooth', block: 'center'});
}

function gpCloseNew() {
  const w = document.getElementById('gpNewWrap');
  if (w) w.style.display = 'none';
}

function _gpPickedAsset() {
  const [kind, id] = (document.getElementById('gpAsset').value || ':').split(':');
  const list = ((GP.data.assets || {})[kind === 'bot' ? 'bots' : 'channels'] || []);
  return {kind, id: Number(id), asset: list.find(x => Number(x.id) === Number(id))};
}

function gpAssetChanged() {
  const {asset} = _gpPickedAsset();
  const cur = asset ? Number(asset.current) : 0;
  txt('gpNowNote', `Сейчас: ${_gpNum(cur)}`);
  // Подсказка по умолчанию — заметный, но достижимый шаг: +50% или +100 человек.
  document.getElementById('gpTarget').value = String(cur + Math.max(100, Math.round(cur * 0.5)));
}

async function gpSaveGoal() {
  const err = document.getElementById('gpNewErr');
  err.style.display = 'none';
  const target = parseInt(document.getElementById('gpTarget').value, 10);
  const days = parseInt(document.getElementById('gpDays').value, 10);
  const btn = document.getElementById('gpNewBtn');
  btn.disabled = true;
  try {
    if (GP.edit) {
      await api(`/api/miniapp/growth/goal/${Number(GP.edit)}/update`, {method: 'POST', body: JSON.stringify({target, days})});
      toast('✅ Цель обновлена');
    } else {
      const {kind, id} = _gpPickedAsset();
      await api('/api/miniapp/growth/goal', {method: 'POST', body: JSON.stringify({asset_kind: kind, asset_id: id, target, days})});
      toast('🎯 Цель поставлена');
    }
    tg.HapticFeedback?.notificationOccurred('success');
    gpLoad();
  } catch (e) {
    err.textContent = errRu(e, 'Не удалось сохранить цель');
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
  }
}

// ── Экран одной цели ───────────────────────────────────────────────────────

// График ряда (дата, число): линия факта и пунктир «от старта к цели».
function _gpChart(g) {
  const pts = (g.series || []).map(([d, v]) => [new Date(d + 'T00:00:00Z').getTime(), Number(v)]);
  if (pts.length < 2) return '<div class="field-note" style="padding:6px 2px">График появится, когда наберётся история за несколько дней.</div>';
  const p = g.progress, W = 320, H = 110, pad = 6;
  const t0 = pts[0][0], t1 = Math.max(pts[pts.length - 1][0], new Date(g.deadline + 'T00:00:00Z').getTime());
  const vals = pts.map(x => x[1]).concat([p.target, p.baseline]);
  const lo = Math.min(...vals), hi = Math.max(...vals), span = Math.max(1, hi - lo);
  const x = t => pad + (W - 2 * pad) * (t - t0) / Math.max(1, t1 - t0);
  const y = v => H - pad - (H - 2 * pad) * (v - lo) / span;
  const line = pts.map((q, i) => (i ? 'L' : 'M') + x(q[0]).toFixed(1) + ' ' + y(q[1]).toFixed(1)).join(' ');
  const s0 = new Date(g.started_on + 'T00:00:00Z').getTime();
  const plan = `M${x(Math.max(s0, t0)).toFixed(1)} ${y(p.baseline).toFixed(1)} L${x(t1).toFixed(1)} ${y(p.target).toFixed(1)}`;
  const color = _GP_STATE_COLOR[p.state] || 'var(--accent)';
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="График роста и план" style="width:100%;height:auto;display:block;margin:6px 0">
    <path d="${plan}" fill="none" stroke="var(--hint)" stroke-width="1.5" stroke-dasharray="4 4"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>
  <div class="field-note" style="padding:0 2px 4px">Сплошная — как растёт на деле, пунктир — как нужно, чтобы успеть${g.asset_kind === 'bot' ? '. У бота ряд собран по дням прихода людей' : ''}</div>`;
}

async function gpOpenGoal(id) {
  const gid = Number(id);
  push('s-growthgoal');
  document.getElementById('gpGoalBody').style.display = 'none';
  document.getElementById('gpGoalHead').innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  let g;
  try {
    g = await api(`/api/miniapp/growth/goal/${gid}/plan`);
  } catch (e) {
    document.getElementById('gpGoalHead').innerHTML = errHtml(errRu(e, 'Не удалось загрузить цель'), 'gpOpenGoal(' + esc(gid) + ')');
    return;
  }
  GP.goal = g;
  const p = g.progress, color = _GP_STATE_COLOR[p.state] || 'var(--accent)';
  txt('gpGoalTitle', '🎯 ' + g.name);
  const chip = (v, l) => `<div class="kpi-chip"><div class="kpi-chip-val">${esc(v)}</div><div class="kpi-chip-lbl">${esc(l)}</div></div>`;
  const pace = p.pace == null ? '—' : String(p.pace).replace('.', ',');
  document.getElementById('gpGoalHead').innerHTML = `
    <div style="font-size:15px;font-weight:600;color:${color}">${esc(g.state_ru)}</div>
    <div style="font-size:13px;line-height:1.5;padding:4px 0 6px;white-space:normal">${esc(p.verdict)}</div>
    ${_gpBar(p)}
    <div style="display:flex;gap:8px;padding:6px 0;flex-wrap:wrap">
      ${chip(_gpNum(p.current) + ' / ' + _gpNum(p.target), 'сейчас / цель')}
      ${chip(_gpNum(p.remaining), 'осталось')}
      ${chip(p.days_left > 0 ? _gpNum(p.days_left) : '0', 'дней до срока')}
      ${chip(_gpNum(p.need_per_day), 'нужно в день')}
      ${chip(pace, p.pace_is_recent ? 'в день за неделю' : 'в день в среднем')}
    </div>
    ${_gpChart(g)}`;
  document.getElementById('gpToday').innerHTML = (g.today || []).map((t, i) => `
    <div class="row tap" onclick="gpAct(GP.goal.today[${i}].action)" role="button" tabindex="0">
      <div class="row-ico" style="background:var(--bg-green-16)">${i + 1}</div>
      <div class="row-body"><div class="row-name" style="white-space:normal">${esc(t.text)}</div></div>
      <span class="chev">›</span>
    </div>`).join('');
  txt('gpCoverage', (g.coverage || {}).text || '');
  document.getElementById('gpLevers').innerHTML = (g.levers || []).map((lv, i) => {
    const est = lv.estimate ? `<span style="color:var(--green)">от +${esc(_gpNum(lv.estimate[0]))} до +${esc(_gpNum(lv.estimate[1]))}</span>` : '';
    return `<div class="row tap" onclick="gpAct(GP.goal.levers[${i}].action)" role="button" tabindex="0">
      <div class="row-ico" style="background:${lv.ready ? 'var(--bg-green-16)' : 'var(--bg3)'}">${lv.ready ? '✅' : '⚪'}</div>
      <div class="row-body">
        <div class="row-name" style="white-space:normal">${esc(lv.title)} ${est}</div>
        <div class="row-val" style="white-space:normal">${esc((lv.facts || []).join(' · '))}</div>
        ${lv.missing ? `<div class="row-val" style="white-space:normal;color:var(--orange, #e6a23c)">${esc(lv.missing)}</div>` : ''}
        <div class="row-val" style="white-space:normal;color:var(--hint);font-size:11px">${esc(lv.estimate_note || '')}</div>
      </div><span class="chev">›</span>
    </div>`;
  }).join('');
  const log = g.log || [];
  document.getElementById('gpLog').innerHTML = log.length ? log.map(o => {
    const after = o.after == null ? '' : ` · за 3 дня после: ${o.after >= 0 ? '+' : ''}${_gpNum(o.after)}`;
    return `<div class="row tap" onclick="openOpDetail(${Number(o.op_id)})" role="button" tabindex="0">
      <div class="row-ico" style="background:var(--bg-blue-16)">📌</div>
      <div class="row-body">
        <div class="row-name" style="white-space:normal">${esc(o.what)}</div>
        <div class="row-val" style="white-space:normal">${esc(o.day_ru)} · <span class="badge ${stb(o.status)[0]}">${stb(o.status)[1]}</span> · ${esc(_gpNum(o.done))} из ${esc(_gpNum(o.total))}${esc(after)}</div>
      </div><span class="chev">›</span>
    </div>`;
  }).join('') + '<div class="field-note" style="padding:8px 4px">«За 3 дня после» — весь прирост за эти дни, а не только от этого действия.</div>'
    : empty('📌', 'Действий по росту ещё не было', 'Начните с первого дела из списка «Сегодня».');
  document.getElementById('gpGoalBody').style.display = 'block';
}

// Действие рычага: сервер называет вид, экран знает, какой раздел открыть.
function gpAct(a) {
  const g = GP.goal;
  if (!a || !g) return;
  switch (a.kind) {
    case 'self_promo': return gpOpenSelfPromo(g.asset_kind, g.asset_id);
    case 'invite':
      openCampaignPlanner();
      if (a.goal) document.getElementById('cpGoal').value = String(a.goal);
      if (a.days) document.getElementById('cpDeadline').value = String(a.days);
      return;
    case 'accounts': return goTab('accounts');
    case 'parser': return openParser();
    case 'va': return openVaAdmin();
    case 'niche': return openNiche();
    case 'growth_agent': return openGrowth();
    case 'seo': return openSeoObj();
    case 'boost': return openBoost();
    case 'edit_goal': return gpEditGoal();
    case 'new_goal': back(); return gpOpenNew();
    default: return;
  }
}

// Самопиар с уже выбранной целью продвижения.
async function gpOpenSelfPromo(kind, id) {
  await openSelfPromo();
  if (!SPC.data) {
    try { SPC.data = await api('/api/miniapp/selfpromo/overview'); }
    catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось открыть самопиар')); return; }
  }
  const mine = (SPC.data.campaigns || []).find(c => c.target_kind === kind && Number(c.target_ref) === Number(id));
  if (mine) spcOpenEditor(mine.id); else spcOpenEditor(null, {target_kind: kind, target_ref: id});
}

// Форма правки живёт на экране списка целей, поэтому его надо ОТКРЫТЬ, а не
// надеяться, что он остался в истории: план открывают и из уведомления.
function gpEditGoal() {
  const g = GP.goal;
  if (!g) return;
  if (typeof CUR !== 'undefined' && CUR === 'growthhub') back(); else push('s-growthhub');
  GP.edit = g.id;
  document.getElementById('gpAssetWrap').style.display = 'none';
  txt('gpNewSec', 'Изменить цель: ' + g.name);
  txt('gpNewBtn', '💾 Сохранить');
  txt('gpNowNote', `Сейчас: ${_gpNum(g.progress.current)} · старт был с ${_gpNum(g.progress.baseline)}`);
  document.getElementById('gpTarget').value = String(g.progress.target);
  const left = Math.max(1, Number(g.progress.days_left) || 30);
  const sel = document.getElementById('gpDays');
  if (![...sel.options].some(o => o.value === String(left))) sel.add(new Option(`${left} дн. — как сейчас`, String(left)), 0);
  sel.value = String(left);
  document.getElementById('gpNewErr').style.display = 'none';
  document.getElementById('gpNewWrap').style.display = 'block';
  document.getElementById('gpNewWrap').scrollIntoView({behavior: 'smooth', block: 'center'});
}

async function gpDeleteGoal() {
  const g = GP.goal;
  if (!g) return;
  if (!await askConfirm(`Удалить цель «${g.name}»?\nКанал или бот останутся, исчезнет только сама цель и её прогресс.`)) return;
  try {
    await api(`/api/miniapp/growth/goal/${Number(g.id)}/delete`, {method: 'POST', body: '{}'});
    toast('🗑 Цель удалена');
    back();
    gpLoad();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось удалить цель'));
  }
}
