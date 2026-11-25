// ── Быстрые ответы и переменные шаблонов ───────────────────────────────────
// Шаблоны у владельца есть давно (экран «📝 Шаблоны»), и во «Входящих» над
// полем ответа уже стоят их чипы. А в переписке от своего имени (Архив
// диалогов → чат) ответ печатался руками каждый раз, хотя половина ответов —
// одни и те же фразы. Здесь:
//   • чипы быстрых ответов над полем ответа в чате архива, с подстановкой
//     имени собеседника и раскрытием вариантов {а|б};
//   • «частые первыми»: порядок чипов — по тому, как часто шаблон вставляли
//     на этом устройстве (память в localStorage, на сервер ничего не идёт);
//   • кнопки-переменные в редакторе шаблона: {name}, {username} и варианты
//     вставляются одним касанием туда, где стоит курсор, а не набираются
//     вручную со скобками.

// Какие подстановки понимает продукт. Тот же набор, что у личной рассылки
// (cmpText: «{name} — имя, иначе ник · {first_name} · {username}») и у
// предпросмотра шаблона (previewTpl).
const TPL_VARS = [
  {token: '{name}', label: '👤 Имя'},
  {token: '{first_name}', label: 'Имя (только)'},
  {token: '{last_name}', label: 'Фамилия'},
  {token: '{username}', label: '@ Ник'},
];

const _TPL_USE_KEY = 'infragram_tpl_use';

function _tplUses() {
  try { return JSON.parse(localStorage.getItem(_TPL_USE_KEY) || '{}') || {}; }
  catch (_) { return {}; }
}

// Отметить, что шаблон вставили: от этого зависит порядок чипов.
function tplNoteUse(id) {
  if (!id) return;
  const u = _tplUses();
  u[id] = (Number(u[id]) || 0) + 1;
  try { localStorage.setItem(_TPL_USE_KEY, JSON.stringify(u)); } catch (_) {}
}

// Шаблоны в порядке «чаще вставляемые — первыми»; при равенстве — как пришли
// с сервера (свежие первыми). Исходный массив не меняется.
function tplByUse(list, uses) {
  const u = uses || _tplUses();
  return (list || []).map(function (t, i) { return {t: t, i: i, n: Number(u[t && t.id]) || 0}; })
    .sort(function (a, b) { return (b.n - a.n) || (a.i - b.i); })
    .map(function (x) { return x.t; });
}

// Подставить человека в шаблон и раскрыть варианты {а|б}.
// person: {name, username}; name — как в Telegram, «Имя Фамилия».
// Если имени нет — берём ник, если нет и ника — fallback (или пусто).
// Скобки без «|» — не варианты ({10%}), их не трогаем.
function tplFill(text, person, fallback) {
  const p = person || {};
  const full = String(p.name || '').trim();
  const parts = full.split(/\s+/).filter(Boolean);
  const first = parts[0] || '';
  const last = parts.slice(1).join(' ');
  const user = String(p.username || '').replace(/^@/, '');
  const nick = user ? '@' + user : '';
  const fb = fallback == null ? '' : String(fallback);
  let s = String(text || '')
    .replace(/\{name\}/g, first || nick || fb)
    .replace(/\{first_name\}/g, first || fb)
    .replace(/\{last_name\}/g, last)
    .replace(/\{username\}/g, nick || first || fb);
  let guard = 0;
  while (/\{[^{}]*\|[^{}]*\}/.test(s) && guard++ < 50) {
    s = s.replace(/\{([^{}]*\|[^{}]*)\}/g, function (m, g) {
      const v = g.split('|'); return v[Math.floor(Math.random() * v.length)];
    });
  }
  return s;
}

// Есть ли в тексте подстановки и варианты — для значков в списке шаблонов.
function tplTraits(text) {
  const s = String(text || '');
  return {
    personal: /\{(name|first_name|last_name|username)\}/.test(s),
    variants: /\{[^{}]*\|[^{}]*\}/.test(s),
  };
}

// Вставить кусок текста в поле там, где курсор (или заменить выделение).
function tplInsertAtCursor(el, piece) {
  if (!el) return;
  const v = el.value || '';
  const a = (typeof el.selectionStart === 'number') ? el.selectionStart : v.length;
  const b = (typeof el.selectionEnd === 'number') ? el.selectionEnd : a;
  el.value = v.slice(0, a) + piece + v.slice(b);
  const pos = a + piece.length;
  try { el.focus(); el.setSelectionRange(pos, pos); } catch (_) {}
  el.dispatchEvent(new Event('input', {bubbles: true}));
}

