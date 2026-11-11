// «🔭 Радар ниши» — поиск чужих ресурсов по нише и присутствие в них.
//
// Сервер: services/mini_app_niche.py (логика — services/niche_radar.py, работа —
// операции niche_scan и niche_presence). Экран: кампания ниши, запуск поиска,
// каталог найденного и запуск присутствия (вступление, комментарии, реакции).
// Состояние — объект NR, объявлен в index.html.

const _nrNum = n => Number(n || 0).toLocaleString('ru-RU');

async function openNiche() {
  push('s-niche');
  nrLoad();
}

async function nrLoad() {
  const box = document.getElementById('nrList');
  if (!box) return;
  box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  nrCloseForm();
  try {
    NR.data = await api('/api/miniapp/niche/overview');
  } catch (e) {
    box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить радар'), 'nrLoad()');
    return;
  }
  const rows = NR.data.campaigns || [];
  txt('nrAccNote', NR.data.accounts
    ? `Работают ${_nrNum(NR.data.accounts)} ${plural(NR.data.accounts, 'аккаунт', 'аккаунта', 'аккаунтов')} флота`
    : 'Нет рабочих аккаунтов — поиск и присутствие им не на чем делать');
  box.innerHTML = rows.length ? rows.map(_nrRow).join('')
    : empty('🔭', 'Ниш пока нет', 'Опишите нишу — агент найдёт по ней максимум чатов и каналов, флот вступит и будет там работать.', {label: '+ Добавить нишу', fn: 'nrOpenForm()'});
}

function _nrRow(c) {
  const s = c.stats || {};
  const acts = s.by_kind || {};
  const line2 = [
    `найдено ${_nrNum(s.found)}`,
    s.joined ? `вступили в ${_nrNum(s.joined)}` : null,
    acts.comment ? `комментариев ${_nrNum(acts.comment.ok)}` : null,
    acts.reaction ? `реакций ${_nrNum(acts.reaction.ok)}` : null,
  ].filter(Boolean).join(' · ');
  const reach = s.reach ? `охват найденного: ${_nrNum(s.reach)} ${plural(s.reach, 'участник', 'участника', 'участников')}` : 'ресурсов пока нет';
  const id = Number(c.id);
  return `<div class="row" style="flex-wrap:wrap">
    <div class="row-ico" style="background:var(--bg-green-16)">🔭</div>
    <div class="row-body">
      <div class="row-name">${esc(c.title)}</div>
      <div class="row-val" style="white-space:normal">${esc(line2)}</div>
      <div class="row-val" style="color:var(--hint)">${esc(reach)}</div>
    </div>
    <div style="display:flex;gap:6px;width:100%;margin-top:8px;flex-wrap:wrap">
      <button class="btn btn-p" style="flex:1;min-height:34px;font-size:13px;padding:6px" onclick="nrScan(${id}, this)">🔭 Искать ещё</button>
      <button class="btn btn-s" style="flex:1;min-height:34px;font-size:13px;padding:6px" onclick="nrOpenAssets(${id})">📋 Каталог (${esc(_nrNum(s.found))})</button>
      <button class="btn btn-s" style="flex:1;min-height:34px;font-size:13px;padding:6px" onclick="nrOpenForm(${id})">✏️</button>
      <button class="icon-btn sm danger" style="min-width:34px;min-height:34px" onclick="nrDelete(${id}, this)" aria-label="Удалить нишу" title="Удалить">🗑</button>
    </div>
  </div>`;
}

// ── Кампания ───────────────────────────────────────────────────────────────

function nrOpenForm(id) {
  if (!NR.data) return;
  const c = id ? (NR.data.campaigns || []).find(x => Number(x.id) === Number(id)) : null;
  NR.edit = c ? Number(c.id) : null;
  txt('nrFormSec', c ? 'Изменить нишу' : 'Новая ниша');
  const sel = document.getElementById('nrTone');
  sel.innerHTML = '';
  for (const t of (NR.data.tones || [])) sel.add(new Option(t.label, t.key));
  sel.value = (c && c.tone) || 'neutral';
  document.getElementById('nrNiche').value = (c && c.niche) || '';
  document.getElementById('nrGeo').value = (c && c.geo) || '';
  document.getElementById('nrKeywords').value = c ? (c.keywords || []).join('\n') : '';
  document.getElementById('nrPromo').value = (c && c.promo_text) || '';
  document.getElementById('nrLink').value = (c && c.promo_link) || '';
  document.getElementById('nrMinMembers').value = String((c && c.min_members) ?? 100);
  document.getElementById('nrFormErr').style.display = 'none';
  txt('nrPreview', '');
  document.getElementById('nrForm').style.display = 'block';
  document.getElementById('nrForm').scrollIntoView({behavior: 'smooth', block: 'center'});
}

