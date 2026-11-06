// «🧩 Виджеты в чатах» — HTML-виджеты Telegram (HTMLBubbles, Bot API 10.4).
//
// Виджет — сообщение бота с маленькой страницей внутри пузыря чата: опрос с
// живыми голосами, отсчёт, карточка с кнопкой, викторина и т. д. Код виджета
// после отправки не меняется, меняются только данные — поэтому шаблоны
// заполняются формой, а правка уже отправленного виджета меняет тексты, не
// трогая голоса. Сервер: services/mini_app_widgets.py.
//
// Глобальное состояние экрана — объект WG, объявлен в index.html (вынесенные
// экраны — классические скрипты и делят с ним область видимости).

const WG_KIND_INPUT = {
  text: 'text', url: 'url', number: 'number', answer: 'number', when: 'datetime-local',
};

async function openWidgets() {
  push('s-widgets');
  WG.edit = null;
  WG.offset = 0;
  _wgModeLabels();
  document.getElementById('wgErr').style.display = 'none';
  document.getElementById('wgTplGrid').innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/widgets/templates');
    WG.tpls = d.templates || [];
    WG.limits = d.limits || {};
  } catch (e) {
    document.getElementById('wgTplGrid').innerHTML = errHtml(errRu(e, 'Не удалось загрузить шаблоны'), 'openWidgets()');
    return;
  }
  _wgRenderTpls();
  wgPickTpl(WG.tpl && (WG.tpl === 'html' || WG.tpls.some(t => t.id === WG.tpl)) ? WG.tpl : (WG.tpls[0] || {}).id);
  _wgLoadBots();
  wgLoadList(false);
}

function _wgModeLabels() {
  const editing = !!WG.edit;
  txt('wgFormSec', editing ? 'Изменить виджет' : 'Новый виджет');
  txt('wgSendBtn', editing ? '💾 Сохранить изменения' : '📤 Отправить');
  document.getElementById('wgSendWrap').style.display = editing ? 'none' : 'block';
  document.getElementById('wgTplWrap').style.display = editing ? 'none' : 'block';
  document.getElementById('wgCancelEdit').style.display = editing ? 'block' : 'none';
}

function _wgRenderTpls() {
  const tiles = WG.tpls.map(t => `<button type="button" class="btn btn-s wg-tpl" data-tpl="${esc(t.id)}"
      style="flex:1 1 45%;min-height:40px;font-size:13px;padding:8px 6px" onclick="wgPickTpl(this.dataset.tpl)">${esc(t.name)}</button>`);
  tiles.push(`<button type="button" class="btn btn-s wg-tpl" data-tpl="html"
      style="flex:1 1 45%;min-height:40px;font-size:13px;padding:8px 6px" onclick="wgPickTpl('html')">🧑‍💻 Свой HTML-код</button>`);
  document.getElementById('wgTplGrid').innerHTML = tiles.join('');
}

function _wgTpl(id) { return WG.tpls.find(t => t.id === id) || null; }

function wgPickTpl(id) {
  if (!id) return;
  WG.tpl = id;
  for (const b of document.querySelectorAll('#wgTplGrid .wg-tpl')) {
    b.className = 'btn wg-tpl ' + (b.dataset.tpl === id ? 'btn-p' : 'btn-s');
  }
  const t = _wgTpl(id);
  txt('wgTplDesc', id === 'html'
    ? 'Свой код виджета: HTML, стили и скрипт в одном файле. Без сети, внешних картинок и полей ввода; после сжатия — не больше 4 КБ.'
    : (t ? t.desc : ''));
  document.getElementById('wgHtmlWrap').style.display = id === 'html' ? 'block' : 'none';
  _wgRenderFields(t, WG.edit ? WG.edit.data : null);
  wgPreview();
}

