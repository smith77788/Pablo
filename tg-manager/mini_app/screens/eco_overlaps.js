// Пересечения аудитории внутри экосистемы: сколько одних и тех же людей сидит
// сразу в двух её каналах. Движок умел это считать (ecosystem_brain
// .analyze_audience_overlap), эндпоинт существовал, а экрана не было — считать
// было некому и некуда показывать.
//
// Зачем владельцу: если два канала пересекаются на 60%, рассылка по обоим — это
// один и тот же человек дважды. Либо объединять, либо развести темы.

let _eoEcoId = null;

function _eoScreen(id, title) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()" role="button" tabindex="0" aria-label="Назад">←</div>' +
      '<div class="hdr-title">' + esc(title) + '</div>' +
      '<div class="icon-btn" role="button" tabindex="0" aria-label="Обновить" ' +
        'style="margin-left:auto" onclick="openEcoOverlaps(_eoEcoId)">🔄</div>' +
    '</div>' +
    '<div class="sb"><div id="' + id + '-body" style="padding:6px 0">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

// Насколько пересечение существенно: подсказка, что с ним делать.
function _eoTone(pct) {
  if (pct >= 50) return {c: 'var(--red)', t: 'почти одна аудитория — есть смысл объединить'};
  if (pct >= 20) return {c: 'var(--orange,#f59e0b)', t: 'заметное пересечение — рассылку дублировать не стоит'};
  if (pct > 0) return {c: 'var(--green)', t: 'аудитории почти не совпадают'};
  return {c: 'var(--hint)', t: 'общих подписчиков нет'};
}

async function openEcoOverlaps(ecoId) {
  _eoEcoId = ecoId;
  _eoScreen('s-ecooverlaps', '🔀 Пересечения аудитории');
  push('s-ecooverlaps');
  const body = document.getElementById('s-ecooverlaps-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/ecosystem/' + ecoId + '/overlaps');
    const pairs = d.pairs || [];
    const chs = d.channels || 0;
    if (!pairs.length) {
      // Честно разделяем «каналов мало» и «людей ещё не собрали»: это разные
      // проблемы, и делать нужно разное.
      body.innerHTML = chs < 2
        ? empty('🔀', 'Сравнивать пока нечего',
                'В экосистеме ' + (chs ? 'всего один канал' : 'нет каналов') +
                '. Добавьте ещё хотя бы один — тогда будет видно, насколько их аудитории совпадают.',
                {label: '🔎 Авто-наполнить состав', fn: 'ecoAutoDiscover()'})
        : empty('👥', 'Подписчики ещё не собраны',
                'Каналов в экосистеме ' + num(chs) + ', но их участники не выгружены. ' +
                'Соберите аудиторию — после этого пересечения посчитаются.',
                {label: '🔍 Парсер аудитории', fn: 'openParser()'});
      return;
    }
    const tot = d.total_subscribers || 0;
    const uniq = d.unique_subscribers || 0;
    const dup = tot > uniq ? tot - uniq : 0;
    let h = '<div class="kpi" style="padding:0 16px">' +
      '<div class="kpi-card"><div class="kpi-val">' + num(chs) + '</div><div class="kpi-lbl">Каналов</div></div>' +
      '<div class="kpi-card"><div class="kpi-val">' + num(uniq) + '</div><div class="kpi-lbl">Людей всего</div></div>' +
      '<div class="kpi-card"><div class="kpi-val" style="' + kpiTone(dup, 'orange') + '">' + num(dup) +
        '</div><div class="kpi-lbl">Дублей в рассылке</div></div></div>';
    if (dup) {
      h += '<div style="margin:8px 16px 0;background:var(--bg2);border-left:3px solid var(--orange,#f59e0b);' +
        'border-radius:8px;padding:10px 12px;font-size:12px;line-height:1.45">' +
        'Лишних подписок ' + num(dup) + ': столько раз люди повторяются в разных каналах экосистемы. ' +
        'Рассылка по всем каналам придёт им по нескольку раз — это главная причина жалоб и отписок.</div>';
    }
    h += '<div class="sec">Пары каналов</div><div class="lst" style="margin:0 16px">';
    h += pairs.map(function (p) {
      const pct = p.overlap_pct || 0;
      const tone = _eoTone(pct);
      const w = Math.max(2, Math.min(100, Math.round(pct)));
      return '<div class="row"><div class="row-body">' +
        '<div class="row-name">' + esc(p.name_a) + ' ↔ ' + esc(p.name_b) + '</div>' +
        '<div class="row-val" style="color:' + tone.c + '">' + pct.toFixed(1) + '% · общих ' + num(p.shared || 0) + '</div>' +
        '<div style="height:5px;background:var(--sep);border-radius:3px;overflow:hidden;margin:5px 0 3px">' +
          '<div style="height:100%;width:' + w + '%;background:' + tone.c + ';border-radius:3px"></div></div>' +
        '<div style="font-size:11px;color:var(--hint);line-height:1.35">' + tone.t + '</div>' +
        '<div style="display:flex;gap:6px;margin-top:7px">' +
          '<button class="btn btn-s" style="font-size:11px;padding:5px 9px" onclick="openChannel(' +
            Number(p.channel_a) + ')">' + esc(p.name_a).substring(0, 18) + ' →</button>' +
          '<button class="btn btn-s" style="font-size:11px;padding:5px 9px" onclick="openChannel(' +
            Number(p.channel_b) + ')">' + esc(p.name_b).substring(0, 18) + ' →</button>' +
        '</div></div></div>';
    }).join('');
    h += '</div>';
    h += '<div style="display:flex;gap:8px;padding:12px 16px 20px">' +
      '<button class="btn btn-s" style="flex:1;font-size:12px;padding:10px 6px" onclick="openMassPub()">📤 Публикация без дублей</button>' +
      '<button class="btn btn-s" style="flex:1;font-size:12px;padding:10px 6px" onclick="goTab(\'channels\')">📡 Мои каналы</button>' +
      '</div>';
    body.innerHTML = h;
  } catch (e) {
    body.innerHTML = errHtml(errRu(e), 'openEcoOverlaps(' + ecoId + ')');
  }
}
