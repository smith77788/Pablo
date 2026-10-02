// Виртуальный администратор каналов: свой администратор на каждом канале.
// Владелец выбирает канал и ставит администратора — дальше тот сам собирает
// профиль ниши, строит контент-план, пишет и публикует посты, смотрит
// статистику и подстраивает рубрики. Логика — services/channel_admin.py.

let _vaCid = null;

function _vaMkScreen(id, title, bodyId) {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = id;
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()">←</div>' +
      '<div class="hdr-title">' + title + '</div>' +
    '</div>' +
    '<div class="sub-sb"><div id="' + bodyId + '">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

function _vaWhen(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  } catch (e) { return '—'; }
}

function _vaState(c) {
  if (!c.installed) return '<span style="color:var(--hint)">не установлен</span>';
  if (!c.enabled) return '<span style="color:var(--hint)">остановлен</span>';
  if (!c.setup_done) return '<span style="color:var(--orange,#fb923c)">изучает канал…</span>';
  if (c.last_error) return '<span style="color:var(--red,#ef4444)">есть проблема</span>';
  return '<span style="color:var(--green)">ведёт канал</span>';
}

async function openVaAdmin() {
  _vaMkScreen('s-va', '🧠 Виртуальный администратор', 's-va-body');
  push('s-va');
  await _vaLoadList();
}

