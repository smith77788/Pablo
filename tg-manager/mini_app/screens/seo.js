// ── Экран "SEO-оптимизация" (screens/seo.js) ──────────────────────────────
// Вынесено из index.html как логически обособленный экран (см.
// docs/CLAUDE.md → «Известный технический долг»). Поведение не менялось —
// код перенесён 1:1 из основного inline-скрипта.
//
// Зависит от глобальных хелперов, определённых в основном <script> блоке
// index.html (push, txt, esc, api, empty) — этот файл подключается через
// <script src> ПОСЛЕ основного inline-скрипта index.html, поэтому эти
// функции уже существуют в глобальной области видимости на момент вызова.
'use strict';

async function openSeo() {
  push('s-seo');
  txt('seoKeywords','<div class="spin-wrap"><div class="spin"></div></div>');
  txt('seoSuggestions','');
  try {
    const d = await api('/api/miniapp/seo');
    const kws = d.keywords||[];
    txt('seoKeywords', kws.length ? kws.map(k=>`
      <div class="row">
        <div class="row-ico" style="background:var(--bg-green-16)">🔍</div>
        <div class="row-body"><div class="row-name">${esc(k.keyword)}</div></div>
        <div class="row-right" style="color:var(--hint)">${k.search_count} поисков</div>
      </div>
    `).join('') : empty('🔍','Нет ключевых слов','Добавьте в разделе Позиции'));
    const sug = d.suggestions||[];
    txt('seoSuggestions', sug.length ? sug.map(s=>`
      <div class="row">
        <div class="row-ico" style="background:var(--bg-purple-16)">✨</div>
        <div class="row-body">
          <div class="row-name">${esc(s.channel_title||s.channel_username||s.chan_id)}</div>
          <div class="row-val">${s.title?'Название: '+esc(s.title.slice(0,40)):''} ${s.username?'@'+esc(s.username):''}</div>
        </div>
      </div>
    `).join('') : empty('✨','Нет SEO-предложений','Запустите SEO-анализ в боте'));
  } catch(e) {
    txt('seoKeywords', empty('⚠️','Ошибка',e.message));
  }
}
