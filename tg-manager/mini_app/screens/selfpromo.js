// «📣 Самопиар» — продвижение своих каналов и ботов по своей же аудитории.
//
// Сервер: services/mini_app_selfpromo.py (логика — services/self_promo.py).
// Экран отвечает на пять вопросов кампании: что продвигаем, где показываем,
// кому, каким сообщением и как часто. Число получателей и причины отсева
// считаются сервером ДО сохранения и запуска — подтверждение называет их.
//
// Состояние — объект SPC, объявлен в index.html (вынесенные экраны —
// классические скрипты и делят с ним область видимости). Старый блок «Реклама
// Infragram по реферальной ссылке» остался в index.html (openSelfPromo).

const _spcExact = n => Number(n || 0).toLocaleString('ru-RU');
const _spcPeople = n => `${_spcExact(n)} ${plural(Number(n) || 0, 'человек', 'человека', 'человек')}`;

async function spcLoad() {
  const list = document.getElementById('spcList');
  if (!list) return;
  list.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    SPC.data = await api('/api/miniapp/selfpromo/overview');
  } catch (e) {
    list.innerHTML = errHtml(errRu(e, 'Не удалось загрузить самопиар'), 'spcLoad()');
    return;
  }
  _spcRenderKpis();
  _spcRenderIdeas();
  _spcRenderList();
}

function _spcRenderKpis() {
  const d = SPC.data, a = d.audience || {};
  const chip = (v, l) => `<div class="kpi-chip"><div class="kpi-chip-val">${esc(v)}</div><div class="kpi-chip-lbl">${esc(l)}</div></div>`;
  document.getElementById('spcKpis').innerHTML =
    chip(_spcExact(a.unique), 'людей в ваших ботах')
    + chip(String((d.bots || []).length), plural((d.bots || []).length, 'бот', 'бота', 'ботов'))
    + chip(String((d.channels || []).length), plural((d.channels || []).length, 'канал', 'канала', 'каналов'))
    + (a.overlap > 0 ? chip(_spcExact(a.overlap), 'подписок-повторов между ботами') : '');
}

function _spcRenderIdeas() {
  const ideas = (SPC.data.ideas || []);
  document.getElementById('spcIdeasWrap').style.display = ideas.length ? 'block' : 'none';
  document.getElementById('spcIdeas').innerHTML = ideas.map((x, i) => `
    <div class="row tap" onclick="spcOpenIdea(${i})" role="button" tabindex="0">
      <div class="row-ico" style="background:var(--bg-green-16)">${x.target_kind === 'bot' ? '🤖' : '📡'}</div>
      <div class="row-body">
        <div class="row-name">${esc(x.name)} <span style="color:var(--green)">до ${esc(_spcPeople(x.reach))}</span></div>
        <div class="row-val" style="white-space:normal">${esc(x.why)}</div>
      </div><span class="chev">›</span>
    </div>`).join('');
}

function _spcStatsLine(c) {
  const parts = [];
  if (Number(c.launches)) {
    parts.push(`доставлено ${_spcExact(c.delivered)} из ${_spcExact(c.planned)}`);
    if (c.target_kind === 'bot' && c.clicks != null) {
      parts.push(`перешли в бота: ${_spcExact(c.click_people)}`);
    }
    if (c.target_kind === 'channel' && c.channel_delta != null) {
      parts.push(`канал с первого запуска: ${c.channel_delta >= 0 ? '+' : ''}${_spcExact(c.channel_delta)}`);
    }
    if (Number(c.channel_posts)) parts.push(`постов в каналах: ${_spcExact(c.channel_posts)}`);
  } else {
    parts.push('ещё не запускалась');
  }
  return parts.join(' · ');
}