function nrCloseForm() {
  const f = document.getElementById('nrForm');
  if (f) f.style.display = 'none';
}

function _nrForm() {
  const body = {
    niche: (document.getElementById('nrNiche').value || '').trim(),
    geo: (document.getElementById('nrGeo').value || '').trim(),
    keywords: (document.getElementById('nrKeywords').value || '').split('\n').map(s => s.trim()).filter(Boolean),
    promo_text: (document.getElementById('nrPromo').value || '').trim(),
    promo_link: (document.getElementById('nrLink').value || '').trim(),
    tone: document.getElementById('nrTone').value,
    min_members: Number(document.getElementById('nrMinMembers').value) || 0,
  };
  if (NR.edit) body.id = NR.edit;
  return body;
}

// Какие запросы уйдут в поиск — видно до запуска, вместе с вариантами от ИИ.
async function nrPreviewKeywords(ai, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/miniapp/niche/keywords', {method: 'POST', body: JSON.stringify(Object.assign(_nrForm(), {ai: !!ai}))});
    const words = r.keywords || [];
    txt('nrPreview', words.length
      ? `Поиск пойдёт по ${words.length} запросам: ${words.slice(0, 12).join(', ')}${words.length > 12 ? '…' : ''}`
        + (ai && !(r.ai || []).length ? '\nИИ не ответил или не подключён — запросы собраны по правилам.' : '')
      : 'Запросы не собрались — уточните нишу.');
  } catch (e) {
    txt('nrPreview', errRu(e, 'Не удалось собрать запросы'));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function nrSave(andScan) {
  const err = document.getElementById('nrFormErr');
  err.style.display = 'none';
  const btn = document.getElementById(andScan ? 'nrSaveScanBtn' : 'nrSaveBtn');
  btn.disabled = true;
  let id;
  try {
    const r = await api('/api/miniapp/niche/campaign', {method: 'POST', body: JSON.stringify(_nrForm())});
    id = Number(r.id);
  } catch (e) {
    err.textContent = errRu(e, 'Не удалось сохранить нишу');
    err.style.display = 'block';
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  toast('💾 Ниша сохранена');
  if (andScan) await nrScan(id, null);
  nrLoad();
}

async function nrScan(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const d = await api(`/api/miniapp/niche/campaign/${Number(id)}/scan`, {method: 'POST', body: '{}'});
    tg.HapticFeedback?.notificationOccurred('success');
    toast('🔭 Поиск запущен — находки появятся в каталоге, ход видно в «Операциях»');
    if (NR.openCampaign === Number(id)) nrOpenAssets(Number(id));
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось запустить поиск'));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function nrDelete(id, el) {
  if (!await confirmDelete('Удалить нишу', el, 'Каталог найденных ресурсов и журнал действий будут удалены. Аккаунты останутся в чатах, куда уже вступили.')) return;
  try {
    await api(`/api/miniapp/niche/campaign/${Number(id)}/delete`, {method: 'POST', body: '{}'});
    toast('🗑 Ниша удалена');
    nrLoad();
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось удалить'));
  }
}

// ── Каталог ────────────────────────────────────────────────────────────────

async function nrOpenAssets(id, status) {
  const cid = Number(id);
  NR.openCampaign = cid;
  NR.status = status || '';
  if (CUR !== 'niche-assets') push('s-niche-assets');
  const c = (NR.data.campaigns || []).find(x => Number(x.id) === Number(id));
  txt('nrAssetsTitle', '📋 ' + ((c && c.title) || 'Каталог ниши'));
  const box = document.getElementById('nrAssets');
  box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  let d;
  try {
    d = await api(`/api/miniapp/niche/campaign/${cid}/assets?limit=60&status=${encodeURIComponent(NR.status)}`);
  } catch (e) {
    box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить каталог'), 'nrOpenAssets(' + esc(cid) + ')');
    return;
  }
  const s = d.stats || {};
  const chip = (v, l) => `<div class="kpi-chip"><div class="kpi-chip-val">${esc(v)}</div><div class="kpi-chip-lbl">${esc(l)}</div></div>`;
  document.getElementById('nrAssetsKpis').innerHTML =
    chip(_nrNum(s.found), 'найдено') + chip(_nrNum(s.joined), 'вступили')
    + chip(_nrNum(s.actions), 'действий') + chip(_nrNum(s.reach), 'охват');
  const items = d.items || [];
  box.innerHTML = items.length ? items.map(a => `
    <div class="row" style="flex-wrap:wrap">
      <div class="row-ico" style="background:${a.status === 'joined' ? 'var(--bg-green-16)' : 'var(--bg3)'}">${a.kind === 'channel' ? '📢' : '💬'}</div>
      <div class="row-body">
        <div class="row-name">${esc(a.title || '@' + a.username)}</div>
        <div class="row-val">@${esc(a.username)} · ${esc(_nrNum(a.members))} уч. · ${esc(a.status_ru)}</div>
        <div class="row-val" style="color:var(--hint)">${esc(a.source_ru)}${a.keyword ? ' · «' + esc(a.keyword) + '»' : ''}${a.actions_done ? ' · действий: ' + esc(_nrNum(a.actions_done)) : ''}</div>
      </div>
      <div style="display:flex;gap:6px;width:100%;margin-top:6px">
        <button class="btn btn-s" style="flex:1;min-height:32px;font-size:13px;padding:6px" onclick="openTgLink('https://t.me/${esc(a.username)}')">Открыть</button>
        ${a.kind === 'group' ? `<button class="btn btn-s" style="flex:1;min-height:32px;font-size:13px;padding:6px" onclick="startChatWarmup('${esc(a.username)}', this)">🔥 Прогрев флотом</button>` : ''}
        ${a.status !== 'skipped' ? `<button class="btn btn-s" style="flex:1;min-height:32px;font-size:13px;padding:6px" onclick="nrSkip(${Number(a.id)}, this)">Не наше</button>` : ''}
      </div>
    </div>`).join('') + (d.has_more ? `<div class="field-note" style="padding:8px 4px">Показаны первые ${esc(String(items.length))} из ${esc(_nrNum(d.total))} — остальные в работе у присутствия.</div>` : '')
    : empty('📋', NR.status ? 'В этом срезе пусто' : 'Каталог пуст', 'Запустите поиск — агент наберёт чаты и каналы ниши.', {label: '🔭 Искать', fn: 'nrScan(' + esc(cid) + ', null)'});
}

function nrFilter(status) {
  nrOpenAssets(NR.openCampaign, status);
}

async function nrSkip(assetId, el) {
  try {
    await api(`/api/miniapp/niche/asset/${Number(assetId)}/skip`, {method: 'POST', body: '{}'});
    if (el && el.closest('.row')) el.closest('.row').style.opacity = '.4';
    toast('Убрали из работы');
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось убрать'));
  }
}

// ── Присутствие ────────────────────────────────────────────────────────────

async function nrPresence() {
  const id = NR.openCampaign;
  if (!id) return;
  const actions = [...document.querySelectorAll('#s-niche-assets .nr-act')].filter(c => c.checked).map(c => c.value);
  if (!actions.length) { toast('Отметьте, что делать: вступать, комментировать, ставить реакции'); return; }
  const limit = Number(document.getElementById('nrLimit').value) || 12;
  const accs = Number(document.getElementById('nrAccCount').value) || 3;
  const names = {join: 'вступление', comment: 'комментарии', reaction: 'реакции'};
  if (!await askConfirm(`Запустить присутствие в нише?\n\nЧто делаем: ${actions.map(a => names[a]).join(', ')}.\n`
      + `Аккаунтов: ${accs}, действий за проход: не больше ${limit}.\n\n`
      + 'Действия идут от ваших аккаунтов в ЧУЖИХ чатах, с паузами. Ход и остановка — в «Операциях».')) return;
  const btn = document.getElementById('nrPresenceBtn');
  btn.disabled = true;
  try {
    const d = await api(`/api/miniapp/niche/campaign/${Number(id)}/presence`, {method: 'POST', body: JSON.stringify({actions, limit, acc_count: accs})});
    tg.HapticFeedback?.notificationOccurred('success');
    toast(`🤝 Запущено: до ${_nrNum(d.targets)} ресурсов за проход`);
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось запустить присутствие'));
  } finally {
    btn.disabled = false;
  }
}

async function nrOpenLog() {
  const id = NR.openCampaign;
  if (!id) return;
  const box = document.getElementById('nrLog');
  box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api(`/api/miniapp/niche/campaign/${Number(id)}/log`);
    const kinds = {join: '➕ вступление', comment: '💬 комментарий', reaction: '👍 реакция', error: '⚠️ ошибка'};
    box.innerHTML = (d.items || []).length ? d.items.map(a => `
      <div class="row">
        <div class="row-ico" style="background:${a.status === 'ok' ? 'var(--bg-green-16)' : 'var(--bg-red-16)'}">${a.status === 'ok' ? '✓' : '!'}</div>
        <div class="row-body">
          <div class="row-name">${esc(kinds[a.kind] || a.kind)} · ${esc(a.title || '@' + a.username)}</div>
          <div class="row-val" style="white-space:normal;color:var(--hint)">${esc((a.detail || '').slice(0, 160))}</div>
        </div>
      </div>`).join('')
      : empty('📝', 'Действий ещё не было', 'Запустите присутствие — здесь появится, что и где сделали.');
  } catch (e) {
    box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить журнал'), 'nrOpenLog()');
  }
}
