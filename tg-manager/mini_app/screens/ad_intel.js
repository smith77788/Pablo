// ── Разведка рекламы: списки и карточки (screens/ad_intel.js) ──────────────
// Экран и KPI открывает openAdIntel (index.html), состояние — глобальный _adi.
// Здесь: вкладки «Каналы / Рекламодатели / Объявления», поиск, сортировка,
// фильтры, «Показать ещё», карточка канала и карточка рекламодателя.
//
// Доли приходят ДОЛЯМИ: reach_rate 0.35 = охват 35%. Прежний экран печатал
// долю как проценты («ER 0,4%» при охвате 35%).
//
// Классический скрипт: грузится после основного блока index.html.
'use strict';

const ADI_SORTS = {
  channels: [['quality','По качеству'],['subscribers','По подписчикам'],['reach','По охвату'],
             ['engagement','По вовлечённости'],['ads','По частоте рекламы'],['price','Сначала дешёвые'],
             ['recent','Недавно обновлённые']],
  advertisers: [['placements','По числу размещений'],['channels','По числу каналов'],
                ['views','По просмотрам'],['recent','Недавние']],
  ads: [['recent','Сначала новые']],
};
const ADI_KINDS = [['','Все'],['channel','Каналы'],['bot','Боты'],['site','Сайты'],['invite','Закрытые']];
const ADI_PAGE = 30;