async function _vaLoadList() {
  const body = document.getElementById('s-va-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  let d;
  try {
    d = await api('/api/miniapp/va/channels');
  } catch (e) {
    body.innerHTML = errHtml('Не удалось загрузить каналы: ' + ((e && e.message) || ''), '_vaLoadList()');
    return;
  }
  const chans = d.channels || [];
  let h = '<div class="lst" style="padding:12px 14px;font-size:13px;line-height:1.5;color:var(--hint)">' +
    'Выберите канал и поставьте администратора. Он сам разберётся в нише канала, составит контент-план, ' +
    'будет писать и публиковать посты под рост аудитории и заявки, следить за статистикой и присылать ' +
    'вам отчёт раз в сутки. Участвовать не нужно.</div>';
  // Если ИИ не подключён — администратор не сможет писать посты. Это причина
  // №1 «тишины», и она общая для всех каналов: показываем заметным баннером.
  if (d.network && d.network.ai_ready === false && d.network.ai_note) {
    h += '<div class="lst" style="padding:12px 14px;margin-top:8px;border-left:3px solid var(--red,#ef4444);' +
      'background:var(--bg2);font-size:13px;line-height:1.5">⚠️ ' + esc(d.network.ai_note) + '</div>';
  }
  h += _vaNetworkHtml(d.network);
  h += _vaDraftsHtml(d.drafts || [], 'list');
  h += '<div class="sec">Каналы (' + chans.length + ')</div>';
  if (!chans.length) {
    h += '<div class="lst" style="padding:14px;color:var(--hint)">Каналов пока нет. Добавьте аккаунт, который ' +
      'администрирует канал, — канал появится здесь.</div>';
  } else {
    h += '<div class="lst">' + chans.map(function (c) {
      const sub = c.installed && c.enabled && c.setup_done
        ? ('следующий пост: ' + _vaWhen(c.next_post_at) + (c.pending_drafts ? ' · ждёт решения: ' + c.pending_drafts : ''))
        : (c.topic ? esc(c.topic).slice(0, 80) : (c.username ? '@' + esc(c.username) : ''));
      return '<div class="li tap" onclick="openVaChannel(\'' + esc(c.channel_id) + '\')">' +
        '<div class="ava ava-purple">🧠</div><div class="li-body">' +
        '<div class="li-name">' + esc(c.title || c.username || c.channel_id) + '</div>' +
        '<div class="li-sub">' + _vaState(c) + (sub ? ' · ' + sub : '') + '</div></div>' +
        '<span class="chev">›</span></div>';
    }).join('') + '</div>';
  }
  h += '<div class="sec">Общие правила редактора</div>' +
    '<div class="lst"><div class="li tap" onclick="openEditorialRules()"><div class="ava ava-purple">✍️</div>' +
    '<div class="li-body"><div class="li-name">Правила для всех каналов</div>' +
    '<div class="li-sub">Запрещённые слова, лимиты, режим проверки ручных публикаций</div></div>' +
    '<span class="chev">›</span></div></div>';
  body.innerHTML = h;
}

// Сводка по всей сети каналов — взгляд руководителя поверх администраторов
// отдельных каналов. Показывается, только когда хоть один администратор
// установлен: пустому владельцу цифры «0/0» не нужны.
function _vaNum(v) {
  const x = Number(v);
  return isFinite(x) ? Math.round(x).toLocaleString('ru') : '—';
}

function _vaNetworkHtml(n) {
  if (!n || !n.admins_installed) return '';
  // Числа целиком и по-русски: общий форматтер пишет «1.2K», а латиница в
  // интерфейсе владельцу непонятна.
  // Полоса счётчиков — общий компонент приложения (.kpi-row/.kpi-card), а не
  // свой ряд inline-стилями: раньше четыре карточки по 80px при 360px делились
  // пополам и подписи вроде «постов за 7 дней» ломались на три строки разной
  // высоты. Сетка сама берёт столько колонок, сколько помещается.
  const kpi = function (val, lbl) {
    return '<div class="kpi-card"><div class="kpi-val">' + val + '</div>' +
      '<div class="kpi-lbl">' + lbl + '</div></div>';
  };
  let h = '<div class="sec">Сеть каналов</div><div class="lst" style="padding:2px 0 8px">' +
    '<div class="kpi-row">' +
      kpi(_vaNum(n.admins_active) + '<span style="font-size:13px;color:var(--hint)">/' + _vaNum(n.admins_installed) + '</span>', 'ведут каналы') +
      kpi(_vaNum(n.posts_7d), 'постов за неделю') +
      kpi(n.avg_views_7d ? _vaNum(n.avg_views_7d) : '—', 'средний охват') +
      kpi(n.members_total ? _vaNum(n.members_total) : '—', 'подписчиков') +
    '</div>';
  if (n.pending_drafts) {
    h += '<div style="padding:6px 14px;font-size:13px">📝 Ждут вашего решения: <b>' + _vaNum(n.pending_drafts) + '</b></div>';
  }
  h += '</div>';

  if (n.top_pillars && n.top_pillars.length) {
    h += '<div class="sec">Что заходит по сети</div><div class="lst" style="padding:10px 14px">' +
      n.top_pillars.map(function (p) {
        return '<div style="display:flex;justify-content:space-between;font-size:13px;padding:4px 0;border-bottom:1px solid var(--sep)">' +
          '<span>' + esc(p.pillar) + '</span>' +
          '<span style="color:var(--hint)">' + _vaNum(p.avg_views) + ' просм. · ' + p.posts + ' ' + plural(p.posts, 'пост', 'поста', 'постов') + '</span></div>';
      }).join('') + '</div>';
  }

  if (n.attention && n.attention.length) {
    h += '<div class="sec">Требуют внимания (' + n.attention.length + ')</div><div class="lst">' +
      n.attention.map(function (a) {
        return '<div class="li tap" onclick="openVaChannel(\'' + esc(a.channel_id) + '\')">' +
          '<div class="ava" style="background:var(--red,#ef4444)">⚠️</div><div class="li-body">' +
          '<div class="li-name">' + esc(a.title) + '</div>' +
          '<div class="li-sub">' + esc(a.error || ('сбоев подряд: ' + a.fail_streak)) + '</div></div>' +
          '<span class="chev">›</span></div>';
      }).join('') + '</div>';
  }
  return h;
}

let _vaDraftCtx = 'list';

function _vaDraftsHtml(drafts, ctx) {
  _vaDraftCtx = ctx;
  if (!drafts.length) return '';
  return '<div class="sec">Ждут вашего решения (' + drafts.length + ')</div>' + drafts.map(function (x) {
    const reasons = (x.reasons || []).length
      ? '<div style="margin-top:8px;color:var(--orange,#fb923c)">✍️ ' + x.reasons.map(esc).join('<br>✍️ ') + '</div>' : '';
    return '<div class="lst" style="padding:12px 14px">' +
      '<div style="font-size:12px;color:var(--hint);margin-bottom:6px">' + esc(x.title || '') +
        (x.pillar ? ' · ' + esc(x.pillar) : '') + '</div>' +
      '<div style="white-space:pre-wrap;font-size:14px;line-height:1.45">' + esc(x.body) + '</div>' + reasons +
      // Три кнопки в один ряд при 360px не помещались: «Опубликовать» ломалось
      // на две строки и вылезало за кнопку, а безымянный «✖️» занимал треть
      // ряда, не говоря, что он делает. Главное действие во всю ширину,
      // второстепенные — рядом и с подписями.
      '<div style="margin-top:10px">' +
        '<button class="btn btn-p" style="width:100%" onclick="vaDraftAct(' + x.id + ',\'publish\')">✅ Опубликовать</button>' +
        '<div style="display:flex;gap:8px;margin-top:8px">' +
          '<button class="btn btn-s" style="flex:1 1 0;min-width:0" onclick="vaDraftAct(' + x.id + ',\'regenerate\')">🔄 Другой</button>' +
          '<button class="btn btn-s" style="flex:1 1 0;min-width:0" onclick="vaDraftWhy(' + x.id + ')">✖️ Пропустить</button>' +
        '</div>' +
      '</div><div id="vaWhy' + x.id + '"></div></div>';
  }).join('');
}

// Причины отказа — те же коды, что channel_admin.REJECT_REASONS.
const _VA_WHY = [['offtopic', 'Не по теме'], ['ads', 'Слишком рекламно'], ['invented', 'Выдуманные факты'],
  ['tone', 'Не тот тон'], ['boring', 'Скучно'], ['long', 'Слишком длинно'], ['', 'Просто пропустить']];

function vaDraftWhy(id) {
  const box = document.getElementById('vaWhy' + id);
  if (!box) return vaDraftAct(id, 'reject');
  box.innerHTML = '<div style="font-size:12px;color:var(--hint);margin:10px 0 6px">Почему пропускаете? Администратор учтёт это в следующих постах.</div>' +
    '<div style="display:flex;flex-wrap:wrap;gap:6px">' + _VA_WHY.map(function (w) {
      return '<button class="btn btn-s" style="padding:6px 10px;font-size:12px" onclick="vaDraftAct(' + id + ',\'reject\',\'' + w[0] + '\')">' + w[1] + '</button>';
    }).join('') + '</div>';
}

async function vaDraftAct(id, action, reason) {
  const msgs = { publish: 'Публикую…', regenerate: 'Пишу другой вариант…', reject: 'Пропускаю…' };
  toast(msgs[action] || '…');
  try {
    await api('/api/miniapp/va/drafts/' + id + '/' + action, { method: 'POST', body: JSON.stringify({ reason: reason || '' }), timeoutMs: 180000 });
    toast(action === 'publish' ? '✅ Отправлено в канал' : (action === 'regenerate' ? '✅ Новый вариант готов' : 'Пропущено'));
  } catch (e) {
    toast('⚠️ ' + ((e && e.message) || 'Не получилось'));
  }
  if (_vaDraftCtx === 'ch') await _vaLoadChannel(); else await _vaLoadList();
}

async function openVaChannel(cid) {
  _vaCid = String(cid);
  _vaMkScreen('s-va-ch', '🧠 Администратор канала', 's-va-ch-body');
  push('s-va-ch');
  await _vaLoadChannel();
}

async function _vaLoadChannel() {
  const body = document.getElementById('s-va-ch-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/va/channel/' + encodeURIComponent(_vaCid));
    body.innerHTML = _vaChannelHtml(d);
  } catch (e) {
    body.innerHTML = errHtml('Не удалось загрузить канал: ' + ((e && e.message) || ''), '_vaLoadChannel()');
  }
}

