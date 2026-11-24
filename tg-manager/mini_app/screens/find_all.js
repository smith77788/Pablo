// ── Поиск по своему хозяйству (screens/find_all.js) ────────────────────────
// Поиск в мини-аппе был у каждого списка свой: чтобы найти «тот аккаунт»,
// «того бота» или «ту рассылку», надо было вспомнить, в каком разделе искать,
// дойти до раздела и набрать запрос там. Экран «🔎 Поиск Telegram» ищет
// ЧУЖОЕ — публичные каналы и ботов; по своему хозяйству поиска не было нигде.
//
// Здесь один запрос на шесть таблиц: аккаунты, боты, каналы, контакты,
// рассылки и операции. Находка несёт имя функции экрана и аргумент — экран
// зовёт функцию по имени и проверяет, что такая есть (тот же приём, что у
// быстрых действий по итогу операции).
'use strict';

let FA_Q = '';
let _faT = null;
let FA_LAST = null;
let FA_PER = 0;              // сколько находок в группе просим (0 — по умолчанию)
const FA_HISTORY_KEY = 'infragram_find_history';
const FA_HISTORY_MAX = 8;

// Последние запросы: искать приходится одно и то же — «тот номер», «тот
// канал», — и набирать это заново с телефона дороже, чем кажется.
function faHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(FA_HISTORY_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter(x => typeof x === 'string') : [];
  } catch (_) { return []; }
}

function faRemember(q) {
  if (!q || q.length < 2) return;
  const next = [q].concat(faHistory().filter(x => x !== q)).slice(0, FA_HISTORY_MAX);
  try { localStorage.setItem(FA_HISTORY_KEY, JSON.stringify(next)); } catch (_) {}
}

function faForget() {
  try { localStorage.removeItem(FA_HISTORY_KEY); } catch (_) {}
  faSearch(FA_Q);
}

// Подсветка найденного куска. Экранируем СНАЧАЛА, подсвечиваем потом: наоборот
// — и <mark> уехал бы в текст, а разметка владельца стала бы разметкой.
function faMark(text, q) {
  const safe = esc(text);
  if (!q) return safe;
  const needle = esc(q).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  try {
    return safe.replace(new RegExp(needle, 'gi'), m => '<mark class="fa-hit">' + m + '</mark>');
  } catch (_) { return safe; }
}

function openFindAll(q) {
  push('s-findall');
  const el = document.getElementById('faQuery');
  if (el) {
    if (q !== undefined) el.value = q || '';
    try { el.focus(); } catch (_) {}
  }
  // Экран никогда не открывается пустым: без находок он объясняет, что здесь
  // ищут. Пустой экран без единого слова читается как сломанный.
  if (q !== undefined) faSearch(q || '');
  else if (FA_LAST) _faRender(FA_LAST);
  else faSearch(el ? el.value : '');
}

// Чип истории открывает запрос ПО НОМЕРУ, а не подставляет его текст в
// разметку обработчика: апостроф в запросе («О'Нил») порвал бы атрибут, а
// кавычки JSON — саму строку. Значение берём из того же списка, который
// рисовали.
function faFromHistory(i) {
  const hist = faHistory();
  const q = hist[Number(i)];
  if (typeof q === 'string' && q) openFindAll(q);
}

function faSearch(v) {
  const next = (v || '').trim();
  // Новый запрос — снова узкая выдача: «показать ещё» относилось к прошлому
  // запросу, а не к набранному сейчас.
  if (next !== FA_Q) FA_PER = 0;
  FA_Q = next;
  clearTimeout(_faT);
  if (FA_Q.length < 2) {
    const hist = faHistory();
    const chips = hist.length
      ? '<div class="nav-cat">🕘 Последние запросы</div><div class="lf-chips">' +
        hist.map((q, i) => '<span class="chip" role="button" tabindex="0" onclick="faFromHistory(' +
          i + ')">' + esc(q) + '</span>').join('') +
        '<span class="chip" role="button" tabindex="0" onclick="faForget()">✖️ Очистить</span></div>'
      : '';
    txt('faResults', (FA_Q
      ? _faHint('Ещё хотя бы один символ — по одной букве нашлось бы всё подряд')
      : _faHint('Телефон, ник, название канала, имя контакта, слово из рассылки или название операции'))
      + chips);
    return;
  }
  // Запрос уходит на сервер, поэтому ждём паузу в наборе: иначе на каждое
  // нажатие уходило бы шесть запросов к базе.
  _faT = setTimeout(_faGo, 300);
}

async function _faGo() {
  const q = FA_Q;
  txt('faResults', '<div class="spin-wrap"><div class="spin"></div></div>');
  try {
    const d = await api('/api/miniapp/search/everything?q=' + encodeURIComponent(q)
      + (FA_PER ? '&per=' + FA_PER : ''));
    if (q !== FA_Q) return;          // человек уже набрал дальше
    FA_LAST = d;
    // Запоминаем только то, что НАШЛОСЬ: история из промахов — это список
    // мест, где уже искали впустую.
    if ((d.found || 0) > 0) faRemember(q);
    _faRender(d);
  } catch (e) {
    txt('faResults', errHtml(errRu(e), 'faSearch(' + JSON.stringify(q) + ')'));
  }
}

function _faHint(text) {
  return '<div class="hint" style="padding:12px">' + esc(text) + '</div>';
}

