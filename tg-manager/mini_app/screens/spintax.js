// ── Экран "Spintax" (screens/spintax.js) ──────────────────────────────────
// Вынесено из index.html как логически обособленный экран (см.
// docs/CLAUDE.md → «Известный технический долг»). Поведение не менялось —
// код перенесён 1:1 из основного inline-скрипта.
//
// Зависит от глобальных хелперов, определённых в основном <script> блоке
// index.html (push, txt, esc, api, toast) — этот файл подключается через
// <script src> ПОСЛЕ основного inline-скрипта index.html, поэтому эти
// функции уже существуют в глобальной области видимости на момент вызова.
// Все функции ниже вызываются только по клику пользователя (onclick),
// то есть уже после полной загрузки страницы — race condition невозможен.
'use strict';

let SPIN_TEMPLATES = [];

function openSpintax() {
  push('s-spintax');
  SPIN_TEMPLATES = [];
  const t = document.getElementById('spinText'); if (t) t.value='';
  txt('spinResult',''); txt('spinErr','');
  document.getElementById('spinRerollBtn').style.display='none';
}
function _spinRender(variants) {
  SPIN_TEMPLATES = variants.map(v=>v.template);
  const reroll = document.getElementById('spinRerollBtn');
  if (!variants.length) {
    // Пустой ответ оставлял белый экран без единого слова: движок мог
    // отбраковать всё, что вернула модель (слишком мало вариантов в группах,
    // чужие буквы, потерянный исходный смысл), а выглядело это как поломка.
    txt('spinResult', empty('🎲','Вариантов не вышло',
      'Движок отбраковал всё, что предложила модель: обычно так бывает с очень коротким текстом или текстом без синонимируемых слов. Попробуйте текст подлиннее или нажмите «Сгенерировать» ещё раз.'));
    if (reroll) reroll.style.display = 'none';
    return;
  }
  txt('spinResult', variants.map((v,i)=>{
    const warn = (v.warnings&&v.warnings.length)
      ? `<div style="font-size:11px;color:var(--orange);margin-top:4px">⚠️ ${esc(v.warnings.join('; '))}</div>` : '';
    // Сколько разных сообщений даёт шаблон — то, ради чего спинтакс и нужен:
    // одинаковый текст, разосланный сотне людей, Telegram ловит как спам.
    // Экран этого не показывал, и шаблон с двумя группами по два слова
    // выглядел так же солидно, как шаблон на тысячу комбинаций.
    const c = v.combos;
    const weak = c != null && c < 50;
    const combo = c == null ? '' :
      `<div style="font-size:12px;color:${weak?'var(--orange)':'var(--hint)'};margin-top:2px">${
        weak ? '⚠️ ' : ''}Даёт ${v.combos_capped?'более миллиарда':num(c)+' '+plural(c,'разное сообщение','разных сообщения','разных сообщений')}${
        v.groups?' · групп синонимов '+num(v.groups):''}${
        weak ? '. Для большой рассылки маловато — Telegram ловит повторы.' : ''}</div>`;
    return `<div class="row" style="flex-direction:column;align-items:stretch;gap:6px">
      <div style="font-weight:600">${i+1}. ${esc(v.sample||'')}</div>
      ${combo}
      <div onclick="copySpinTpl(${i})" role="button" tabindex="0"
           style="font-family:monospace;font-size:12px;background:var(--bg-input);border-radius:8px;padding:8px;white-space:pre-wrap;word-break:break-word;cursor:pointer">${esc(v.template)}</div>
      <div style="font-size:11px;color:var(--hint)">Нажмите на шаблон, чтобы скопировать его</div>
      ${warn}
    </div>`;
  }).join('') + `<button class="btn btn-s" style="width:100%;margin-top:8px" onclick="copyAllSpinTpl()">📋 Скопировать все ${num(variants.length)} ${plural(variants.length,'шаблон','шаблона','шаблонов')}</button>`);
  if (reroll) reroll.style.display = 'block';
}
function copySpinTpl(i) {
  const tpl = SPIN_TEMPLATES[i];
  if (tpl==null) return;
  copyToClipboard(tpl, '📋 Шаблон скопирован');
}
// Копировать по одному — это ровно столько переключений между приложениями,
// сколько шаблонов. В рассылку их всё равно вставляют списком.
function copyAllSpinTpl() {
  if (!SPIN_TEMPLATES.length) return;
  copyToClipboard(SPIN_TEMPLATES.join('\n\n'),
    '📋 Скопировано шаблонов: ' + SPIN_TEMPLATES.length);
}
async function submitSpin() {
  const script = (document.getElementById('spinText').value||'').trim();
  txt('spinErr','');
  if (!script) { txt('spinErr','Пришлите текст сценария'); return; }
  const btn = document.getElementById('spinBtn');
  btn.disabled=true; const old=btn.textContent; btn.textContent='🎲 Генерирую…';
  txt('spinResult','<div class="spin-wrap"><div class="spin"></div></div>');
  document.getElementById('spinRerollBtn').style.display='none';
  try {
    const d = await api('/api/miniapp/spintax/generate',{method:'POST',body:JSON.stringify({script})});
    _spinRender(d.variants||[]);
  } catch(e) {
    // errRu, а не e.message: сетевые и серверные сбои приходят английским
    // текстом, а владелец по-английски не читает.
    txt('spinResult',''); txt('spinErr', errRu(e,'Не удалось сгенерировать'));
  } finally { btn.disabled=false; btn.textContent=old; }
}
async function rerollSpin() {
  if (!SPIN_TEMPLATES.length) return;
  const btn = document.getElementById('spinRerollBtn');
  btn.disabled=true; const old=btn.textContent; btn.textContent='🔁 …';
  try {
    const d = await api('/api/miniapp/spintax/expand',{method:'POST',body:JSON.stringify({templates:SPIN_TEMPLATES})});
    _spinRender(d.variants||[]);
  } catch(e) { toast('⚠️ '+errRu(e,'Не удалось пересобрать')); }
  finally { btn.disabled=false; btn.textContent=old; }
}
