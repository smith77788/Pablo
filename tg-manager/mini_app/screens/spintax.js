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
  txt('spinResult', variants.map((v,i)=>{
    const warn = (v.warnings&&v.warnings.length)
      ? `<div style="font-size:11px;color:var(--orange);margin-top:4px">⚠️ ${esc(v.warnings.join('; '))}</div>` : '';
    return `<div class="row" style="flex-direction:column;align-items:stretch;gap:6px">
      <div style="font-weight:600">${i+1}. ${esc(v.sample||'')}</div>
      <div onclick="copySpinTpl(${i})"
           style="font-family:monospace;font-size:12px;background:var(--bg-input);border-radius:8px;padding:8px;white-space:pre-wrap;word-break:break-word;cursor:pointer">${esc(v.template)}</div>
      ${warn}
    </div>`;
  }).join(''));
  document.getElementById('spinRerollBtn').style.display = variants.length?'block':'none';
}
function copySpinTpl(i) {
  const tpl = SPIN_TEMPLATES[i];
  if (tpl==null) return;
  copyToClipboard(tpl, '📋 Шаблон скопирован');
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
    txt('spinResult',''); txt('spinErr', e.message);
  } finally { btn.disabled=false; btn.textContent=old; }
}
async function rerollSpin() {
  if (!SPIN_TEMPLATES.length) return;
  const btn = document.getElementById('spinRerollBtn');
  btn.disabled=true; const old=btn.textContent; btn.textContent='🔁 …';
  try {
    const d = await api('/api/miniapp/spintax/expand',{method:'POST',body:JSON.stringify({templates:SPIN_TEMPLATES})});
    _spinRender(d.variants||[]);
  } catch(e) { toast(e.message); }
  finally { btn.disabled=false; btn.textContent=old; }
}
