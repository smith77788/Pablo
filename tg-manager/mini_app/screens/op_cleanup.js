// Уборка по результатам диагностики: выбор → серверный план → применение.
async function openOperationCleanup(opId) {
  const path = '/api/miniapp/operations/' + encodeURIComponent(opId) + '/cleanup';
  const overlay = document.createElement('div');
  overlay.className = 'mbg show';
  overlay.style.zIndex = '9999';
  overlay.innerHTML = '<div class="modal" role="dialog" aria-modal="true" aria-label="Уборка по итогам проверки">'
    + '<div class="modal-title">Уборка по итогам проверки</div>'
    + '<div data-cleanup-body></div><div data-cleanup-notice role="status" class="field-note"></div>'
    + '<button type="button" class="btn btn-s" style="width:100%;margin-top:12px" data-cleanup-close>Закрыть</button></div>';
  const body = overlay.querySelector('[data-cleanup-body]');
  const notice = overlay.querySelector('[data-cleanup-notice]');
  const close = overlay.querySelector('[data-cleanup-close]');
  const previousFocus = document.activeElement;
  close.onclick = () => {
    overlay.remove();
    previousFocus?.focus();
  };
  overlay.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !close.disabled) close.click();
    if (event.key !== 'Tab') return;
    const controls = [...overlay.querySelectorAll('button,select,input,summary')].filter(
      control => !control.disabled && !control.closest('[hidden]'));
    const edge = event.shiftKey ? controls[0] : controls.at(-1);
    if (!controls.includes(document.activeElement) || document.activeElement === edge) {
      event.preventDefault();
      (event.shiftKey ? controls.at(-1) : controls[0])?.focus();
    }
  });
  document.body.appendChild(overlay);
  close.focus();

  const labels = {channel:'Канал', group:'Группа', bot:'Бот', account:'Аккаунт'};
  const listStyle = 'max-height:42vh;overflow-y:auto;overscroll-behavior:contain';
  const defaultTargetLimit = 1000;
  const actionText = {detach:'Убрать из проекта', disable:'Отключить в Infragram'};
  const title = item => item.title || (labels[item.kind] || 'Ресурс') + ' #' + item.id;
  const row = (item, text) => `<div class="row"><div class="row-body">
    <div class="row-name" style="overflow-wrap:anywhere">${esc(title(item))}</div>
    <div class="row-val" style="white-space:normal">${esc(text)}</div></div></div>`;
  const statusText = {unreachable:'Недоступен снаружи', restricted:'Ограничен Telegram',
    hidden:'Скрыт из глобального поиска', check_failed:'Не удалось проверить'};
  const outcomeText = {detached:'Убран из проекта', disabled:'Отключён', applied:'Применено', changed:'Применено',
    unchanged:'Без изменений', skipped:'Пропущено', failed:'Ошибка', stale:'Состояние изменилось', not_found:'Не найден'};
  let findings = [];

  async function load() {
    notice.textContent = '';
    body.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
    try {
      const result = await api(path);
      if (!overlay.isConnected) return;
      findings = Array.isArray(result.findings) ? result.findings : [];
      const maxTargets = Number(result.max_targets) || defaultTargetLimit;
      const history = Array.isArray(result.history) ? result.history : [];
      const renderHistory = () => {
        if (!history.length) return;
        const block = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'История изменений' + (result.history_has_more ? ' · последние 50' : '');
        block.append(summary);
        const list = document.createElement('ul');
        list.style.cssText = listStyle;
        for (const entry of history) {
          const line = document.createElement('li');
          const time = entry.occurred_at ? new Date(entry.occurred_at).toLocaleString('ru-RU') : '';
          line.textContent = [actionText[entry.action], entry.target, time].filter(Boolean).join(' · ');
          list.append(line);
        }
        block.append(list);
        body.append(block);
      };
      const projects = new Map();
      findings.forEach(item => (item.projects || []).forEach(project => {
        projects.set(String(project.id), project.name);
      }));
      const legacy = result.legacy
        ? '<div class="field-note">Проверка сохранена в старом формате. Список может быть неполным. Запустите новую проверку для актуального списка.</div>'
        : '';
      if (!findings.length) {
        body.innerHTML = legacy + '<div class="field-note">В сохранённом результате нет ресурсов для уборки.</div>';
        renderHistory();
        return;
      }
      body.innerHTML = legacy
        + '<div class="field-note">Выберите ресурсы и проверьте план. Действия выполняются в Infragram.</div>'
        + '<div class="field"><label for="opCleanupAction">Действие</label><select class="sel" id="opCleanupAction" data-cleanup-action>'
        + `<option value="detach">${actionText.detach}</option><option value="disable">${actionText.disable}</option></select></div>`
        + '<div class="field" data-cleanup-project-field><label for="opCleanupProject">Проект</label>'
        + '<select class="sel" id="opCleanupProject" data-cleanup-project>'
        + '<option value="">Выберите проект</option>'
        + [...projects].map(([id, name]) => `<option value="${esc(id)}">${esc(name || '#' + id)}</option>`).join('')
        + '</select></div><div class="field"><label for="opCleanupStatus">Результат проверки</label>'
        + '<select class="sel" id="opCleanupStatus"><option value="">Все результаты</option>'
        + Object.entries(statusText).map(([key, label]) => `<option value="${key}">${label}</option>`).join('')
        + '</select></div><div style="display:flex;gap:8px;margin-bottom:8px">'
        + '<button type="button" class="btn btn-s" data-cleanup-select-all>Выбрать доступные</button>'
        + '<button type="button" class="btn btn-s" data-cleanup-clear>Снять выбор</button></div>'
        + `<div class="lst" data-cleanup-findings style="${listStyle}"></div>`
        + '<button type="button" class="btn btn-p" data-cleanup-preview style="width:100%;margin-top:12px">Проверить план</button>';
      const action = body.querySelector('[data-cleanup-action]');
      const project = body.querySelector('[data-cleanup-project]');
      const projectField = body.querySelector('[data-cleanup-project-field]');
      const list = body.querySelector('[data-cleanup-findings]');
      const status = body.querySelector('#opCleanupStatus');
      const button = body.querySelector('[data-cleanup-preview]');
      const selectAll = body.querySelector('[data-cleanup-select-all]');
      const clear = body.querySelector('[data-cleanup-clear]');
      if (!projects.size) {
        action.value = 'disable';
        action.options[0].disabled = true;
      }
      const selected = new Set();
      const refreshSelection = () => {
        const count = selected.size;
        const visible = list.querySelectorAll('input:checked').length;
        const remaining = maxTargets - count;
        button.disabled = !count;
        selectAll.disabled = remaining <= 0 || !list.querySelector('input:not(:disabled):not(:checked)');
        clear.disabled = !count;
        const hidden = count - visible;
        notice.textContent = count ? 'Выбрано: ' + count + (hidden ? ' · вне фильтра: ' + hidden : '') : '';
        if (!remaining) notice.textContent += ' · достигнут предел ' + maxTargets;
      };
      const renderFindings = () => {
        projectField.hidden = action.value !== 'detach';
        list.innerHTML = findings.map((item, index) => {
          if (status.value && status.value !== item.status) return '';
          const eligible = action.value === 'detach'
            ? project.value && (item.projects || []).some(p => String(p.id) === project.value)
            : item.can_disable === true;
          const details = [
            labels[item.kind] || 'Ресурс',
            statusText[item.status] || 'Требуется проверка',
            item.reason,
            action.value === 'detach' ? (item.projects || []).map(p => p.name).filter(Boolean).join(', ') : '',
            action.value === 'disable' && !eligible ? item.disable_reason : '',
          ].filter(Boolean).join(' · ');
          return `<label class="row chk-row" style="cursor:${eligible ? 'pointer' : 'default'}">
            <input type="checkbox" data-cleanup-target value="${esc(index)}" ${eligible ? '' : 'disabled'} ${selected.has(index) ? 'checked' : ''}>
            <span class="row-body"><span class="row-name" style="display:block;overflow-wrap:anywhere">${esc(title(item))}</span>
            <span class="row-val" style="display:block;white-space:normal">${esc(details)}</span></span></label>`;
        }).join('');
        if (!list.childElementCount) list.textContent = 'В этом срезе нет ресурсов.';
        refreshSelection();
      };
      selectAll.onclick = () => {
        for (const input of list.querySelectorAll('input:not(:disabled):not(:checked)')) {
          if (selected.size >= maxTargets) break;
          selected.add(Number(input.value));
          input.checked = true;
        }
        refreshSelection();
      };
      clear.onclick = () => {
        selected.clear();
        renderFindings();
      };
      list.onchange = event => {
        const input = event.target;
        if (!input.matches('[data-cleanup-target]') || input.disabled) return;
        const index = Number(input.value);
        if (!input.checked) selected.delete(index);
        else if (selected.size < maxTargets) selected.add(index);
        else input.checked = false;
        refreshSelection();
      };
      const resetScope = () => {
        selected.clear();
        renderFindings();
      };
      action.onchange = resetScope;
      project.onchange = resetScope;
      status.onchange = renderFindings;
      if (projects.size === 1) project.selectedIndex = 1;
      button.onclick = async () => {
        const targets = [...selected].map(index => {
          const {kind, id} = findings[index];
          return {kind, id: String(id)};
        });
        if (!targets.length || targets.length > maxTargets) return;
        const payload = {action:action.value, targets};
        if (action.value === 'detach') payload.project_id = project.value;
        body.querySelectorAll('input,select,button').forEach(input => { input.disabled = true; });
        notice.textContent = 'Проверяем текущее состояние ресурсов…';
        try {
          const preview = await api(path, {method:'POST', body:JSON.stringify({...payload, preview:true})});
          if (overlay.isConnected) renderPreview(payload, preview);
        } catch (e) {
          if (overlay.isConnected) {
            await load();
            notice.textContent = errRu(e, 'Не удалось подготовить план');
          }
        }
      };
      renderFindings();
      renderHistory();
    } catch (e) {
      body.innerHTML = '<button type="button" class="btn btn-s" style="width:100%">Повторить загрузку</button>';
      body.querySelector('button').onclick = load;
      notice.textContent = errRu(e, 'Не удалось загрузить результат проверки');
    }
  }

  function renderPreview(payload, preview) {
    const items = Array.isArray(preview.items) ? preview.items : [];
    const readyCount = items.filter(item => item.outcome === 'ready').length;
    notice.textContent = '';
    const project = items.flatMap(item => item.projects || []).find(p => String(p.id) === payload.project_id);
    const scope = project ? ' · ' + (project.name || '#' + project.id) : '';
    body.innerHTML = `<div class="field-note">${esc(actionText[payload.action] + scope)} · ${esc(readyCount)} к применению</div>`
      + (items.length ? `<div class="lst" style="${listStyle}">` + items.map(item => {
        const planned = payload.action === 'detach'
          ? 'Отвязать от проекта · связей: ' + (item.edge_ids || []).length : actionText.disable;
        return row(item, item.outcome === 'skipped' ? item.reason || 'Пропущено' : planned);
      }).join('') + '</div>' : '<div class="field-note">Сейчас нет ресурсов для этого действия.</div>')
      + (readyCount && preview.token ? '<button type="button" class="btn btn-p" data-cleanup-apply style="width:100%;margin-top:12px">Подтвердить</button>' : '')
      + '<button type="button" class="btn btn-s" data-cleanup-reload style="width:100%;margin-top:8px">Вернуться к выбору</button>';
    const reload = body.querySelector('[data-cleanup-reload]');
    reload.onclick = load;
    const apply = body.querySelector('[data-cleanup-apply]');
    (apply || reload).focus();
    if (!apply) return;
    apply.onclick = async () => {
      apply.disabled = true;
      reload.disabled = true;
      close.disabled = true;
      notice.textContent = 'Применяем подтверждённый план…';
      try {
        const result = await api(path, {method:'POST', body:JSON.stringify({...payload, token:preview.token})});
        const applied = Array.isArray(result.items) ? result.items : [];
        body.innerHTML = '<div class="field-note">Результат</div>'
          + (applied.length ? `<div class="lst" style="${listStyle}">` + applied.map(item => row(item,
            [outcomeText[item.outcome] || 'Нет подтверждения изменения', item.outcome === 'applied' ? '' : item.reason].filter(Boolean).join(' · '))).join('') + '</div>'
            : '<div class="field-note">Сервер не вернул результаты по ресурсам.</div>')
          + '<button type="button" class="btn btn-s" style="width:100%;margin-top:12px">Обновить список</button>';
        body.querySelector('button').onclick = load;
        body.querySelector('button').focus();
        notice.textContent = '';
      } catch (e) {
        apply.remove();
        reload.disabled = false;
        reload.focus();
        notice.textContent = errRu(e, 'Не удалось подтвердить результат') + '. Обновите список перед новой попыткой.';
      } finally {
        close.disabled = false;
      }
    };
  }

  await load();
}
