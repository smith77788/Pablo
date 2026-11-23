// Знакомые персоны — люди, с которыми она уже говорила (persona_peers).
// Рисуется внутри панели памяти (openPersonaMemory в index.html): что персона
// помнит о каждом человеке, последние реплики, правка заметки и «забыть».
// Классический скрипт: делит глобальную область с index.html (esc/api/toast).

function personaPeersHtml(pid, d) {
  const peers = d.peers || [];
  const total = d.peers_total || 0;
  const rows = peers.map(p => {
    const lines = (p.lines || []).map(l => `<div style="font-size:12px;color:var(--text-2)">${esc(l)}</div>`).join('');
    const key = [p.name, p.where, p.notes, p.peer_id].map(x => String(x || '')).join(' ').toLowerCase();
    return `<div class="row pm-peer" data-q="${esc(key)}" style="display:block">
      <div class="row-val">👤 ${esc(p.name || ('id ' + p.peer_id))} · ${num(p.exchanges || 0)} ${plural(p.exchanges || 0, 'обмен', 'обмена', 'обменов')}${p.where ? ' · ' + esc(p.where) : ''}${p.at ? ' · ' + esc(ago(p.at)) : ''}</div>
      <textarea id="pmPeer${Number(p.peer_id)}" rows="2" maxlength="600" placeholder="Заметка появится после нескольких обменов" style="width:100%;margin:6px 0;padding:8px;border-radius:var(--radius);border:1.5px solid var(--border-input);background:var(--bg-input);color:var(--text);font-size:13px;resize:vertical">${esc(p.notes || '')}</textarea>
      ${lines}
      <div style="display:flex;gap:6px;margin-top:6px">
        <button class="btn btn-s" style="flex:1;font-size:12px;padding:6px" onclick="savePeerNotes(${Number(pid)}, ${Number(p.peer_id)}, this)">💾 Сохранить заметку</button>
        <button class="btn btn-d" style="flex:1;font-size:12px;padding:6px" onclick="forgetPeer(${Number(pid)}, ${Number(p.peer_id)})">🧽 Забыть человека</button>
      </div></div>`;
  }).join('');
  return `<div class="sec" style="padding:10px 0 6px">🤝 Знакомые: ${num(total)}</div>
    <div class="field-note">Люди, которым персона уже отвечала. При новой встрече она их узнаёт, а черновик ответа в личке знает, о чём с человеком уже говорили.</div>
    ${peers.length > 5 ? `<input type="search" placeholder="🔎 Найти по имени, чату или заметке" oninput="ppFilter(this)" style="width:100%;margin:6px 0;padding:8px 10px;border-radius:var(--radius);border:1.5px solid var(--border-input);background:var(--bg-input);color:var(--text);font-size:13px"><div class="field-note pp-none" style="display:none">Никого не нашлось</div>` : ''}
    <div class="lst">${rows || empty('🤝', 'Пока никого', 'Знакомые появятся, когда персона ответит людям в чатах прогрева')}</div>`;
}

// Знакомых бывает сотни: найти нужного листанием нельзя.
function ppPeerMatches(key, q) {
  const words = String(q || '').toLowerCase().split(/\s+/).filter(Boolean);
  const k = String(key || '');
  return words.every(w => k.includes(w));
}

function ppFilter(input) {
  const box = input.parentNode;
  let shown = 0;
  box.querySelectorAll('.pm-peer').forEach(r => {
    const on = ppPeerMatches(r.getAttribute('data-q'), input.value);
    r.style.display = on ? 'block' : 'none';
    if (on) shown++;
  });
  const none = box.querySelector('.pp-none');
  if (none) none.style.display = shown ? 'none' : 'block';
}

async function savePeerNotes(pid, peer, btn) {
  const notes = ((document.getElementById('pmPeer' + Number(peer)) || {}).value || '').trim();
  if (btn) btn.disabled = true;
  try {
    await api(`/api/miniapp/persona/${Number(pid)}/peer/${Number(peer)}`, {method: 'PUT', body: JSON.stringify({notes})});
    toast('💾 Заметка сохранена');
  } catch (e) { toast('⚠️ ' + errRu(e,'Ошибка')); }
  finally { if (btn) btn.disabled = false; }
}

async function forgetPeer(pid, peer) {
  if (!await askConfirm('Забыть этого человека? При следующей встрече персона его не узнает.')) return;
  try {
    await api(`/api/miniapp/persona/${Number(pid)}/peer/${Number(peer)}`, {method: 'DELETE'});
    toast('🧽 Забыто');
    openPersonaMemory(pid);
  } catch (e) { toast('⚠️ ' + errRu(e,'Ошибка')); }
}
