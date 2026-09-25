// Редактор канала (Virtual Channel Administrator): правила, по которым пост
// проверяется перед публикацией, и совет редактора на подтверждении.
// Совет — не запрет: публиковать можно как есть. Правила общие для всех
// каналов владельца (массовая публикация идёт сразу во все).

let _edPolicy = null;

function _edScreen() {
  let el = document.getElementById('s-editorial');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'screen';
  el.id = 's-editorial';
  el.innerHTML =
    '<div class="hdr">' +
      '<div class="back" onclick="back()">←</div>' +
      '<div class="hdr-title">✍️ Правила редактора</div>' +
    '</div>' +
    '<div class="sub-sb"><div id="s-editorial-body">' +
      '<div class="spin-wrap"><div class="spin"></div></div>' +
    '</div></div>';
  document.body.appendChild(el);
  return el;
}

async function openEditorialRules() {
  _edScreen();
  push('s-editorial');
  const body = document.getElementById('s-editorial-body');
  body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const d = await api('/api/miniapp/editorial/policy');
    _edPolicy = d.policy || {};
    body.innerHTML = _edRender(_edPolicy);
  } catch (e) {
    body.innerHTML = errHtml('Не удалось загрузить правила: ' + ((e && e.message) || ''), 'openEditorialRules()');
  }
}

function _edNum(v) { return (v === null || v === undefined) ? '' : String(v); }

function _edRender(p) {
  const dup = Math.round((Number(p.dup_threshold) || 0.6) * 100);
  const dupOpts = [40, 50, 60, 70, 80, 90].map(function (x) {
    return '<option value="' + x + '"' + (x === dup ? ' selected' : '') + '>' + x + '%' +
      (x === 60 ? ' (по умолчанию)' : '') + '</option>';
  }).join('');
  const maxChars = (p.max_chars && p.max_chars < 4096) ? p.max_chars : '';
  const minChars = p.min_chars ? p.min_chars : '';
  return '' +
    '<div class="sec">Как это работает</div>' +
    '<div class="lst" style="padding:12px 14px;font-size:13px;line-height:1.5;color:var(--hint)">' +
      'Перед массовой публикацией редактор сравнивает пост с вашими недавними постами и проверяет эти правила. ' +
      'Если что-то не так, на подтверждении появится совет. Это подсказка, а не запрет: публиковать можно как есть.' +
      (p.configured ? '' : '<br><br>Сейчас действуют правила по умолчанию: только проверка на повтор.') +
    '</div>' +
    '<div class="sec">Повторы</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div class="field"><label>Считать повтором, если текст совпадает на</label>' +
        '<select id="edDup">' + dupOpts + '</select>' +
        '<div class="field-note">Чем ниже порог, тем строже: при 40% редактор заметит даже пересказ старого поста.</div></div>' +
    '</div>' +
    '<div class="sec">Голос канала</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div style="display:flex;gap:10px">' +
        '<div class="field" style="flex:1"><label>Эмодзи, не больше</label>' +
          '<input type="number" id="edEmoji" min="0" max="100" inputmode="numeric" placeholder="без ограничения" value="' + esc(_edNum(p.max_emoji)) + '"></div>' +
        '<div class="field" style="flex:1"><label>Призывов, не больше</label>' +
          '<input type="number" id="edCta" min="0" max="20" inputmode="numeric" placeholder="без ограничения" value="' + esc(_edNum(p.max_cta)) + '"></div>' +
      '</div>' +
      '<div class="field-note" style="margin:-4px 0 12px">Призыв — «подпишитесь», «переходите по ссылке», «закажите» и похожие фразы.</div>' +
      '<div style="display:flex;gap:10px">' +
        '<div class="field" style="flex:1"><label>Длина от</label>' +
          '<input type="number" id="edMin" min="0" max="4096" inputmode="numeric" placeholder="0" value="' + esc(_edNum(minChars)) + '"></div>' +
        '<div class="field" style="flex:1"><label>Длина до</label>' +
          '<input type="number" id="edMax" min="1" max="4096" inputmode="numeric" placeholder="4096" value="' + esc(_edNum(maxChars)) + '"></div>' +
      '</div>' +
      '<div class="field"><label>Запрещённые слова</label>' +
        '<textarea id="edWords" rows="3" placeholder="через запятую или с новой строки">' + esc((p.forbidden_words || []).join(', ')) + '</textarea>' +
        '<div class="field-note">До 100 слов. Регистр не важен, ищется и внутри слов.</div></div>' +
      '<div class="field"><label>Запрещённые начала поста</label>' +
        '<textarea id="edOpenings" rows="2" placeholder="например: друзья, всем привет">' + esc((p.banned_openings || []).join(', ')) + '</textarea>' +
        '<div class="field-note">Одинаковые вступления — главный признак шаблонного потока постов.</div></div>' +
      '<div class="field-err" id="edErr"></div>' +
      '<button class="btn btn-p" id="edSaveBtn" onclick="saveEditorialRules()" style="width:100%;margin-top:4px">💾 Сохранить правила</button>' +
    '</div>' +
    '<div class="sec">Проверить текст</div>' +
    '<div class="lst" style="padding:14px">' +
      '<div class="field"><textarea id="edTry" rows="4" maxlength="4096" placeholder="Вставьте пост — редактор скажет, что бы он поправил"></textarea></div>' +
      '<button class="btn btn-s" id="edTryBtn" onclick="tryEditorialReview()" style="width:100%">✍️ Проверить</button>' +
      '<div id="edTryOut" style="margin-top:10px;font-size:13px;line-height:1.5"></div>' +
    '</div>';
}