function _spcRenderList() {
  const rows = SPC.data.campaigns || [];
  const box = document.getElementById('spcList');
  if (!rows.length) {
    const noBots = !(SPC.data.bots || []).length;
    box.innerHTML = noBots
      ? empty('📣', 'Сначала нужен бот', 'Самопиар показывает ваши каналы и ботов подписчикам ваших ботов. Добавьте бота — и возвращайтесь.', {label: '🤖 К ботам', fn: "goTab('bots')"})
      : empty('📣', 'Кампаний пока нет', 'Выберите идею выше или создайте кампанию: что продвигаем и кому показываем.', {label: '+ Создать кампанию', fn: 'spcOpenEditor()'});
    return;
  }
  const ico = {bot: '🤖', channel: '📡', link: '🔗'};
  box.innerHTML = rows.map(c => {
    const auto = Number(c.auto_days) > 0
      ? (c.status === 'active' ? `🔁 новым раз в ${Number(c.auto_days)} дн.` : '⏸ автопоказ на паузе')
      : 'вручную';
    const id = Number(c.id);
    return `<div class="row" style="flex-wrap:wrap">
      <div class="row-ico" style="background:var(--bg-purple-16)">${ico[c.target_kind] || '📣'}</div>
      <div class="row-body">
        <div class="row-name">${esc(c.title)}</div>
        <div class="row-val" style="white-space:normal">${esc(_spcStatsLine(c))}</div>
        <div class="row-val" style="color:var(--hint)">${esc(c.segment_ru)} · ${esc(auto)}</div>
      </div>
      <div style="display:flex;gap:6px;width:100%;margin-top:8px">
        <button class="btn btn-p" style="flex:2;min-height:34px;font-size:13px;padding:6px" onclick="spcLaunch(${id}, this)">▶ Запустить</button>
        <button class="btn btn-s" style="flex:1;min-height:34px;font-size:13px;padding:6px" onclick="spcOpenEditor(${id})">✏️ Изменить</button>
        ${Number(c.auto_days) > 0 ? `<button class="btn btn-s" style="flex:1;min-height:34px;font-size:13px;padding:6px" onclick="spcToggle(${id})">${c.status === 'active' ? '⏸ Пауза' : '▶ Авто'}</button>` : ''}
        <button class="icon-btn sm danger" style="min-width:34px;min-height:34px" onclick="spcDelete(${id}, this)" aria-label="Удалить кампанию" title="Удалить">🗑</button>
      </div>
    </div>`;
  }).join('');
}

// ── Редактор ───────────────────────────────────────────────────────────────

function spcOpenIdea(i) {
  const x = (SPC.data.ideas || [])[i];
  if (!x) return;
  spcOpenEditor(null, {target_kind: x.target_kind, target_ref: x.target_ref});
}

function spcOpenEditor(id, preset) {
  if (!SPC.data) { toast('Самопиар ещё загружается — повторите через секунду'); return; }
  const c = id ? (SPC.data.campaigns || []).find(x => Number(x.id) === Number(id)) : null;
  SPC.edit = c ? Number(c.id) : null;
  const v = c || Object.assign({target_kind: 'bot', bot_ids: null, channel_ids: [], segment: 'all',
    skip_target_audience: true, cooldown_days: 7, auto_days: 0, body: '', button_text: '', title: '',
    target_url: ''}, preset || {});
  push('s-spc-edit');
  txt('spcEditTitle', c ? '📣 Изменить кампанию' : '📣 Новая кампания');
  document.getElementById('spcErr').style.display = 'none';
  document.getElementById('spcKind').value = v.target_kind;
  const seg = document.getElementById('spcSegment');
  seg.innerHTML = '';
  for (const s of (SPC.data.segments || [])) seg.add(new Option(s.label, s.key));
  seg.value = v.segment || 'all';
  document.getElementById('spcSkip').checked = v.skip_target_audience !== false;
  document.getElementById('spcCooldown').value = String(v.cooldown_days ?? 7);
  document.getElementById('spcAuto').value = String(v.auto_days || 0);
  document.getElementById('spcBody').value = v.body || '';
  document.getElementById('spcButton').value = v.button_text || '';
  document.getElementById('spcName').value = c ? (v.title || '') : '';
  document.getElementById('spcUrl').value = v.target_kind === 'bot' ? '' : (v.target_url || '');
  spcCount();
  _spcFillTargets(v.target_ref);
  // Новая кампания: отмечены все боты — как правило, показать хотят всем.
  const botSel = v.bot_ids ? new Set(v.bot_ids.map(Number)) : null;
  const chSel = new Set((v.channel_ids || []).map(Number));
  document.getElementById('spcBots').innerHTML = (SPC.data.bots || []).length
    ? SPC.data.bots.map(b => `<label class="chk-row" style="display:flex;align-items:center;gap:8px;padding:6px 0;font-size:14px">
        <input type="checkbox" class="spc-bot" value="${esc(b.bot_id)}" ${(!botSel || botSel.has(Number(b.bot_id))) ? 'checked' : ''} onchange="spcPreviewSoon()">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">@${esc(b.username || b.first_name || b.bot_id)}</span>
        <span style="color:var(--hint);font-size:12px">${esc(_spcExact(b.subs))} подп.</span></label>`).join('')
    : '<div style="font-size:13px;color:var(--hint);padding:6px 0">Активных ботов нет</div>';
  document.getElementById('spcChannels').innerHTML = (SPC.data.channels || []).length
    ? SPC.data.channels.map(ch => `<label class="chk-row" style="display:flex;align-items:center;gap:8px;padding:6px 0;font-size:14px">
        <input type="checkbox" class="spc-ch" value="${esc(ch.channel_id)}" ${chSel.has(Number(ch.channel_id)) ? 'checked' : ''} onchange="spcPreviewSoon()">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(ch.title || (ch.username ? '@' + ch.username : 'Канал'))}</span>
        <span style="color:var(--hint);font-size:12px">${esc(_spcExact(ch.members))} уч.</span></label>`).join('')
    : '<div style="font-size:13px;color:var(--hint);padding:6px 0">Каналов нет</div>';
  spcKindChanged(true);
}