function adiPct(x, digits) {
  const v = (Number(x)||0) * 100;
  return v.toFixed(digits == null ? (v < 10 ? 1 : 0) : digits).replace('.', ',') + '%';
}
function adiRub(n) { return n ? '~' + num(n) + ' ₽' : '—'; }
function adiAgo(iso) { return iso ? ago(iso) : ''; }
// Сайт рекламодателя — во внешнем браузере: openTelegramLink открывает только t.me.
function adiOpenSite(domain) {
  const url = 'https://' + String(domain || '').replace(/^https?:\/\//, '');
  try { if (tg && tg.openLink) { tg.openLink(url, {try_instant_view:false}); return; } } catch(_) {}
  try { window.open(url, '_blank'); } catch(_) { copyToClipboard(url, 'Ссылка скопирована'); }
}

// ── Вкладки ──────────────────────────────────────────────────────────────

function adIntelShowTab(tab) {
  _adi.tab = tab;
  document.querySelectorAll('#adIntelTabs .chip').forEach(c =>
    c.classList.toggle('on', c.dataset.tab === tab));
  const ids = {channels:'adIntelChannels', advertisers:'adIntelAdv', ads:'adIntelAds'};
  Object.entries(ids).forEach(([t,id]) => {
    const el = document.getElementById(id);
    if (el) el.style.display = t === tab ? '' : 'none';
  });
  const sel = document.getElementById('adIntelSort');
  if (sel) {
    const opts = ADI_SORTS[tab] || [];
    if (!opts.some(o => o[0] === _adi.sort)) _adi.sort = opts.length ? opts[0][0] : '';
    sel.innerHTML = opts.map(([k,l]) => `<option value="${esc(k)}"${k===_adi.sort?' selected':''}>${esc(l)}</option>`).join('');
    sel.style.display = opts.length > 1 ? '' : 'none';
  }
  const q = document.getElementById('adIntelQ');
  if (q) q.placeholder = tab === 'ads' ? 'Поиск по тексту или рекламодателю'
                      : tab === 'advertisers' ? 'Поиск рекламодателя' : 'Поиск канала';
}

function adIntelTab(tab) {
  if (_adi.tab !== tab) { _adi.niche = ''; _adi.kind = ''; }
  adIntelShowTab(tab);
  adIntelReload();
}

function adIntelSearch() {
  clearTimeout(_adiSearchT);
  _adiSearchT = setTimeout(adIntelReload, 350);
}

function adIntelSetFilter(key, val) {
  _adi[key] = (key === 'clean' || key === 'erid') ? !_adi[key] : val;
  adIntelReload();
}

function adIntelReload() {
  _adi.q = (document.getElementById('adIntelQ')?.value || '').trim();
  const sel = document.getElementById('adIntelSort');
  if (sel && sel.value) _adi.sort = sel.value;
  _adi.offset = 0;
  adIntelShowTab(_adi.tab);
  adIntelLoad(false);
}

function adIntelMore() {
  _adi.offset += ADI_PAGE;
  adIntelLoad(true);
}

async function adIntelLoad(append) {
  const tab = _adi.tab;
  const box = {channels:'adIntelChannels', advertisers:'adIntelAdv', ads:'adIntelAds'}[tab];
  const el = document.getElementById(box);
  if (!el) return;
  if (!append) el.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  txt('adIntelMore', '');
  const p = new URLSearchParams({limit: ADI_PAGE, offset: _adi.offset});
  if (_adi.q) p.set('q', _adi.q);
  if (_adi.sort) p.set('sort', _adi.sort);
  let url;
  if (tab === 'channels') {
    if (_adi.niche) p.set('niche', _adi.niche);
    if (_adi.clean) p.set('clean', '1');
    url = '/api/miniapp/ad_intel/channels?' + p;
  } else if (tab === 'advertisers') {
    if (_adi.kind) p.set('kind', _adi.kind);
    url = '/api/miniapp/ad_intel/advertisers?' + p;
  } else {
    if (_adi.erid) p.set('erid', '1');
    if (_adi.days) p.set('days', _adi.days);
    url = '/api/miniapp/ad_intel/ads?' + p;
  }
  try {
    const d = await api(url);
    const items = d.items || [];
    if (tab === 'channels') adIntelFiltersChannels(d.niches || []);
    else if (tab === 'advertisers') adIntelFiltersAdv();
    else adIntelFiltersAds();
    const html = tab === 'channels' ? items.map(adIntelChannelRow).join('')
               : tab === 'advertisers' ? items.map(adIntelAdvRow).join('')
               : items.map(a => adIntelAdRow(a, true)).join('');
    if (!append && !items.length) {
      const filtered = _adi.q || _adi.niche || _adi.kind || _adi.clean || _adi.erid || _adi.days;
      el.innerHTML = filtered
        ? empty('🔍', 'Ничего не найдено', 'Измените поиск или снимите фильтр')
        : tab === 'advertisers'
          ? empty('🏢', 'Рекламодателей пока нет', 'В рекламе добавленных каналов ещё не нашлось ссылок на рекламодателей. Разведка обновляет каналы раз в сутки и дополнит список сама', {label:'➕ Добавить каналы', fn:'openAdIntelModal()'})
          : empty('📰', 'Рекламных постов пока нет', 'В последних 100 постах добавленных каналов реклама не найдена. Разведка обновляет каналы раз в сутки', {label:'➕ Добавить каналы', fn:'openAdIntelModal()'});
    } else if (append) {
      el.insertAdjacentHTML('beforeend', html);
    } else {
      el.innerHTML = html;
    }
    const shown = _adi.offset + items.length;
    txt('adIntelMore', d.has_more
      ? `<button class="btn btn-s" style="width:100%" onclick="adIntelMore()">Показать ещё · ${num(shown)} из ${num(d.total||0)}</button>`
      : (d.total ? `<div class="field-note" style="text-align:center">Всего: ${num(d.total)}</div>` : ''));
  } catch (e) {
    if (!append) el.innerHTML = errHtml(errRu(e), 'adIntelReload()');
    else toast('⚠️ ' + errRu(e));
  }
}

function adiChip(label, on, fn) {
  return `<span class="chip${on ? ' on' : ''}" role="button" tabindex="0" onclick="${fn}">${esc(label)}</span>`;
}

function adIntelFiltersChannels(niches) {
  const chips = [adiChip('Все ниши', !_adi.niche, "adIntelSetFilter('niche','')")]
    .concat(niches.map(n => adiChip(`${n.niche} · ${n.count}`, _adi.niche === n.niche,
      `adIntelSetFilter('niche','${escJs(n.niche)}')`)));
  chips.push(adiChip('✅ Без признаков накрутки', _adi.clean, "adIntelSetFilter('clean')"));
  txt('adIntelFilters', chips.join(''));
}
function adIntelFiltersAdv() {
  txt('adIntelFilters', ADI_KINDS.map(([k,l]) =>
    adiChip(l, _adi.kind === k, `adIntelSetFilter('kind','${k}')`)).join(''));
}
function adIntelFiltersAds() {
  const chips = [[0,'За всё время'],[7,'7 дней'],[30,'30 дней']].map(([d,l]) =>
    adiChip(l, (_adi.days||0) === d, `adIntelSetFilter('days',${d})`));
  chips.push(adiChip('С маркировкой erid', _adi.erid, "adIntelSetFilter('erid')"));
  txt('adIntelFilters', chips.join(''));
}

// ── Строки ───────────────────────────────────────────────────────────────

function adIntelChannelRow(c) {
  const nm = c.title || (c.username ? '@' + c.username : 'Канал');
  const flags = (c.fraud_flags || []).length;
  const sub = [
    c.username ? '@' + esc(c.username) : '',
    num(c.subscribers || 0) + ' подписчиков',
    'охват ' + adiPct(c.reach_rate),
    'ER ' + adiPct(c.engagement_rate),
    c.ads_per_week ? String(c.ads_per_week).replace('.', ',') + ' рекл./нед.' : 'без рекламы',
    c.ad_price_rub ? 'пост ' + adiRub(c.ad_price_rub) : '',
  ].filter(Boolean).join(' · ');
  const warn = c.last_error ? '⚠️ ' : (flags ? '🚩 ' : '');
  return `
    <div class="row tap" role="button" tabindex="0" onclick="openAdChannel('${escJs(c.username)}')">
      <div class="row-ico" style="background:var(--bg-yellow-16)">📺</div>
      <div class="row-body">
        <div class="row-name">${warn}${esc(nm)}</div>
        <div class="row-val">${sub}</div>
      </div>
      <div class="row-right" style="display:flex;align-items:center;gap:5px"><span style="color:var(--orange);font-size:13px" title="Качество для размещения">${(Number(c.quality_score)||0).toFixed(0)}/100</span><span class="chev">›</span></div>
    </div>`;
}

function adIntelAdvRow(a) {
  const ico = {bot:'🤖', site:'🌐', invite:'🔒'}[a.kind] || '📣';
  const nm = a.kind === 'site' || a.kind === 'invite' ? a.username : '@' + a.username;
  return `
    <div class="row tap" role="button" tabindex="0" onclick="openAdAdvertiser('${escJs(a.username)}')">
      <div class="row-ico" style="background:var(--bg-violet-16)">${ico}</div>
      <div class="row-body">
        <div class="row-name">${esc(nm)}</div>
        <div class="row-val">${esc(a.kind_label || '')}${a.kind_label ? ' · ' : ''}${num(a.placements||0)} ${plural(a.placements||0,'размещение','размещения','размещений')} в ${num(a.channels||0)} ${plural(a.channels||0,'канале','каналах','каналах')} · ${num(a.views||0)} просмотров</div>
      </div>
      <div class="row-right" style="display:flex;align-items:center;gap:5px"><span style="color:var(--hint);font-size:13px">${adiAgo(a.last_seen)}</span><span class="chev">›</span></div>
    </div>`;
}

function adIntelAdRow(a, showChannel) {
  const who = a.advertiser
    ? `<span class="tap" role="button" tabindex="0" style="color:var(--accent)" onclick="event.stopPropagation();openAdAdvertiser('${escJs(a.advertiser)}')">${esc(a.advertiser_kind === 'site' || a.advertiser_kind === 'invite' ? a.advertiser : '@' + a.advertiser)}</span>`
    : '<span style="color:var(--hint)">рекламодатель не определён</span>';
  const where = showChannel && a.channel
    ? `<span class="tap" role="button" tabindex="0" onclick="event.stopPropagation();openAdChannel('${escJs(a.channel)}')">в ${esc(a.channel_title || '@' + a.channel)}</span> · ` : '';
  const badge = a.has_erid ? '<span style="color:var(--green)">erid</span> · ' : '';
  const why = (a.reasons || []).slice(0, 3).join(', ');
  const open = a.channel && a.msg_id ? `openTgUser('${escJs(a.channel)}/${Number(a.msg_id)}')` : '';
  return `
    <div class="card${open ? ' tap' : ''}" ${open ? `role="button" tabindex="0" onclick="${open}"` : ''} style="margin:0 16px 8px">
      <div style="font-size:12px;color:var(--hint);margin-bottom:4px">${where}${badge}${adiAgo(a.date)} · ${num(a.views||0)} просмотров · ER ${adiPct(a.er)}</div>
      <div style="font-size:13px;white-space:pre-wrap;max-height:7.5em;overflow:hidden">${esc(a.text || '')}</div>
      <div style="font-size:12px;margin-top:6px">Рекламодатель: ${who}</div>
      ${why ? `<div style="font-size:11px;color:var(--hint);margin-top:2px">Почему реклама: ${esc(why)} · уверенность ${Number(a.confidence)||0}%</div>` : ''}
    </div>`;
}

// ── Карточка канала ──────────────────────────────────────────────────────

async function openAdChannel(username) {
  push('s-adchan');
  txt('adChanTitle', '📺 @' + esc(username));
  txt('adChanBody', '<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const c = await api('/api/miniapp/ad_intel/channel/' + encodeURIComponent(username));
    txt('adChanTitle', '📺 ' + esc(c.title || '@' + c.username));
    const kpi = (v, l) => `<div class="kpi-card"><div class="kpi-val">${v}</div><div class="kpi-lbl">${l}</div></div>`;
    const flags = (c.fraud_flags || []);
    const err = c.last_error ? `<div class="card" style="margin:0 16px 8px;font-size:13px">⚠️ Последнее обновление не удалось: ${esc(c.last_error)}</div>` : '';
    const fraud = flags.length
      ? `<div class="card" style="margin:0 16px 8px;font-size:13px">🚩 Признаки накрутки:<br>${flags.map(f => '• ' + esc(f)).join('<br>')}</div>`
      : (c.posts_scanned ? '<div class="field-note" style="padding:0 16px 8px">✅ Признаков накрутки не найдено</div>' : '');
    const advs = (c.advertisers || []).map(a => adIntelAdvRow({
      username: a.username, kind: a.kind, kind_label: a.kind_label,
      placements: a.posts, channels: 1, views: a.views, last_seen: a.last_seen})).join('');
    const ads = (c.ads || []).map(a => adIntelAdRow(a, false)).join('');
    txt('adChanBody', `
      ${err}
      <div class="kpi-row" style="padding:8px 16px 10px">
        ${kpi((Number(c.quality_score)||0).toFixed(0) + '<span style="font-size:12px;color:var(--hint)">/100</span>', 'Качество')}
        ${kpi(num(c.subscribers||0), 'Подписчиков')}
        ${kpi(adiPct(c.reach_rate), 'Охват поста')}
        ${kpi(adiPct(c.engagement_rate), 'Вовлечённость')}
      </div>
      <div class="lst">
        <div class="row"><div class="row-body"><div class="row-name">Средние просмотры поста</div></div><div class="row-right">${num(c.views_avg||0)}</div></div>
        <div class="row"><div class="row-body"><div class="row-name">Оценка цены поста</div><div class="row-val">просмотры × средний CPM ниши ÷ 1000 — для сравнения каналов, не прайс</div></div><div class="row-right">${adiRub(c.ad_price_rub)}</div></div>
        <div class="row"><div class="row-body"><div class="row-name">Реклама</div><div class="row-val">доля среди последних ${num(c.posts_scanned||0)} постов</div></div><div class="row-right">${String(c.ads_per_week||0).replace('.', ',')} в неделю · ${adiPct(c.ad_share, 0)}</div></div>
        <div class="row"><div class="row-body"><div class="row-name">Ниши</div></div><div class="row-right" style="max-width:60%;text-align:right">${esc((c.niches||[]).join(', ') || 'не определены')}</div></div>
        <div class="row"><div class="row-body"><div class="row-name">Обновлено</div></div><div class="row-right">${c.last_scanned ? adiAgo(c.last_scanned) : '—'}</div></div>
        <label class="row" style="cursor:pointer"><div class="row-body"><div class="row-name">Обновлять раз в сутки</div><div class="row-val">Аккаунт только читает последние посты</div></div><div class="row-right"><input type="checkbox" ${c.auto_rescan ? 'checked' : ''} onchange="adChannelAuto('${escJs(c.username)}', this.checked)"></div></label>
      </div>
      ${fraud}
      <div style="display:flex;gap:8px;padding:8px 16px">
        <button class="btn btn-p" style="flex:1 1 auto" onclick="adChannelRescan('${escJs(c.username)}')">🔄 Обновить сейчас</button>
        <button class="btn btn-s" style="flex:1 1 auto" onclick="openTgUser('${escJs(c.username)}')">Открыть в Telegram</button>
      </div>
      <div class="sec">Рекламодатели в канале</div>
      <div class="lst">${advs || empty('🏢', 'Рекламодателей не найдено', 'В рекламе этого канала нет ссылок, по которым можно назвать рекламодателя')}</div>
      <div class="sec" style="margin-top:8px">Рекламные посты</div>
      ${ads || empty('📰', 'Рекламы не найдено', 'Среди последних постов рекламы нет — это плюс для размещения: аудитория не перегружена')}
      <div style="padding:16px"><button class="btn btn-d" style="width:100%" onclick="adChannelDelete('${escJs(c.username)}')">Убрать с разведки</button></div>
    `);
  } catch (e) {
    txt('adChanBody', errHtml(errRu(e), `openAdChannel('${escJs(username)}')`));
  }
}

async function adChannelRescan(username) {
  try {
    const r = await api('/api/miniapp/ad_intel/channel/' + encodeURIComponent(username) + '/rescan', {method:'POST', body:'{}'});
    toast('🔄 Обновление запущено');
    if (r.op_id) pollOpResult(r.op_id, 'Разведка рекламы');
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function adChannelAuto(username, on) {
  try {
    await api('/api/miniapp/ad_intel/channel/' + encodeURIComponent(username) + '/auto', {method:'POST', body:JSON.stringify({on: !!on})});
    toast(on ? '✅ Канал будет обновляться раз в сутки' : 'Автообновление выключено');
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

async function adChannelDelete(username) {
  if (!await askConfirm(`Убрать @${username} с разведки? Его рекламные посты удалятся, счётчики рекламодателей пересчитаются.`)) return;
  try {
    await api('/api/miniapp/ad_intel/channel/' + encodeURIComponent(username), {method:'DELETE'});
    toast('Канал убран с разведки');
    back();
    openAdIntel();
  } catch (e) { toast('⚠️ ' + errRu(e)); }
}

// ── Карточка рекламодателя ───────────────────────────────────────────────

async function openAdAdvertiser(username) {
  push('s-adadv');
  txt('adAdvTitle', '🏢 ' + esc(username));
  txt('adAdvBody', '<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const a = await api('/api/miniapp/ad_intel/advertiser/' + encodeURIComponent(username));
    const nm = a.kind === 'site' || a.kind === 'invite' ? a.username : '@' + a.username;
    txt('adAdvTitle', '🏢 ' + esc(nm));
    const kpi = (v, l) => `<div class="kpi-card"><div class="kpi-val">${v}</div><div class="kpi-lbl">${l}</div></div>`;
    const chans = (a.channels_list || []).map(c => `
      <div class="row tap" role="button" tabindex="0" onclick="openAdChannel('${escJs(c.username)}')">
        <div class="row-ico" style="background:var(--bg-yellow-16)">📺</div>
        <div class="row-body"><div class="row-name">${esc(c.title || '@' + c.username)}</div>
          <div class="row-val">${num(c.posts)} ${plural(c.posts,'пост','поста','постов')} · ${num(c.views)} просмотров · ${num(c.subscribers)} подписчиков${c.ad_price_rub ? ' · пост ' + adiRub(c.ad_price_rub) : ''}</div></div>
        <div class="row-right" style="display:flex;align-items:center;gap:5px"><span style="color:var(--hint);font-size:13px">${adiAgo(c.last_seen)}</span><span class="chev">›</span></div>
      </div>`).join('');
    const openHref = a.kind === 'site' ? `adiOpenSite('${escJs(a.username)}')`
                   : `openTgUser('${escJs(a.username)}')`;
    const track = a.can_track
      ? (a.tracked
          ? `<button class="btn btn-s" style="flex:1 1 auto" onclick="openAdChannel('${escJs(a.username)}')">📡 Уже на разведке</button>`
          : `<button class="btn btn-p" style="flex:1 1 auto" onclick="openAdIntelModal('@${escJs(a.username)}')">📡 Поставить на разведку</button>`)
      : '';
    txt('adAdvBody', `
      <div class="kpi-row" style="padding:8px 16px 10px">
        ${kpi(num(a.placements||0), 'Размещений')}
        ${kpi(num(a.channels||0), 'Каналов')}
        ${kpi(num(a.views||0), 'Просмотров')}
      </div>
      <div class="field-note" style="padding:0 16px 8px">${esc(a.kind_label || 'Рекламодатель')}${a.niche ? ' · ниша: ' + esc(a.niche) : ''} · впервые замечен ${adiAgo(a.first_seen)}, последний раз ${adiAgo(a.last_seen)}</div>
      <div style="display:flex;gap:8px;padding:0 16px 8px">
        ${track}
        <button class="btn btn-s" style="flex:1 1 auto" onclick="${openHref}">Открыть</button>
      </div>
      <div class="sec">Где размещался</div>
      <div class="lst">${chans || empty('📺', 'Каналов нет', '')}</div>
      <div class="sec" style="margin-top:8px">Его рекламные посты</div>
      ${(a.ads || []).map(x => adIntelAdRow(x, true)).join('') || empty('📰', 'Постов нет', '')}
    `);
  } catch (e) {
    txt('adAdvBody', errHtml(errRu(e), `openAdAdvertiser('${escJs(username)}')`));
  }
}