// Поле формы — по виду из шаблона (services/bot_widgets.py TEMPLATES.fields).
function _wgFieldHtml(f, val) {
  const req = f.need ? ' *' : '';
  const ph = f.hint ? ` placeholder="${esc(f.hint)}"` : '';
  const max = f.max ? ` maxlength="${esc(f.max)}"` : '';
  let input;
  if (f.kind === 'long' || f.kind === 'options') {
    const rows = f.kind === 'options' ? 5 : 3;
    const lock = (f.kind === 'options' && WG.edit && WG.edit.template === 'poll') ? ' readonly' : '';
    input = `<textarea class="inp wg-f" data-key="${esc(f.key)}" data-kind="${esc(f.kind)}" rows="${rows}"${ph}${max}${lock}
      style="width:100%;box-sizing:border-box;resize:vertical" oninput="wgPreviewSoon()">${esc(val == null ? '' : val)}</textarea>`;
    if (lock) input += '<div class="field-note">Варианты опроса после отправки не меняются: голоса привязаны к ним</div>';
  } else {
    const type = WG_KIND_INPUT[f.kind] || 'text';
    const extra = f.kind === 'answer' ? ' min="1" max="6"' : (f.kind === 'number' ? ' min="0" step="any" inputmode="decimal"' : '');
    input = `<input type="${type}" class="inp wg-f" data-key="${esc(f.key)}" data-kind="${esc(f.kind)}"${ph}${max}${extra}
      value="${esc(val == null ? '' : val)}" style="width:100%;box-sizing:border-box" oninput="wgPreviewSoon()">`;
  }
  return `<div class="field"><label>${esc(f.label)}${req}</label>${input}</div>`;
}