function _spcFillTargets(selected) {
  const kind = document.getElementById('spcKind').value;
  const sel = document.getElementById('spcTarget');
  sel.innerHTML = '';
  if (kind === 'bot') {
    for (const b of (SPC.data.bots || [])) if (b.username) sel.add(new Option('@' + b.username, String(b.bot_id)));
  } else if (kind === 'channel') {
    for (const ch of (SPC.data.channels || [])) {
      sel.add(new Option((ch.title || (ch.username ? '@' + ch.username : 'Канал')) + (ch.username ? '' : ' · закрытый'), String(ch.channel_id)));
    }
  }
  if (!sel.options.length && kind !== 'link') sel.add(new Option(kind === 'bot' ? 'Ботов нет' : 'Каналов нет', ''));
  if (selected != null && [...sel.options].some(o => o.value === String(selected))) sel.value = String(selected);
}

function spcKindChanged(keep) {
  const kind = document.getElementById('spcKind').value;
  if (!keep) _spcFillTargets(null);
  document.getElementById('spcTargetWrap').style.display = kind === 'link' ? 'none' : 'block';
  txt('spcTargetLbl', kind === 'bot' ? 'Какого бота' : 'Какой канал или группу');
  document.getElementById('spcSkipWrap').style.display = kind === 'bot' ? 'flex' : 'none';
  spcTargetChanged();
}

function spcTargetChanged() {
  const kind = document.getElementById('spcKind').value;
  const ref = document.getElementById('spcTarget').value;
  let needUrl = kind === 'link', note = '';
  if (kind === 'channel') {
    const ch = (SPC.data.channels || []).find(x => String(x.channel_id) === ref);
    if (ch && !ch.username) {
      needUrl = true;
      note = 'У канала нет @username — вставьте пригласительную ссылку (https://t.me/+…), иначе людям некуда перейти.';
    }
  }
  if (kind === 'link') note = 'Сайт, чужой канал, ваша реферальная ссылка — что угодно. Переходы по внешней ссылке Telegram не сообщает.';
  document.getElementById('spcUrlWrap').style.display = needUrl ? 'block' : 'none';
  txt('spcUrlLbl', kind === 'channel' ? 'Пригласительная ссылка' : 'Ссылка (https://…)');
  txt('spcUrlNote', note);
  spcPreviewSoon();
}

function spcCount() {
  txt('spcBodyLen', String((document.getElementById('spcBody').value || '').length));
}

function _spcForm() {
  const kind = document.getElementById('spcKind').value;
  const ids = cls => [...document.querySelectorAll('#s-spc-edit .' + cls)].filter(c => c.checked).map(c => Number(c.value));
  const body = {
    target_kind: kind,
    title: (document.getElementById('spcName').value || '').trim(),
    body: (document.getElementById('spcBody').value || '').trim(),
    button_text: (document.getElementById('spcButton').value || '').trim(),
    bot_ids: ids('spc-bot'), channel_ids: ids('spc-ch'),
    segment: document.getElementById('spcSegment').value,
    skip_target_audience: document.getElementById('spcSkip').checked,
    cooldown_days: Number(document.getElementById('spcCooldown').value),
    auto_days: Number(document.getElementById('spcAuto').value),
  };
  if (kind !== 'link') body.target_ref = Number(document.getElementById('spcTarget').value) || null;
  if (document.getElementById('spcUrlWrap').style.display !== 'none') {
    body.target_url = (document.getElementById('spcUrl').value || '').trim();
  }
  if (SPC.edit) body.id = SPC.edit;
  return body;
}