function _vaOpt(val, cur, label) {
  return '<option value="' + val + '"' + (String(val) === String(cur) ? ' selected' : '') + '>' + label + '</option>';
}

function _vaHours(cur) {
  let o = '';
  for (let i = 0; i <= 24; i++) o += _vaOpt(i, cur, (i < 10 ? '0' : '') + i + ':00');
  return o;
}

function _vaTz(cur) {
  let o = '';
  // Без скобок: «UTC+3 (Москва)» не помещалось в селект шириной в половину
  // экрана и обрезалось на закрывающей скобке.
  for (let i = -12; i <= 14; i++) o += _vaOpt(i, cur, 'UTC' + (i >= 0 ? '+' : '') + i + (i === 3 ? ' Москва' : ''));
  return o;
}

function _vaInstallHtml(ch, s) {
  return '<div class="lst" style="padding:14px;font-size:13px;line-height:1.5">' +
      '<b>' + esc(ch.title || ch.username || '') + '</b><br><br>' +
      'Администратор сам изучит канал: название, описание, подписчиков и уже вышедшие посты. ' +
      'Если канал пустой — определит нишу по названию и начнёт с поста-знакомства. ' +
      'Чем больше вы расскажете о бизнесе ниже, тем точнее он попадёт в клиентов, но можно и ничего не писать.' +
    '</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div class="field"><label>О проекте своими словами (необязательно)</label>' +
        '<textarea id="vaProject" rows="4" maxlength="2000" placeholder="Например: доставка мебели по Москве за 1 день, работаем с 2015 года, свои грузчики, сборка бесплатно"></textarea></div>' +
      '<div class="field"><label>Куда вести клиентов (необязательно)</label>' +
        '<input id="vaContact" maxlength="200" placeholder="@manager, ссылка на сайт или номер">' +
        '<div class="field-note">Этот контакт администратор будет указывать в продающих постах.</div></div>' +
      '<div class="field"><label>Постов в день</label><select id="vaPpd">' +
        [1, 2, 3, 4, 5, 6].map(function (n) { return _vaOpt(n, s.posts_per_day || 2, n); }).join('') + '</select></div>' +
      '<div class="field-err" id="vaErr"></div>' +
      '<button class="btn btn-p" id="vaInstallBtn" style="width:100%" onclick="vaInstall()">🚀 Поставить администратора</button>' +
    '</div>';
}

function _vaReportHtml(r, s) {
  if (!r) return '';
  const delta = (r.members_delta_7d === null || r.members_delta_7d === undefined) ? '' :
    ' <span style="color:' + (r.members_delta_7d >= 0 ? 'var(--green)' : 'var(--red,#ef4444)') + '">(' +
    (r.members_delta_7d >= 0 ? '+' : '') + r.members_delta_7d + ' за неделю)</span>';
  let h = '<div class="sec">Статистика</div><div class="lst" style="padding:12px 14px;font-size:13px;line-height:1.7">' +
    'Подписчиков: <b>' + (r.members !== null && r.members !== undefined ? _vaNum(r.members) : (s.members_count ? _vaNum(s.members_count) : '—')) + '</b>' + delta + '<br>' +
    'Постов за неделю: <b>' + _vaNum(r.posts_7d) + '</b>' + (r.avg_views_7d ? ', в среднем <b>' + _vaNum(r.avg_views_7d) + '</b> ' + plural(r.avg_views_7d, 'просмотр', 'просмотра', 'просмотров') : '') + '<br>' +
    'Всего постов в памяти: ' + _vaNum(r.posts_total);
  if ((r.pillars || []).length) {
    h += '<br><br>Что заходит аудитории:<br>' + r.pillars.map(function (p) {
      return '• ' + esc(p.name) + ' — <span style="white-space:nowrap">' + _vaNum(p.avg_views) + ' просм. в среднем, ' +
        p.posts + ' ' + plural(p.posts, 'пост', 'поста', 'постов') + '</span>';
    }).join('<br>');
  } else {
    h += '<br><span style="color:var(--hint)">Отклик по рубрикам появится, когда наберутся просмотры первых постов.</span>';
  }
  return h + '</div>';
}