// ── Редактор шаблона: кнопки-переменные ─────────────────────────────────────
function tplVarsBarHtml() {
  return TPL_VARS.map(function (v) {
    return '<span class="preset-chip" role="button" tabindex="0" title="Вставить ' + esc(v.token) +
      '" onclick="tplInsertVar(\'' + v.token + '\')">' + esc(v.label) + '</span>';
  }).join('') +
    '<span class="preset-chip" role="button" tabindex="0" title="Варианты: в каждом сообщении — случайный" ' +
    'onclick="tplInsertVariants()">🎲 Варианты</span>';
}
function tplRenderVarsBar() {
  const box = document.getElementById('tplVars');
  if (box && !box.innerHTML) box.innerHTML = tplVarsBarHtml();
}
function tplInsertVar(token) {
  tplInsertAtCursor(document.getElementById('tplText'), token);
}
function tplInsertVariants() {
  tplInsertAtCursor(document.getElementById('tplText'), '{Привет|Здравствуйте}');
}

// ── Чат архива: быстрые ответы над полем ответа ─────────────────────────────
let _vqTpl = null;

async function vaultQuickReplies() {
  const box = document.getElementById('vchatQuick');
  if (!box) return;
  if (!VAULT_CAN_REPLY || VAULT_READ_ONLY) { box.style.display = 'none'; return; }
  box.style.display = '';
  if (!_vqTpl) {
    try { _vqTpl = (await api('/api/miniapp/templates?limit=100')).templates || []; }
    catch (_) { _vqTpl = []; }
  }
  const all = _vqTpl = tplByUse(_vqTpl);
  const head = all.slice(0, 6);
  box.innerHTML = head.map(function (t, k) {
    return '<span class="preset-chip" role="button" tabindex="0" onclick="vaultUseTpl(' + k + ')">⚡ ' +
      esc(t.name || 'Шаблон') + '</span>';
  }).join('') +
    (all.length > head.length ? '<span class="preset-chip" role="button" tabindex="0" onclick="vaultPickTpl()">📝 Все · ' + all.length + '</span>' : '') +
    '<span class="preset-chip" role="button" tabindex="0" onclick="vaultSaveTpl()">💾 В шаблоны</span>';
}

// Кто собеседник открытого чата: имя есть в списке диалогов, ник — только в
// карточке диалога (/info). Карточку тянем, лишь когда шаблону нужен ник.
async function _vaultPerson(needUsername) {
  const c = (typeof _vaultChatById === 'function' && _vaultChatById(VAULT_CHAT)) || {};
  let info = (VAULT_INFO && VAULT_INFO.chat_id === VAULT_CHAT) ? VAULT_INFO : null;
  if (needUsername && !info) {
    try { info = await api(vaultPath('/api/miniapp/vault/chat/' + VAULT_CHAT + '/info')); }
    catch (_) { info = null; }
  }
  return {
    name: (info && info.peer_name) || c.peer_name || VAULT_NAMES[VAULT_CHAT] || '',
    username: (info && info.peer_username) || '',
  };
}

async function vaultUseTpl(k) {
  const t = _vqTpl && _vqTpl[k];
  const inp = document.getElementById('vchatInput');
  if (!t || !inp) return;
  const raw = (t.template && t.template.text) || '';
  const text = tplFill(raw, await _vaultPerson(/\{username\}/.test(raw)));
  // Пустое поле — шаблон и есть ответ; начатый ответ не стираем, а дописываем
  // туда, где курсор.
  if ((inp.value || '').trim()) tplInsertAtCursor(inp, text);
  else inp.value = text;
  autoGrowInput(inp);
  inp.focus();
  tplNoteUse(t.id);
}
async function vaultPickTpl() {
  const k = await tplPickFrom(_vqTpl);
  if (k >= 0) vaultUseTpl(k);
}
async function vaultSaveTpl() {
  if (await tplSaveFrom(document.getElementById('vchatInput'))) { _vqTpl = null; vaultQuickReplies(); }
}

// Общие для «Входящих» и чата архива: выбрать шаблон из всего списка (индекс
// или -1) и сохранить написанный ответ как шаблон (true — сохранён).
async function tplPickFrom(list) {
  const id = await askChoice('⚡ Быстрый ответ', (list || []).map(function (t) {
    return {value: t.id, label: t.name || 'Шаблон', sub: String((t.template && t.template.text) || '').slice(0, 80)};
  }));
  return (list || []).findIndex(function (t) { return t.id === id; });
}
async function tplSaveFrom(el) {
  const text = (el && el.value || '').trim();
  if (!text) { toast('Сначала напишите ответ'); return false; }
  const name = ((await askPrompt('Название быстрого ответа', {value: text.slice(0, 40)})) || '').trim();
  if (!name) return false;
  try {
    await api('/api/miniapp/template', {method: 'POST', body: JSON.stringify({name: name, text: text})});
    toast('💾 Сохранено в шаблоны');
    return true;
  } catch (e) { toast('⚠️ ' + errRu(e, 'Не удалось сохранить')); return false; }
}

// Шаблоны поменяли на экране «Шаблоны» — чипы в чате перечитают их сами.
function tplCachesReset() { _vqTpl = null; if (typeof _ibTpl !== 'undefined') _ibTpl = null; }