function _wgFormValue(f, data) {
  if (!data || data[f.key] == null) return '';
  const v = data[f.key];
  if (f.kind === 'options') return Array.isArray(v) ? v.join('\n') : '';
  if (f.kind === 'answer') return Number(v) + 1;
  if (f.kind === 'when') {
    const d = new Date(Number(v) * 1000);
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  return v;
}

function _wgRenderFields(t, data) {
  const box = document.getElementById('wgFields');
  box.innerHTML = t ? t.fields.map(f => _wgFieldHtml(f, _wgFormValue(f, data))).join('') : '';
}

// Значения формы → то, что ждёт сервер (values шаблона).
function _wgValues() {
  const out = {};
  for (const el of document.querySelectorAll('#wgFields .wg-f')) {
    const k = el.dataset.key, kind = el.dataset.kind, v = (el.value || '').trim();
    if (kind === 'when') out[k] = v ? Math.floor(new Date(v).getTime() / 1000) : '';
    else if (kind === 'options') out[k] = v.split('\n').map(s => s.trim()).filter(Boolean);
    else out[k] = v;
  }
  return out;
}

// Данные для предпросмотра — те же, что соберёт сервер (без проверок).
function _wgPreviewData() {
  const v = _wgValues(), id = WG.tpl, out = {};
  for (const [k, x] of Object.entries(v)) {
    if (x === '' || (Array.isArray(x) && !x.length)) continue;
    out[k] = x;
  }
  if (id === 'quiz' && out.a) out.a = Number(out.a) - 1;
  if (id === 'goal') { out.g = Number(out.g) || 0; out.h = Number(out.h) || 0; }
  if (id === 'countdown' && !out.at) out.at = Math.floor(Date.now() / 1000) + 3 * 86400 + 4000;
  const n = id === 'poll' ? (out.o || []).length : ({rating: 5, rsvp: 3})[id] || 0;
  if (n) {
    const old = WG.edit && WG.edit.data;
    out.v = (old && Array.isArray(old.v)) ? old.v : Array(n).fill(0);
    out.r = 0;
    if (id === 'rsvp') out.n = (old && old.n) || [];
  }
  return out;
}

let _wgPrevTimer = 0;
function wgPreviewSoon() {
  clearTimeout(_wgPrevTimer);
  _wgPrevTimer = setTimeout(wgPreview, 350);
}

// Тема Telegram для пузыря: те же переменные --tg-theme-*, что даст клиент.
function _wgTheme() {
  const tp = (window.Telegram && Telegram.WebApp && Telegram.WebApp.themeParams) || {};
  const dark = (window.Telegram && Telegram.WebApp && Telegram.WebApp.colorScheme) === 'dark';
  const pick = (k, d) => /^#[0-9a-fA-F]{3,8}$/.test(tp[k] || '') ? tp[k] : d;
  return {
    'bg-color': pick('bg_color', dark ? '#212d3b' : '#ffffff'),
    'text-color': pick('text_color', dark ? '#ffffff' : '#000000'),
    'hint-color': pick('hint_color', dark ? '#7d8b99' : '#999999'),
    'link-color': pick('link_color', '#2481cc'),
    'button-color': pick('button_color', '#2481cc'),
    'button-text-color': pick('button_text_color', '#ffffff'),
    'secondary-bg-color': pick('secondary_bg_color', dark ? '#2b3a4a' : '#efeff3'),
    'accent-text-color': pick('accent_text_color', '#2481cc'),
    'destructive-text-color': pick('destructive_text_color', '#e53935'),
    'section-separator-color': pick('section_separator_color', dark ? '#3a4a5a' : '#e7e7e7'),
  };
}

// Заглушка TelegramWidget для предпросмотра: данные из формы, голосование —
// понарошку, внутри рамки; ссылки не открываются.
function _wgStub(publicData, tpl) {
  const theme = _wgTheme();
  const vars = Object.entries(theme).map(([k, v]) => `--tg-theme-${k}:${v}`).join(';');
  const pd = JSON.stringify(publicData).replace(/</g, '\\u003c');
  const tp = JSON.stringify(tpl).replace(/</g, '\\u003c');
  return '<!doctype html><meta charset="utf-8"><style>:root{' + vars + '}'
    + 'body{background:var(--tg-theme-bg-color);color:var(--tg-theme-text-color);'
    + 'font:15px/1.35 -apple-system,system-ui,Roboto,sans-serif}button{cursor:pointer}</style>'
    + '<script>(function(){let my=-1;const H={impactOccurred(){},notificationOccurred(){},selectionChanged(){}};'
    + 'const T={publicData:' + pd + ',chatData:"",isPreview:false,isForward:false,chatType:"private",'
    + 'viewer:{id:1,first_name:"Вы"},HapticFeedback:H,onEvent(){},watchPublicData(){},'
    + 'openLink(){},openTelegramLink(){},copyText(){},setResizeAnchor(){},'
    + 'request(d){const m=/^vote:(\\d+)$/.exec(d||"");if(!m)return Promise.reject({code:"FAILED"});'
    + 'let P={};try{P=JSON.parse(T.publicData||"{}")}catch(e){}const v=(P.v||[]).slice(),n=+m[1];'
    + 'if(my>=0)v[my]=Math.max(0,(v[my]||0)-1);if(my===n)my=-1;else{my=n;v[n]=(v[n]||0)+1}'
    + 'P.v=v;P.r=(P.r|0)+1;if(' + tp + '==="rsvp")P.n=my===0?["Вы"]:[];T.publicData=JSON.stringify(P);'
    + 'return new Promise(r=>setTimeout(()=>r({result:JSON.stringify({my:my,v:v,r:P.r,n:P.n})}),250))}};'
    + 'window.TelegramWidget=T})()<\/script>';
}

async function wgPreview() {
  const frame = document.getElementById('wgPreview');
  if (!frame) return;
  let html, data = '';
  if (WG.tpl === 'html') {
    html = document.getElementById('wgHtml').value || '';
    _wgCheckOwn(html);
  } else {
    const t = _wgTpl(WG.tpl);
    if (!t) return;
    html = t.html;
    data = JSON.stringify(_wgPreviewData());
    const bytes = new TextEncoder().encode(data).length;
    txt('wgSize', `Код шаблона после сжатия: ${t.packed} из ${WG.limits.packed_max || 4096} байт · данные: ${bytes} из ${WG.limits.data_max || 1024} байт`);
    document.getElementById('wgSize').style.color = bytes > (WG.limits.data_max || 1024) ? 'var(--red)' : '';
  }
  frame.srcdoc = _wgStub(data, WG.tpl) + html;
}

let _wgCheckTimer = 0;
function _wgCheckOwn(html) {
  clearTimeout(_wgCheckTimer);
  if (!html.trim()) { txt('wgSize', ''); return; }
  _wgCheckTimer = setTimeout(async () => {
    try {
      const r = await api('/api/miniapp/widgets/check', {method: 'POST', body: JSON.stringify({html})});
      const lines = [`Размер: ${r.bytes} байт, после сжатия ${r.packed} из ${WG.limits.packed_max || 4096}`]
        .concat(r.errors.map(x => '⛔ ' + x), r.warnings.map(x => '⚠️ ' + x));
      txt('wgSize', lines.join('\n'));
      document.getElementById('wgSize').style.color = r.ok ? '' : 'var(--red)';
    } catch (e) {
      txt('wgSize', 'Проверить размер не удалось: ' + errRu(e));
    }
  }, 500);
}

async function _wgLoadBots() {
  const sel = document.getElementById('wgBot');
  sel.innerHTML = '';
  sel.add(new Option('Загрузка…', ''));
  try {
    const d = await api('/api/miniapp/bots?limit=500');
    const bots = (d.bots || []).filter(b => b.is_active);
    sel.innerHTML = '';
    if (!bots.length) sel.add(new Option('Нет активных ботов — добавьте бота в разделе «Боты»', ''));
    for (const b of bots) sel.add(new Option('@' + (b.username || b.first_name || b.bot_id), String(b.bot_id)));
  } catch (e) {
    sel.innerHTML = '';
    sel.add(new Option('Не удалось загрузить ботов', ''));
  }
}

function wgWhereToggle() {
  const v = document.getElementById('wgWhere').value;
  document.getElementById('wgChatWrap').style.display = v === 'chat' ? 'block' : 'none';
  txt('wgWhereNote', v === 'me'
    ? 'Виджет придёт вам в личку с ботом — так удобно проверить вид. Если ещё не открывали этого бота, откройте его и нажмите «Старт».'
    : 'Бот должен состоять в группе и иметь право отправлять медиа. В каналы Telegram виджеты не пускает — туда виджет можно переслать.');
}

async function wgSend() {
  const errEl = document.getElementById('wgErr');
  const fail = m => { errEl.textContent = m; errEl.style.display = 'block'; tg.HapticFeedback?.notificationOccurred('error'); };
  errEl.style.display = 'none';
  if (WG.edit) return _wgSaveEdit(fail);
  const bot_id = document.getElementById('wgBot').value;
  if (!bot_id) return fail('Выберите бота');
  const where = document.getElementById('wgWhere').value;
  const chat = where === 'me' ? 'me' : (document.getElementById('wgChat').value || '').trim();
  if (!chat) return fail('Укажите группу: @username или числовой id');
  const body = {bot_id: Number(bot_id), chat, template: WG.tpl,
    caption: (document.getElementById('wgCaption').value || '').trim()};
  if (WG.tpl === 'html') {
    body.html = document.getElementById('wgHtml').value || '';
    if (!body.html.trim()) return fail('Вставьте код виджета');
  } else {
    body.values = _wgValues();
  }
  const btn = document.getElementById('wgSendBtn');
  btn.disabled = true; btn.textContent = '⏳ Отправка…';
  try {
    const d = await api('/api/miniapp/widgets/send', {method: 'POST', body: JSON.stringify(body)});
    tg.HapticFeedback?.notificationOccurred('success');
    toast(d.warning ? '⚠️ ' + d.warning : (where === 'me' ? '✅ Виджет у вас в личке с ботом' : '✅ Виджет отправлен'));
    wgLoadList(false);
  } catch (e) {
    fail(errRu(e, 'Не удалось отправить виджет'));
  } finally {
    btn.disabled = false; _wgModeLabels();
  }
}

async function wgLoadList(more) {
  const box = document.getElementById('wgList');
  if (!more) { WG.offset = 0; box.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>'; }
  try {
    const d = await api(`/api/miniapp/widgets?limit=30&offset=${WG.offset}`);
    const items = d.items || [];
    const html = items.map(_wgRow).join('');
    if (!more) box.innerHTML = html || empty('🧩', 'Виджетов пока нет', 'Соберите первый выше и отправьте себе — посмотреть, как он выглядит в чате');
    else box.insertAdjacentHTML('beforeend', html);
    WG.offset += items.length;
    WG.items = more ? (WG.items || []).concat(items) : items;
    document.getElementById('wgMore').innerHTML = moreBtn(WG.offset, d.total, 'wgLoadList(true)');
  } catch (e) {
    if (!more) box.innerHTML = errHtml(errRu(e, 'Не удалось загрузить виджеты'), 'wgLoadList(false)');
    else toast('⚠️ ' + errRu(e, 'Не удалось загрузить ещё'));
  }
}

function _wgVotesLine(w) {
  const d = w.data || {}, v = Array.isArray(d.v) ? d.v.map(x => Number(x) || 0) : null;
  if (!v) return '';
  const total = v.reduce((a, b) => a + b, 0);
  if (!total) return 'Голосов пока нет';
  if (w.template === 'rating') {
    const avg = v.reduce((a, b, i) => a + b * (i + 1), 0) / total;
    return `Средняя оценка ${avg.toFixed(1).replace('.', ',')} · оценок: ${total}`;
  }
  if (w.template === 'rsvp') return `Идут: ${v[0] || 0} · может быть: ${v[1] || 0} · не идут: ${v[2] || 0}`;
  const opts = Array.isArray(d.o) ? d.o : [];
  return opts.map((o, i) => `${o}: ${Math.round((v[i] || 0) * 100 / total)}%`).join(' · ') + ` · всего ${total}`;
}

function _wgRow(w) {
  const where = Number(w.chat_id) > 0 ? 'личка' : 'группа';
  const when = new Date(w.created_at).toLocaleString('ru-RU', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
  const votes = _wgVotesLine(w);
  return `<div class="row" style="flex-wrap:wrap">
    <div class="row-ico" style="background:var(--bg-blue-16)">🧩</div>
    <div class="row-body">
      <div class="row-name">${esc(w.title)}</div>
      <div class="row-val">${esc(w.template_name)} · @${esc(w.bot_username)} · ${esc(where)} · ${esc(when)}</div>
      ${votes ? `<div class="row-val" style="margin-top:2px">${esc(votes)}</div>` : ''}
    </div>
    <div style="display:flex;gap:6px;width:100%;margin-top:8px">
      ${w.editable ? `<button class="btn btn-s" style="flex:1;min-height:32px;font-size:13px;padding:6px" onclick="wgOpenEdit(${Number(w.id)})">✏️ Изменить</button>` : ''}
      ${w.shared ? `<button class="btn btn-s" style="flex:1;min-height:32px;font-size:13px;padding:6px" onclick="wgBroadcast(${Number(w.id)})">📣 Подписчикам</button>` : ''}
      <button class="btn btn-s" style="flex:1;min-height:32px;font-size:13px;padding:6px" onclick="wgDelete(${Number(w.id)}, this)">🗑 Удалить</button>
    </div>
  </div>`;
}

function wgOpenEdit(id) {
  const w = (WG.items || []).find(x => Number(x.id) === Number(id));
  if (!w) return;
  WG.edit = w;
  _wgModeLabels();
  wgPickTpl(w.template);
  document.getElementById('s-widgets').querySelector('.sub-sb').scrollTo({top: 0, behavior: 'smooth'});
}

function wgCancelEdit() {
  WG.edit = null;
  _wgModeLabels();
  wgPickTpl(WG.tpl);
}

async function _wgSaveEdit(fail) {
  const btn = document.getElementById('wgSendBtn');
  btn.disabled = true; btn.textContent = '⏳ Сохранение…';
  try {
    await api(`/api/miniapp/widgets/${Number(WG.edit.id)}/edit`, {method: 'POST', body: JSON.stringify({values: _wgValues()})});
    tg.HapticFeedback?.notificationOccurred('success');
    toast('✅ Виджет обновлён во всех чатах, где он есть');
    WG.edit = null;
    wgPickTpl(WG.tpl);
    wgLoadList(false);
  } catch (e) {
    fail(errRu(e, 'Не удалось сохранить изменения'));
  } finally {
    btn.disabled = false; _wgModeLabels();
  }
}

async function wgDelete(id, el) {
  if (!await confirmDelete('Удалить виджет', el, 'Сообщение с виджетом исчезнет из чата, голоса будут удалены.')) return;
  try {
    await api(`/api/miniapp/widgets/${Number(id)}/delete`, {method: 'POST', body: JSON.stringify({})});
    toast('🗑 Виджет удалён');
  } catch (e) {
    const msg = errRu(e, 'Не удалось удалить');
    if (!await askConfirm(msg + '\n\nУбрать виджет только из этого списка? В чате он останется.')) return;
    try {
      await api(`/api/miniapp/widgets/${Number(id)}/delete`, {method: 'POST', body: JSON.stringify({forget_only: true})});
      toast('Виджет убран из списка');
    } catch (e2) {
      toast('⚠️ ' + errRu(e2, 'Не удалось убрать из списка'));
      return;
    }
  }
  if (WG.edit && Number(WG.edit.id) === Number(id)) wgCancelEdit();
  wgLoadList(false);
}

// Разослать общий виджет подписчикам бота: та же рассылка, что у текста
// (журнал доставок, стоп, докатка), но у всех получателей один счёт голосов.
// Число адресатов — ДО подтверждения.
async function wgBroadcast(id) {
  const w = (WG.items || []).find(x => Number(x.id) === Number(id));
  if (!w) return;
  const segment = await askChoice('Кому разослать виджет', [
    {value: 'all', label: 'Всем активным подписчикам'},
    {value: 'active_7d', label: 'Заходившим за 7 дней'},
    {value: 'active_30d', label: 'Заходившим за 30 дней'},
    {value: 'inactive_14d', label: 'Спящим — не заходили 14+ дней'},
  ]);
  if (!segment) return;
  let count = 0;
  try {
    const r = await api(`/api/miniapp/broadcast/recipients?bot_id=${Number(w.bot_id)}&segment=${encodeURIComponent(segment)}`);
    count = Number(r.count) || 0;
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось посчитать получателей'));
    return;
  }
  if (!count) { toast('Некому отправлять: в этой аудитории нет активных подписчиков'); return; }
  if (!await askConfirm(`Виджет «${w.title}» получат ${num(count)} подписчиков бота @${w.bot_username}. `
      + 'Рассылку видно и можно остановить в «Операциях». Запустить?')) return;
  try {
    const d = await api(`/api/miniapp/widgets/${Number(id)}/broadcast`, {method: 'POST', body: JSON.stringify({segment})});
    tg.HapticFeedback?.notificationOccurred('success');
    toast(`✅ Рассылка виджета запущена · ${num(d.total)} получателей`);
  } catch (e) {
    toast('⚠️ ' + errRu(e, 'Не удалось запустить рассылку'));
  }
}