function spcPreviewSoon() {
  clearTimeout(SPC.previewTimer);
  SPC.previewTimer = setTimeout(spcPreview, 400);
}

// Что именно произойдёт при запуске — словами и точными числами.
function _spcCountsText(c) {
  const lines = [];
  if (c.will_send) lines.push(`📨 В личку уйдёт ${_spcPeople(c.will_send)}`);
  else if (c.candidates) lines.push('📨 В личку сейчас не уйдёт никому');
  if (c.channel_posts) lines.push(`📡 Пост выйдет в ${_spcExact(c.channel_posts)} ${plural(c.channel_posts, 'канале', 'каналах', 'каналах')}`);
  const skipped = [];
  if (c.got_it) skipped.push(`${_spcExact(c.got_it)} уже получали эту кампанию`);
  if (c.in_target) skipped.push(`${_spcExact(c.in_target)} уже подписаны на продвигаемого бота`);
  if (c.cooling) skipped.push(`${_spcExact(c.cooling)} недавно получали другой самопиар`);
  if (c.over_cap) skipped.push(`${_spcExact(c.over_cap)} — в следующий запуск (за раз не больше ${_spcExact(SPC.data.limits.per_launch)})`);
  if (skipped.length) lines.push('Не получат: ' + skipped.join('; '));
  return lines.join('\n');
}

async function spcPreview() {
  const box = document.getElementById('spcPreview');
  if (!box) return;
  const body = _spcForm();
  // Пока текста нет, сервер откажет — для подсчёта аудитории он не важен.
  if (!body.body) body.body = '…';
  try {
    const r = await api('/api/miniapp/selfpromo/preview', {method: 'POST', body: JSON.stringify(body)});
    SPC.counts = r.counts;
    box.style.color = '';
    box.textContent = _spcCountsText(r.counts) || 'Выберите, где показывать';
  } catch (e) {
    SPC.counts = null;
    box.style.color = 'var(--hint)';
    box.textContent = errRu(e, 'Не удалось посчитать аудиторию');
  }
}

async function spcPickText(ai, btn) {
  const body = _spcForm();
  if (btn) btn.disabled = true;
  let r;
  try {
    r = await api('/api/miniapp/selfpromo/texts', {method: 'POST', body: JSON.stringify({
      target_kind: body.target_kind, target_ref: body.target_ref, target_url: body.target_url || '', ai: !!ai})});
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось получить варианты'));
    return;
  } finally {
    if (btn) btn.disabled = false;
  }
  const opts = [];
  for (const t of (r.ai || [])) opts.push({value: {body: t}, label: '✨ ' + t.slice(0, 60) + (t.length > 60 ? '…' : ''), sub: t.length > 60 ? t : ''});
  if (ai && !(r.ai || []).length) toast('ИИ не ответил или не подключён — показываю готовые тексты');
  for (const p of (r.presets || [])) opts.push({value: p, label: '📋 ' + p.body.slice(0, 60) + (p.body.length > 60 ? '…' : ''), sub: p.body.length > 60 ? p.body : ''});
  const pick = await askChoice('Выберите текст — его можно поправить', opts);
  if (!pick) return;
  document.getElementById('spcBody').value = pick.body;
  if (pick.button_text && !document.getElementById('spcButton').value.trim()) {
    document.getElementById('spcButton').value = pick.button_text;
  }
  spcCount();
}

function _spcFail(m) {
  const el = document.getElementById('spcErr');
  el.textContent = m; el.style.display = 'block';
  tg.HapticFeedback?.notificationOccurred('error');
}

async function spcTest() {
  document.getElementById('spcErr').style.display = 'none';
  const body = _spcForm();
  if (!body.body) return _spcFail('Введите текст сообщения');
  const btn = document.getElementById('spcTestBtn');
  btn.disabled = true; btn.textContent = '⏳ Отправка…';
  try {
    const r = await api('/api/miniapp/selfpromo/test', {method: 'POST', body: JSON.stringify(body)});
    tg.HapticFeedback?.notificationOccurred('success');
    toast(`✅ Пробное сообщение у вас в личке с @${r.bot}`);
  } catch (e) {
    _spcFail(errRu(e, 'Не удалось отправить пробное'));
  } finally {
    btn.disabled = false; btn.textContent = '🧪 Отправить пробное себе';
  }
}

