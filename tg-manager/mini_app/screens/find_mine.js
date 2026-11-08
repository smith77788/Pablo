// 🔎 «Найдено у вас»: поиск по своим объектам из строки поиска каталога.
//
// Строка поиска «Ещё» искала только функции. Человек, вводящий имя клиента
// или @бота, получал «ничего не найдено», хотя объект у него есть. Теперь
// под строкой — совпадения среди своих контактов, ботов, аккаунтов, каналов,
// ИИ-менеджеров и операций (services/mini_app_find.py), каждая строка
// открывает объект. Функции по-прежнему фильтруются ниже (filterMore).
let _fmTimer = null;
let _fmSeq = 0;
// Каждая группа знает, чем открыть свой объект. Нет функции — строка не
// кликается (см. _fmOpen), а не молчит при нажатии.
const _FM_OPEN = {
  contacts:   id => openContactDetail(id),
  deals:      id => _fmOpenDeal(Number(id)),
  bots:       id => openBot(Number(id)),
  accounts:   id => openAccount(Number(id)),
  channels:   id => openChannel(Number(id)),
  personas:   id => openPersonaEditor(Number(id)),
  operations: id => openOpDetail(Number(id)),
};
const _FM_MORE = {
  contacts: 'openContacts()', deals: 'openDeals()', bots: "goTab('bots')", accounts: "goTab('accounts')",
  channels: 'openChannels()', personas: 'openSalesManager()', operations: 'openMassOps()',
};
// Сделка открывается на доске своей карточкой. Доска показывает первые
// карточки колонки; если найденная сделка дальше, говорим это прямо, а не
// открываем молча доску без неё.
async function _fmOpenDeal(id) {
  await openDeals();
  if (typeof _dkFindDeal === 'function' && _dkFindDeal(id)) { dkOpenDeal(id); return; }
  toast('Сделка на доске ниже первых карточек — нажмите «Ещё» в её колонке');
}
// Кнопка объекта из /find в боте открывает мини-апп с #obj:<группа>.<id> —
// тот же переход, что строка «Найдено у вас» здесь.
function openFoundObject(ref) {
  const dot = String(ref || '').indexOf('.');
  const key = dot > 0 ? ref.slice(0, dot) : '';
  const id = dot > 0 ? ref.slice(dot + 1) : '';
  const f = _FM_OPEN[key];
  if (!f || !id) { toast('⚠️ Ссылка на объект повреждена'); return; }
  try { return f(id); } catch (_) { toast('⚠️ Не получилось открыть'); }
}
function findMine(q) {
  const box = document.getElementById('moreFindMine');
  if (!box) return;
  q = String(q || '').trim();
  clearTimeout(_fmTimer);
  if (q.length < 2) { box.style.display = 'none'; box.innerHTML = ''; return; }
  _fmTimer = setTimeout(() => _fmRun(q), 250);
}
async function _fmRun(q) {
  const box = document.getElementById('moreFindMine');
  const seq = ++_fmSeq;
  box.style.display = 'block';
  box.innerHTML = '<div class="hint" style="padding:8px 2px;font-size:12px">Ищу у вас…</div>';
  let d;
  try { d = await api('/api/miniapp/find?q=' + encodeURIComponent(q)); }
  catch (e) {
    if (seq !== _fmSeq) return;
    box.innerHTML = '<div class="hint" style="padding:8px 2px;font-size:12px">⚠️ ' + esc(errRu(e)) + '</div>';
    return;
  }
  if (seq !== _fmSeq) return;   // пришёл ответ на старый запрос — не перетираем новый
  const groups = d.groups || [];
  if (!groups.length) { box.innerHTML = '<div class="hint" style="padding:8px 2px;font-size:12px">У вас ничего не нашлось по «' + esc(q) + '»</div>'; return; }
  box.innerHTML = '<div class="sec" style="margin-top:8px">Найдено у вас · ' + num(d.total || 0) + '</div>' +
    groups.map(g => {
      const rows = (g.items || []).map((it, i) =>
        '<div class="row tap" role="button" tabindex="0" style="min-height:44px" onclick="_fmOpen(\'' + esc(g.key) + '\',' + i + ')">' +
          '<div class="row-body"><div class="row-name">' + esc(it.title) + '</div>' +
          (it.sub ? '<div class="row-val">' + esc(it.sub) + '</div>' : '') + '</div></div>').join('');
      const more = g.has_more && _FM_MORE[g.key]
        ? '<button class="btn btn-s" style="margin:4px 0 8px;font-size:12px;padding:6px 12px" onclick="' + _FM_MORE[g.key] + '">Ещё ' + num(g.total - g.items.length) + ' — открыть раздел</button>' : '';
      return '<div style="font-size:12px;color:var(--hint);margin:8px 2px 2px">' + esc(g.label) + ' · ' + num(g.total) + '</div>' +
        '<div class="card" style="padding:0 12px">' + rows + '</div>' + more;
    }).join('');
  box._groups = groups;
}
function _fmOpen(key, i) {
  const box = document.getElementById('moreFindMine');
  const it = box && box._groups && (box._groups.find(g => g.key === key) || {}).items;
  const row = it && it[i];
  const f = _FM_OPEN[key];
  if (!row || typeof f !== 'function') { toast('⚠️ Этот объект отсюда не открывается'); return; }
  try { f(row.id); } catch (_) { toast('⚠️ Не получилось открыть'); }
}