function _edWords(id) {
  return (document.getElementById(id).value || '')
    .split(/[,\n]/).map(function (s) { return s.trim(); }).filter(Boolean);
}

async function saveEditorialRules() {
  const errEl = document.getElementById('edErr');
  const btn = document.getElementById('edSaveBtn');
  errEl.style.display = 'none';
  const v = function (id) { const x = document.getElementById(id).value.trim(); return x === '' ? null : x; };
  const payload = {
    dup_threshold: Number(document.getElementById('edDup').value) / 100,
    max_emoji: v('edEmoji'),
    max_cta: v('edCta'),
    min_chars: v('edMin'),
    max_chars: v('edMax'),
    forbidden_words: _edWords('edWords'),
    banned_openings: _edWords('edOpenings'),
  };
  btn.disabled = true; btn.textContent = '⏳ Сохраняю…';
  try {
    const d = await api('/api/miniapp/editorial/policy', { method: 'PUT', body: JSON.stringify(payload) });
    _edPolicy = d.policy || _edPolicy;
    tg.HapticFeedback?.notificationOccurred('success');
    toast('✅ Правила редактора сохранены');
  } catch (e) {
    errEl.textContent = (e && e.message) || 'Не удалось сохранить';
    errEl.style.display = 'block';
    tg.HapticFeedback?.notificationOccurred('error');
  } finally {
    btn.disabled = false; btn.textContent = '💾 Сохранить правила';
  }
}

// Совет редактора в виде HTML-блока ('' — замечаний нет).
function editorialAdviceHtml(r) {
  if (!r || !r.needs_review || !(r.reasons || []).length) return '';
  return '<div style="padding:10px 12px;border-radius:10px;background:rgba(251,146,60,.10);line-height:1.5">' +
    '<b>✍️ Редактор советует проверить:</b><br>' +
    r.reasons.map(function (x) { return '• ' + esc(x); }).join('<br>') +
    '<br><span style="opacity:.7">Это подсказка — публиковать можно как есть.</span></div>';
}

async function tryEditorialReview() {
  const text = (document.getElementById('edTry').value || '').trim();
  const out = document.getElementById('edTryOut');
  if (!text) { out.innerHTML = '<span style="color:var(--hint)">Вставьте текст поста</span>'; return; }
  const btn = document.getElementById('edTryBtn');
  btn.disabled = true;
  out.innerHTML = '<span style="color:var(--hint)">Проверяю…</span>';
  try {
    const r = await api('/api/miniapp/editorial/review', { method: 'POST', body: JSON.stringify({ text: text }) });
    out.innerHTML = editorialAdviceHtml(r) ||
      '<span style="color:var(--green)">✅ Замечаний нет: не похоже на недавние посты и правила соблюдены.</span>';
  } catch (e) {
    out.innerHTML = errHtml('Не удалось проверить: ' + ((e && e.message) || ''), 'tryEditorialReview()');
  } finally {
    btn.disabled = false;
  }
}

// Совет перед массовой публикацией. Fail-soft: сбой проверки не мешает
// публиковать и не пугает ложной тревогой — возвращаем null.
async function editorialReviewForConfirm(text) {
  try {
    return await api('/api/miniapp/editorial/review', {
      method: 'POST', body: JSON.stringify({ text: text }), timeoutMs: 8000,
    });
  } catch (e) {
    return null;
  }
}