async function spcSave(andLaunch) {
  document.getElementById('spcErr').style.display = 'none';
  const body = _spcForm();
  if (!body.body) return _spcFail('Введите текст сообщения');
  const btn = document.getElementById(andLaunch ? 'spcLaunchBtn' : 'spcSaveBtn');
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Сохранение…';
  let id;
  try {
    const r = await api('/api/miniapp/selfpromo/campaign', {method: 'POST', body: JSON.stringify(body)});
    id = Number(r.id);
    SPC.edit = id;
  } catch (e) {
    btn.disabled = false; btn.textContent = label;
    return _spcFail(errRu(e, 'Не удалось сохранить кампанию'));
  }
  btn.disabled = false; btn.textContent = label;
  if (andLaunch && !await _spcConfirmAndLaunch(id, body)) {
    toast('💾 Кампания сохранена, запуск отменён');
  } else if (!andLaunch) {
    toast('💾 Кампания сохранена');
  }
  back();
  spcLoad();
}

// Подтверждение называет точные числа: сообщения в личку отозвать нельзя.
async function _spcConfirmAndLaunch(id, body) {
  let counts = null;
  try {
    const r = await api('/api/miniapp/selfpromo/preview', {method: 'POST', body: JSON.stringify(Object.assign({}, body, {id}))});
    counts = r.counts;
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось посчитать аудиторию'));
    return false;
  }
  if (!counts.will_send && !counts.channel_posts) {
    showInfo('Сейчас отправлять некому.\n\n' + (_spcCountsText(counts) || 'Подходящих людей нет.'));
    return false;
  }
  if (!await askConfirm('Запустить самопиар?\n\n' + _spcCountsText(counts)
      + '\n\nСообщения в личку отозвать нельзя. Рассылку видно и можно остановить в «Операциях».')) return false;
  try {
    const r = await api(`/api/miniapp/selfpromo/campaign/${Number(id)}/launch`, {method: 'POST', body: '{}'});
    tg.HapticFeedback?.notificationOccurred('success');
    toast(`🚀 Запущено: ${_spcPeople(r.recipients)}` + (r.channel_posts ? ` и ${_spcExact(r.channel_posts)} ${plural(r.channel_posts, 'канал', 'канала', 'каналов')}` : ''));
    return true;
  } catch (e) {
    showInfo(errRu(e, 'Не удалось запустить кампанию'));
    return false;
  }
}

async function spcLaunch(id, btn) {
  const c = (SPC.data.campaigns || []).find(x => Number(x.id) === Number(id));
  if (!c) return;
  btn.disabled = true;
  try {
    const body = {id: Number(c.id), target_kind: c.target_kind, target_ref: c.target_ref,
      target_url: c.target_kind === 'bot' ? '' : c.target_url, title: c.title, body: c.body,
      button_text: c.button_text, bot_ids: c.bot_ids, channel_ids: c.channel_ids, segment: c.segment,
      skip_target_audience: c.skip_target_audience, cooldown_days: c.cooldown_days, auto_days: c.auto_days};
    if (await _spcConfirmAndLaunch(c.id, body)) spcLoad();
  } finally {
    btn.disabled = false;
  }
}

async function spcToggle(id) {
  const c = (SPC.data.campaigns || []).find(x => Number(x.id) === Number(id));
  if (!c) return;
  const status = c.status === 'active' ? 'paused' : 'active';
  try {
    await api(`/api/miniapp/selfpromo/campaign/${Number(id)}/status`, {method: 'POST', body: JSON.stringify({status})});
    toast(status === 'active' ? '▶ Автопоказ включён' : '⏸ Автопоказ на паузе');
    spcLoad();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось изменить'));
  }
}

async function spcDelete(id, el) {
  if (!await confirmDelete('Удалить кампанию', el, 'Уже отправленные сообщения останутся у людей, история кампании будет удалена.')) return;
  try {
    await api(`/api/miniapp/selfpromo/campaign/${Number(id)}/delete`, {method: 'POST', body: '{}'});
    toast('🗑 Кампания удалена');
    spcLoad();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось удалить'));
  }
}