// Кнопка строится ровно так же, как быстрые действия по итогу операции:
// функция зовётся ПО ИМЕНИ и только если она есть. Иначе переименование на
// фронте превращало бы находку в кнопку, которая не делает ничего.
function _faClick(it) {
  if (!it.fn || typeof window[it.fn] !== 'function') return '';
  const arg = typeof it.arg === 'number'
    ? String(it.arg)
    : "'" + String(it.arg == null ? '' : it.arg).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
  return it.fn + '(' + arg + ')';
}

// Разделы приложения ищутся ЗДЕСЬ, по самому каталогу: у них нет и не должно
// быть таблицы в базе, а искать «где включить прогрев» человек приходит тем же
// полем, что и «тот номер». Раньше для этого было отдельное поле в боковом
// меню — то есть надо было помнить, что раздел ищут не там, где данные.
function faSections(q) {
  const more = document.getElementById('s-more');
  const needle = String(q || '').trim().toLowerCase();
  if (!more || needle.length < 2) return [];
  const out = [];
  more.querySelectorAll('.mgmt-tile').forEach(tile => {
    if (out.length >= 6) return;
    const lbl = ((tile.querySelector('.mgmt-tile-lbl') || {}).textContent || '').trim();
    if (!lbl) return;
    const desc = (typeof MODULE_DESCS !== 'undefined' && MODULE_DESCS[lbl]) || '';
    if (!lbl.toLowerCase().includes(needle) && !desc.toLowerCase().includes(needle)) return;
    const icoEl = tile.querySelector('.mgmt-tile-ico');
    const ico = (icoEl && (icoEl.dataset.emoji || icoEl.textContent || '').trim()) || '•';
    // Обработчик берём из НАШЕЙ разметки — это тот же переход, что у плитки
    // каталога, поэтому раздел не разъедется с каталогом при переименовании.
    out.push({ title: lbl, subtitle: desc, ico, go: tile.getAttribute('onclick') || '' });
  });
  return out;
}

function _faRender(d) {
  const groups = (d.groups || []).filter(g => (g.items || []).length);
  const sections = faSections(d.q);
  if (!groups.length && !sections.length) {
    txt('faResults', empty('🔍', 'Ничего не найдено',
      'По запросу «' + (d.q || '') + '» ничего нет ни в разделах приложения, ни в ' +
      'аккаунтах, ботах, каналах, контактах, рассылках и операциях'));
    return;
  }
  let html = '';
  if (sections.length) {
    html += '<div class="nav-cat">🧭 Разделы приложения · ' + String(sections.length) + '</div>';
    html += sections.map(it =>
      '<div class="nav-item has-desc" role="button" tabindex="0" onclick="' + esc(it.go) + '">' +
      '<span class="ni-ico">' + esc(it.ico) + '</span>' +
      '<span class="ni-body"><span>' + faMark(it.title, d.q) + '</span>' +
      (it.subtitle ? '<span class="ni-desc">' + faMark(it.subtitle, d.q) + '</span>' : '') +
      '</span></div>').join('');
  }
  for (const g of groups) {
    html += '<div class="nav-cat">' + esc(g.icon) + ' ' + esc(g.label) +
      ' · ' + String(g.items.length) + '</div>';
    html += g.items.map(it => {
      const go = _faClick(it);
      const body = '<span class="ni-body"><span>' + faMark(it.title, d.q) + '</span>' +
        (it.subtitle ? '<span class="ni-desc">' + faMark(it.subtitle, d.q) + '</span>' : '') +
        '</span>';
      // Находка без рабочей двери остаётся видимой, но не притворяется
      // кнопкой: молча пропасть хуже, чем не нажиматься.
      return go
        ? '<div class="nav-item has-desc" role="button" tabindex="0" onclick="' + esc(go) + '">' +
          '<span class="ni-ico">' + esc(it.icon || '•') + '</span>' + body + '</div>'
        : '<div class="nav-item" style="opacity:.6">' +
          '<span class="ni-ico">•</span>' + body + '</div>';
    }).join('');
  }
  // «Показать ещё» имеет смысл, только если какая-то группа упёрлась в потолок.
  if (d.capped && !FA_PER) {
    html += '<button class="btn btn-s" style="width:100%;margin-top:10px;font-size:13px;' +
      'padding:10px" onclick="faMore()">Показать больше находок</button>';
  }
  // Где не искали — говорим вслух: пустая группа из-за сбоя неотличима от
  // честного «ничего нет», и молчать здесь значит соврать.
  if ((d.failed || []).length) {
    html += '<div class="hint" style="padding:10px 12px">⚠️ Не удалось поискать: ' +
      esc(d.failed.join(', ')) + '. Попробуйте ещё раз.</div>';
  }
  txt('faResults', html);
}

// У рассылки своего экрана нет: ведём в «Сообщения» с тем же запросом в
// поиске истории — то есть в список найденного, а не в строку, которую
// пришлось бы искать там заново.
// Та же выдача шире, а не вторая страница: групп шесть, и «ещё» по каждой
// означало бы шесть счётчиков смещения на экране ради редкого случая.
function faMore() {
  FA_PER = 25;
  _faGo();
}

function openBroadcastSearch(q) {
  goTab('broadcasts');
  const el = document.getElementById('bcastSearch');
  if (el) el.value = q || '';
  onBcastSearch(q || '');
}