function _vaBriefHtml(b) {
  const rows = [];
  if (b.niche) rows.push('<b>Ниша:</b> ' + esc(b.niche));
  if (b.offer) rows.push('<b>Предложение:</b> ' + esc(b.offer));
  if (b.usp) rows.push('<b>Чем сильнее конкурентов:</b> ' + esc(b.usp));
  if (b.geo) rows.push('<b>География:</b> ' + esc(b.geo));
  if ((b.pains || []).length) rows.push('<b>Боли клиентов:</b> ' + b.pains.map(esc).join('; '));
  if ((b.objections || []).length) rows.push('<b>Возражения:</b> ' + b.objections.map(esc).join('; '));
  if ((b.triggers || []).length) rows.push('<b>Что приводит к заявке:</b> ' + b.triggers.map(esc).join('; '));
  if (!rows.length) return '';
  return '<div class="sec">Как администратор понял нишу</div><div class="lst" style="padding:12px 14px;font-size:13px;line-height:1.6">' +
    rows.join('<br>') + '</div>';
}

// Бизнес-настройки: слова владельца, которые ИИ ставит выше своих догадок о нише.
const _VA_GOALS = [
  ['', 'Пусть решит администратор'], ['sales', 'Продажи'], ['leads', 'Заявки и обращения'],
  ['brand', 'Доверие и узнаваемость'], ['audience', 'Рост подписчиков'],
  ['community', 'Живое обсуждение'], ['expert', 'Экспертность автора'],
];

function _vaArea(id, label, val, max, ph, rows) {
  return '<div class="field"><label>' + label + '</label><textarea id="' + id + '" rows="' + (rows || 2) +
    '" maxlength="' + max + '" placeholder="' + esc(ph) + '">' + esc(val || '') + '</textarea></div>';
}

function _vaBusinessHtml(b) {
  const share = b.sales_share == null ? '' : String(b.sales_share);
  // Поля о бизнесе держим свёрнутыми. Открытыми они растягивали экран настроек
  // на несколько пролистываний, и кнопка «Сохранить» уезжала так далеко, что до
  // неё никто не добирался. Заполняют их один раз, а заходят на экран, чтобы
  // посмотреть, как идут дела. В свёрнутом виде справа видно, сколько заполнено.
  const bizDone = _VA_BIZ_KEYS.filter(function (k) { return b[k[0]]; }).length;
  return '<details class="acc-actions" style="margin:0 0 12px;background:var(--bg3);border-radius:12px">' +
    '<summary style="cursor:pointer;font-size:14px;font-weight:500;padding:12px 12px;display:flex;align-items:center;gap:8px">' +
      '<span style="flex:1;min-width:0">💼 Бизнес</span>' +
      '<span style="font-size:12px;color:var(--hint);font-weight:400;white-space:nowrap">' +
        (bizDone ? bizDone + ' из ' + _VA_BIZ_KEYS.length : 'не заполнено') + '</span>' +
      '<span class="acc-chev" style="color:var(--hint)">▾</span>' +
    '</summary>' +
    '<div style="padding:0 12px 2px">' +
    _vaBizFill(b) +
    '<div class="field"><label>Цель канала</label><select id="vaGoal">' +
      _VA_GOALS.map(function (g) { return _vaOpt(g[0], b.goal || '', g[1]); }).join('') + '</select></div>' +
    _vaArea('vaProducts', 'Товары и услуги с ценами', b.products, 1500, 'Например: стрижка — 1500 ₽, окрашивание — от 4000 ₽', 3) +
    _vaArea('vaPromo', 'Действующая акция', b.promo, 300, 'Например: −20% на первый визит') +
    '<div class="field"><label>Акция действует до</label><input type="date" id="vaPromoUntil" value="' + esc(b.promo_until || '') + '">' +
      '<div class="field-note">' + (_vaPromoOver(b) ? '⚠️ Срок прошёл — администратор больше не упоминает эту акцию.' :
        'Пусто — бессрочно. После этой даты акция пропадёт из постов сама.') + '</div></div>' +
    _vaArea('vaUsp', 'Чем вы лучше конкурентов', b.usp, 300, 'Например: выезд в день обращения, гарантия год') +
    _vaArea('vaPains', 'Боли и частые вопросы клиентов', b.pains, 600, 'С чем к вам приходят и о чём спрашивают') +
    _vaArea('vaFacts', 'Факты и цифры, которые можно приводить', b.facts, 800, 'Опыт, число клиентов, сроки. Других цифр ИИ не выдумает', 3) +
    _vaArea('vaBanned', 'Запретные темы', b.banned_topics, 600, 'Например: политика, здоровье, сравнение цен') +
    _vaArea('vaRivals', 'Конкуренты — не упоминать', b.competitors, 300, 'Названия через запятую. Пост с ними уйдёт вам на проверку') +
    '<div style="display:flex;gap:10px">' +
      '<div class="field" style="flex:1"><label>Обращение</label><select id="vaAddr">' +
        _vaOpt('', b.address || '', 'Как пойдёт') + _vaOpt('vy', b.address || '', 'На «вы»') +
        _vaOpt('ty', b.address || '', 'На «ты»') + '</select></div>' +
      '<div class="field" style="flex:1"><label>Продающих постов</label><select id="vaShare">' +
        _vaOpt('', share, 'По рубрикам') +
        [0, 10, 20, 30, 40, 50, 60].map(function (n) { return _vaOpt(String(n), share, 'до ' + n + ' %'); }).join('') +
      '</select></div>' +
    '</div>' +
    '<div class="field-note" style="margin:0 0 10px">Доля считается по последним 10 постам: лишний продающий пост администратор заменит полезным.</div>' +
    '</div></details>';
}

function _vaPromoOver(b) {
  if (!b.promo || !b.promo_until) return false;
  const d = new Date();
  const today = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  return b.promo_until < today;
}

// Что ещё не рассказано о бизнесе — с самого важного для точности постов.
const _VA_BIZ_KEYS = [
  ['goal', 'цель канала'], ['products', 'товары и цены'], ['facts', 'факты и цифры'],
  ['pains', 'боли клиентов'], ['usp', 'чем вы лучше'], ['address', 'обращение'],
  ['banned_topics', 'запретные темы'],
];

function _vaBizFill(b) {
  const miss = _VA_BIZ_KEYS.filter(function (k) { return !b[k[0]]; });
  const done = _VA_BIZ_KEYS.length - miss.length;
  if (!miss.length) return '<div class="field-note" style="margin:0 0 12px">✅ Всё главное о бизнесе заполнено.</div>';
  return '<div class="field-note" style="margin:0 0 12px">Заполнено ' + done + ' из ' + _VA_BIZ_KEYS.length +
    '. Посты станут точнее, если указать: ' + miss.slice(0, 3).map(function (k) { return k[1]; }).join(', ') + '.</div>';
}

function _vaBusinessVal() {
  const share = _vaVal('vaShare');
  return {
    goal: _vaVal('vaGoal') || '', products: _vaVal('vaProducts') || '', promo: _vaVal('vaPromo') || '', promo_until: _vaVal('vaPromoUntil') || '',
    usp: _vaVal('vaUsp') || '', pains: _vaVal('vaPains') || '', facts: _vaVal('vaFacts') || '',
    banned_topics: _vaVal('vaBanned') || '', competitors: _vaVal('vaRivals') || '',
    address: _vaVal('vaAddr') || '', sales_share: share === '' || share == null ? null : Number(share),
  };
}

// Каналы-образцы: на кого равняться. Показываем, что администратор понял из каждого.
const _VA_REF_KINDS = [['competitor', 'Конкурент'], ['own', 'Мой успешный канал'], ['example', 'Просто нравится']];

function _vaRefHtml(r) {
  const st = r.stats || {}, le = r.lessons || {};
  let body;
  if (r.status === 'pending') body = '<div style="color:var(--hint)">⏳ Ещё не изучен — займусь в ближайшие минуты.</div>';
  else if (r.status === 'error') body = '<div style="color:var(--orange,#fb923c)">⚠️ ' + esc(r.error) + '</div>';
  else {
    const facts = [];
    if (st.members) facts.push(_vaNum(st.members) + ' подписчиков');
    if (st.per_day) facts.push(String(st.per_day).replace('.', ',') + ' поста в день');
    if (st.avg_len) facts.push('пост ≈ ' + _vaNum(st.avg_len) + ' знаков');
    if (st.reach_pct) facts.push('охват ' + String(st.reach_pct).replace('.', ',') + ' %');
    if ((st.top_hours || []).length) facts.push('чаще пишет в ' + st.top_hours.map(function (x) { return x + ':00'; }).join(', '));
    const rows = [];
    if (facts.length) rows.push(facts.map(esc).join(' · '));
    if (le.summary) rows.push('<b>Чем берёт:</b> ' + esc(le.summary));
    if (le.style) rows.push('<b>Подача:</b> ' + esc(le.style));
    if ((le.works || []).length) rows.push('<b>Что заходит:</b> ' + le.works.map(esc).join('; '));
    if ((le.formats || []).length) rows.push('<b>Приёмы:</b> ' + le.formats.map(esc).join('; '));
    if ((le.avoid || []).length) rows.push('<b>Заходит хуже:</b> ' + le.avoid.map(esc).join('; '));
    if (st.length_hint) rows.push(esc(st.length_hint[0].toUpperCase() + st.length_hint.slice(1)));
    if (!le.summary && !le.style) rows.push('<span style="color:var(--hint)">Разбор подачи не получен — пока учитываю только цифры.</span>');
    body = rows.join('<br>');
  }
  return '<div class="lst" style="padding:12px 14px;font-size:13px;line-height:1.55">' +
    '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:6px">' +
      '<b>@' + esc(r.username) + '</b><span style="font-size:12px;color:var(--hint)">' + esc(r.kind_label) + '</span></div>' +
    body +
    '<div style="display:flex;gap:8px;margin-top:10px">' +
      '<button class="btn btn-s" style="flex:1;padding:6px" onclick="vaRefAct(' + r.id + ',\'refresh\')">🔄 Изучить заново</button>' +
      '<button class="btn btn-s" style="padding:6px 12px" onclick="vaRefAct(' + r.id + ',\'delete\')" aria-label="Убрать образец">🗑</button>' +
    '</div></div>';
}

function _vaRefsHtml(refs) {
  return '<div class="sec">Каналы-образцы</div>' +
    '<div class="field-note" style="margin:0 0 8px">Конкуренты или ваш успешный канал. Администратор изучит, как они пишут и что у них набирает просмотры, и будет писать так же сильно — без копирования текстов.</div>' +
    refs.map(_vaRefHtml).join('') +
    (refs.length < 5 ? '<div class="lst" style="padding:12px 14px">' +
      '<div class="field"><label>Публичный канал</label><input id="vaRefName" maxlength="80" placeholder="@channel или t.me/channel"></div>' +
      '<div class="field"><label>Это</label><select id="vaRefKind">' +
        _VA_REF_KINDS.map(function (k) { return _vaOpt(k[0], 'competitor', k[1]); }).join('') + '</select></div>' +
      '<div class="field-err" id="vaRefErr"></div>' +
      '<button class="btn btn-p" id="vaRefBtn" style="width:100%" onclick="vaRefAdd()">➕ Добавить и изучить</button>' +
    '</div>' : '');
}

async function vaRefAdd() {
  const btn = document.getElementById('vaRefBtn'), err = document.getElementById('vaRefErr');
  err.textContent = '';
  btn.disabled = true; btn.textContent = '⏳ Читаю канал и разбираю…';
  try {
    const d = await api('/api/miniapp/va/channel/' + encodeURIComponent(_vaCid) + '/references', {
      method: 'POST', timeoutMs: 180000,
      body: JSON.stringify({ ref: _vaVal('vaRefName') || '', kind: _vaVal('vaRefKind') || 'competitor' }),
    });
    toast('✅ Образец добавлен');
    document.getElementById('s-va-ch-body').innerHTML = _vaChannelHtml(d);
  } catch (e) {
    err.textContent = (e && e.message) || 'Не получилось';
    btn.disabled = false; btn.textContent = '➕ Добавить и изучить';
  }
}

async function vaRefAct(id, action) {
  if (action === 'delete' && !(await askConfirm('Убрать этот канал из образцов?'))) return;
  toast(action === 'delete' ? 'Убираю…' : 'Изучаю заново…');
  try {
    const d = await api('/api/miniapp/va/references/' + id + (action === 'delete' ? '' : '/refresh'), {
      method: action === 'delete' ? 'DELETE' : 'POST', body: '{}', timeoutMs: 180000,
    });
    toast('✅ Готово');
    document.getElementById('s-va-ch-body').innerHTML = _vaChannelHtml(d);
  } catch (e) {
    toast('⚠️ ' + ((e && e.message) || 'Не получилось'));
  }
}

function _vaChannelHtml(d) {
  const ch = d.channel || {}, s = d.settings || {};
  if (!s.installed) return _vaInstallHtml(ch, s);
  const state = !s.enabled ? '⏸ Остановлен' : (!s.setup_done ? '🔎 Изучает канал и строит план…' :
    (s.last_error ? '⚠️ ' + esc(s.last_error) : '✅ Ведёт канал сам'));
  let h = '<div class="lst" style="padding:14px;font-size:13px;line-height:1.6">' +
    '<b>' + esc(ch.title || ch.username || '') + '</b><br>' + state +
    (s.enabled && s.setup_done ? '<br>Следующий пост: <b>' + _vaWhen(s.next_post_at) + '</b>' : '') +
    (s.last_post_at ? '<br>Последний пост: ' + _vaWhen(s.last_post_at) : '') +
    '<div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">' +
      '<button class="btn btn-p" id="vaNowBtn" style="flex:1 1 45%" onclick="vaPostNow()">✍️ Пост сейчас</button>' +
      (s.enabled
        ? '<button class="btn btn-s" style="flex:1 1 45%" onclick="vaToggle(false)">⏸ Остановить</button>'
        : '<button class="btn btn-s" style="flex:1 1 45%" onclick="vaToggle(true)">▶️ Запустить</button>') +
    '</div></div>';
  h += _vaDraftsHtml(d.drafts || [], 'ch');
  h += _vaReportHtml(d.report, s);
  h += _vaBriefHtml(s.brief || {});
  h += _vaRefsHtml(d.references || []);
  const plan = d.plan || [];
  h += '<div class="sec">Контент-план</div><div class="lst">' + (plan.length ? plan.slice(0, 14).map(function (p) {
    return '<div class="li"><div class="li-body"><div class="li-name">' + esc(p.pillar || 'Пост') + '</div>' +
      '<div class="li-sub">' + _vaWhen(p.slot_at) + (p.topic ? ' · ' + esc(p.topic) : '') + '</div></div></div>';
  }).join('') : '<div style="padding:14px;color:var(--hint);font-size:13px">План появится после того, как администратор изучит канал.</div>') + '</div>';
  // Настройки шли одной простынёй из двадцати полей, и кнопка «Сохранить»
  // лежала в самом низу — на телефоне до неё четыре пролистывания. Сверху
  // оставили то, ради чего на экран заходят (о проекте, куда вести клиентов,
  // темп и режим), остальное — в двух свёрнутых группах. Поля остаются в
  // разметке и в закрытой группе, поэтому «Сохранить» по-прежнему отправляет
  // их все.
  h += '<div class="sec">Настройки</div><div class="lst" style="padding:14px">' +
    '<div class="field"><label>О проекте своими словами</label><textarea id="vaProject" rows="3" maxlength="2000">' + esc(s.project_info) + '</textarea></div>' +
    '<div class="field"><label>Куда вести клиентов</label><input id="vaContact" maxlength="200" value="' + esc(s.lead_contact) + '" placeholder="@manager, сайт или номер"></div>' +
    _vaBusinessHtml(s.business || {}) +
    '<div style="display:flex;gap:10px">' +
      '<div class="field" style="flex:1"><label>Постов в день</label><select id="vaPpd">' +
        [1, 2, 3, 4, 5, 6, 8, 10, 12].map(function (n) { return _vaOpt(n, s.posts_per_day, n); }).join('') + '</select></div>' +
      '<div class="field" style="flex:1"><label>Часовой пояс</label><select id="vaTz">' + _vaTz(s.tz_offset) + '</select></div>' +
    '</div>' +
    '<div style="display:flex;gap:10px">' +
      '<div class="field" style="flex:1"><label>Публиковать с</label><select id="vaWs">' + _vaHours(s.window_start) + '</select></div>' +
      '<div class="field" style="flex:1"><label>до</label><select id="vaWe">' + _vaHours(s.window_end) + '</select></div>' +
    '</div>' +
    '<div class="field"><label>Режим</label><select id="vaMode">' +
      _vaOpt('auto', s.publish_mode, 'Полностью сам — публикует без меня') +
      _vaOpt('review', s.publish_mode, 'Присылает пост мне на одобрение') + '</select>' +
      '<div class="field-note">В автономном режиме в канал уходят только посты, к которым у редактора нет замечаний.</div></div>' +
    '<details class="acc-actions" style="margin:0 0 12px;background:var(--bg3);border-radius:12px">' +
      '<summary style="cursor:pointer;font-size:14px;font-weight:500;padding:12px 12px;display:flex;align-items:center;gap:8px">' +
        '<span style="flex:1;min-width:0">📝 Как писать</span>' +
        '<span style="font-size:12px;color:var(--hint);font-weight:400;white-space:nowrap">заполнил сам</span>' +
        '<span class="acc-chev" style="color:var(--hint)">▾</span>' +
      '</summary>' +
      '<div style="padding:0 12px 2px">' +
        '<div class="field-note" style="margin:0 0 10px">Тон, рубрики и запреты администратор заполнил сам, когда изучил канал. Меняйте, если он понял что-то не так.</div>' +
        '<div class="field"><label>Тематика канала</label><textarea id="vaTopic" rows="2" maxlength="500">' + esc(s.topic) + '</textarea></div>' +
        '<div class="field"><label>Аудитория</label><input id="vaAudience" maxlength="300" value="' + esc(s.audience) + '"></div>' +
        '<div class="field"><label>Голос канала</label><input id="vaTone" maxlength="200" value="' + esc(s.tone) + '"></div>' +
        '<div class="field"><label>Пожелания и запреты</label><textarea id="vaNotes" rows="2" maxlength="1000" placeholder="Например: не упоминать цены, обращаться на «вы»">' + esc(s.notes) + '</textarea></div>' +
        '<div class="field"><label>Рубрики и доли</label><textarea id="vaPillars" rows="4">' +
          esc((d.pillars || []).map(function (p) { return p.name + ': ' + p.weight; }).join('\n')) + '</textarea>' +
          '<div class="field-note">Одна на строку, доля 1–10. Администратор сам подстраивает доли по просмотрам.</div></div>' +
        '<label style="display:flex;gap:8px;align-items:center;font-size:13px;margin:4px 0 12px">' +
          '<input type="checkbox" id="vaTune"' + (s.auto_tune ? ' checked' : '') + '> Подстраивать рубрики по статистике</label>' +
      '</div>' +
    '</details>' +
    '<div class="field-err" id="vaErr"></div>' +
    '<button class="btn btn-p" id="vaSaveBtn" style="width:100%" onclick="vaSave()">💾 Сохранить</button>' +
    '<div style="display:flex;gap:8px;margin-top:8px">' +
      '<button class="btn btn-s" style="flex:1" onclick="openEditorialRules(_vaCid)">✍️ Правила редактора</button>' +
      '<button class="btn btn-s" style="flex:1" onclick="vaReconfigure()">🔄 Изучить заново</button>' +
    '</div></div>';
  const ev = d.events || [];
  if (ev.length) {
    h += '<div class="sec">Журнал администратора</div><div class="lst" style="padding:10px 14px;font-size:12px;line-height:1.6">' +
      ev.map(function (e) { return '<div><span style="color:var(--hint)">' + _vaWhen(e.at) + '</span> · ' + esc(e.text) + '</div>'; }).join('') +
      '</div>';
  }
  return h;
}

function _vaVal(id) { const el = document.getElementById(id); return el ? el.value : undefined; }

function _vaShowErr(e) {
  const el = document.getElementById('vaErr');
  if (!el) { toast('⚠️ ' + ((e && e.message) || 'Ошибка')); return; }
  el.textContent = (e && e.message) || 'Не получилось';
  el.style.display = 'block';
}

async function vaInstall() {
  const btn = document.getElementById('vaInstallBtn');
  btn.disabled = true; btn.textContent = '⏳ Ставлю…';
  try {
    const d = await api('/api/miniapp/va/channel/' + encodeURIComponent(_vaCid) + '/install', {
      method: 'POST',
      body: JSON.stringify({
        project_info: _vaVal('vaProject') || '',
        lead_contact: _vaVal('vaContact') || '',
        posts_per_day: Number(_vaVal('vaPpd') || 2),
      }),
    });
    tg.HapticFeedback?.notificationOccurred('success');
    toast('✅ Администратор поставлен — изучает канал');
    document.getElementById('s-va-ch-body').innerHTML = _vaChannelHtml(d);
  } catch (e) {
    _vaShowErr(e);
    btn.disabled = false; btn.textContent = '🚀 Поставить администратора';
  }
}

async function vaSave() {
  const btn = document.getElementById('vaSaveBtn');
  btn.disabled = true; btn.textContent = '⏳ Сохраняю…';
  try {
    const d = await api('/api/miniapp/va/channel/' + encodeURIComponent(_vaCid), {
      method: 'PUT',
      body: JSON.stringify({
        project_info: _vaVal('vaProject'), lead_contact: _vaVal('vaContact'),
        topic: _vaVal('vaTopic'), audience: _vaVal('vaAudience'), tone: _vaVal('vaTone'),
        notes: _vaVal('vaNotes'), pillars: _vaVal('vaPillars') || '',
        business: _vaBusinessVal(),
        posts_per_day: Number(_vaVal('vaPpd')), tz_offset: Number(_vaVal('vaTz')),
        window_start: Number(_vaVal('vaWs')), window_end: Number(_vaVal('vaWe')),
        publish_mode: _vaVal('vaMode'), auto_tune: !!document.getElementById('vaTune').checked,
      }),
    });
    toast('✅ Сохранено');
    document.getElementById('s-va-ch-body').innerHTML = _vaChannelHtml(d);
  } catch (e) {
    _vaShowErr(e);
    btn.disabled = false; btn.textContent = '💾 Сохранить';
  }
}

async function vaToggle(on) {
  if (!on && !(await askConfirm('Остановить администратора? Посты в канал выходить перестанут.'))) return;
  try {
    const d = await api('/api/miniapp/va/channel/' + encodeURIComponent(_vaCid), {
      method: 'PUT', body: JSON.stringify({ enabled: !!on }),
    });
    toast(on ? '▶️ Администратор запущен' : '⏸ Администратор остановлен');
    document.getElementById('s-va-ch-body').innerHTML = _vaChannelHtml(d);
  } catch (e) {
    toast('⚠️ ' + ((e && e.message) || 'Не получилось'));
  }
}

async function vaReconfigure() {
  if (!(await askConfirm('Администратор заново изучит канал и пересоберёт тематику, рубрики и контент-план. Продолжить?'))) return;
  try {
    const d = await api('/api/miniapp/va/channel/' + encodeURIComponent(_vaCid) + '/reconfigure', { method: 'POST', body: '{}' });
    toast('🔄 Изучает канал заново');
    document.getElementById('s-va-ch-body').innerHTML = _vaChannelHtml(d);
  } catch (e) {
    toast('⚠️ ' + ((e && e.message) || 'Не получилось'));
  }
}

async function vaPostNow() {
  const btn = document.getElementById('vaNowBtn');
  btn.disabled = true; btn.textContent = '⏳ Пишу пост…';
  try {
    const r = await api('/api/miniapp/va/channel/' + encodeURIComponent(_vaCid) + '/post_now', {
      method: 'POST', body: '{}', timeoutMs: 240000,
    });
    toast((r.ok ? '✅ ' : '⚠️ ') + (r.message || 'Готово'));
  } catch (e) {
    toast('⚠️ ' + ((e && e.message) || 'Не получилось'));
  }
  await _vaLoadChannel();
}
