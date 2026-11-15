// Уборка по результатам диагностики: выбор → серверный план → применение.
function cleanupFindingMatches(item, verdict, query) {
  if (verdict && verdict !== item.status) return false;
  if (!query) return true;
  if (/^#?-?\d+$/.test(query)) return String(item.id) === query.replace(/^#/, '');
  return String(item.title || '').toLocaleLowerCase('ru-RU').includes(query);
}

async function openOperationCleanup(opId) {
  const existing = document.querySelector('[data-operation-cleanup]');
  if (existing) { existing.querySelector('[data-cleanup-close]')?.focus(); return; }
  const path = '/api/miniapp/operations/' + encodeURIComponent(opId) + '/cleanup';
  const overlay = document.createElement('div');
  overlay.dataset.operationCleanup = '';
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
      control => !control.disabled && control.getClientRects().length);
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
  const pageSize = 40;
  const actionText = {detach:'Убрать из проекта', disable:'Отключить в Infragram'};
  const title = item => item.title || (labels[item.kind] || 'Ресурс') + ' #' + item.id;
  const row = (item, text, recovery) => `<div class="row"><div class="row-body">
    <div class="row-name" style="overflow-wrap:anywhere">${esc(title(item))}</div>
    <div class="row-val" style="white-space:normal">ID: ${esc(item.id)} · ${esc(text)}</div>
    ${recovery}</div></div>`;
  const statusText = {unreachable:'Недоступен снаружи', restricted:'Ограничен Telegram',
    hidden:'Скрыт из глобального поиска', check_failed:'Не удалось проверить'};
  const outcomeText = {detached:'Убран из проекта', disabled:'Отключён', applied:'Применено', changed:'Применено',
    unchanged:'Без изменений', skipped:'Пропущено', failed:'Ошибка', stale:'Состояние изменилось', not_found:'Не найден'};
  let findings = [];

  function appendRecovery(container, items) {
    const bots = items.filter(item => item.kind === 'bot');
    const channels = items.filter(item => item.kind === 'channel' || item.kind === 'group');
    if (channels.length) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-s';
      button.textContent = 'Открыть список восстановления каналов и групп';
      button.onclick = () => { close.click(); openRemovedChannels(); };
      container.append(button);
    }
    if (channels.length === 1) {
      const [item] = channels;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-s';
      button.textContent = 'Восстановление · ' + title(item);
      button.onclick = () => { close.click(); openRemovedChannels(String(item.id)); };
      container.append(button);
    } else if (channels.length > 1) {
      const select = document.createElement('select');
      select.className = 'sel';
      select.setAttribute('aria-label', 'Ресурс для восстановления');
      select.add(new Option('Выберите ресурс для восстановления', ''));
      for (const item of channels) {
        select.add(new Option(title(item) + ' · ID: ' + item.id, String(item.id)));
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-s';
      button.textContent = 'Открыть выбранный ресурс';
      button.disabled = true;
      select.onchange = () => { button.disabled = !select.value; };
      button.onclick = () => { if (select.value) { close.click(); openRemovedChannels(select.value); } };
      container.append(select, button);
    }
    for (const item of bots) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-s';
      button.textContent = 'Открыть настройки бота · ' + title(item);
      button.onclick = () => { close.click(); openBot(String(item.id)); };
      container.append(button);
    }
  }

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
          const label = entry.action === 'restore' ? 'Восстановлен в Infragram' : actionText[entry.action];
          line.textContent = [label, entry.target, time].filter(Boolean).join(' · ');
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
        + '<div class="field-note" data-cleanup-scope></div>'
        + '<div class="field"><label for="opCleanupAction">Действие</label><select class="sel" id="opCleanupAction" data-cleanup-action>'
        + `<option value="disable">${actionText.disable}</option><option value="detach">${actionText.detach}</option></select></div>`
        + '<div class="field" data-cleanup-project-field><label for="opCleanupProject">Проект</label>'
        + '<select class="sel" id="opCleanupProject" data-cleanup-project>'
        + '<option value="">Выберите проект</option>'
        + [...projects].map(([id, name]) => `<option value="${esc(id)}">${esc(name || '#' + id)}</option>`).join('')
        + '</select></div><div class="field"><label for="opCleanupStatus">Результат проверки</label>'
        + '<select class="sel" id="opCleanupStatus"><option value="">Все результаты</option>'
        + Object.entries(statusText).map(([key, label]) => `<option value="${key}">${label}</option>`).join('')
        + '</select></div><div class="field"><label for="opCleanupSearch">Найти ресурс</label>'
        + '<input type="search" class="inp" id="opCleanupSearch" placeholder="Название или точный ID" autocomplete="off"></div>'
        + '<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">'
        + '<button type="button" class="btn btn-s" data-cleanup-select-all>Выбрать доступные</button>'
        + '<button type="button" class="btn btn-s" data-cleanup-clear>Снять выбор</button>'
        + '<button type="button" class="btn btn-s" data-cleanup-selected aria-pressed="false">Только выбранные</button></div>'
        + `<div class="lst" data-cleanup-findings style="${listStyle}"></div>`
        + '<div data-cleanup-pages role="group" aria-label="Страницы результатов" hidden style="margin-top:8px">'
        + '<button type="button" class="btn btn-s" data-cleanup-prev>Назад</button> '
        + '<span data-cleanup-page role="status"></span> '
        + '<button type="button" class="btn btn-s" data-cleanup-next>Вперёд</button></div>'
        + '<button type="button" class="btn btn-p" data-cleanup-preview style="width:100%;margin-top:12px">Проверить план</button>';
      const action = body.querySelector('[data-cleanup-action]');
      const project = body.querySelector('[data-cleanup-project]');
      const projectField = body.querySelector('[data-cleanup-project-field]');
      const list = body.querySelector('[data-cleanup-findings]');
      const status = body.querySelector('#opCleanupStatus');
      const search = body.querySelector('#opCleanupSearch');
      const pages = body.querySelector('[data-cleanup-pages]');
      const pageLabel = body.querySelector('[data-cleanup-page]');
      const prev = body.querySelector('[data-cleanup-prev]');
      const next = body.querySelector('[data-cleanup-next]');
      const button = body.querySelector('[data-cleanup-preview]');
      const selectAll = body.querySelector('[data-cleanup-select-all]');
      const clear = body.querySelector('[data-cleanup-clear]');
      const selectedOnly = body.querySelector('[data-cleanup-selected]');
      const scope = body.querySelector('[data-cleanup-scope]');
      if (!projects.size) action.querySelector('[value="detach"]').disabled = true;
      const selected = new Set();
      const indexed = findings.map((item, index) => ({item, index}));
      let filtered = indexed;
      let page = 0;
      let showSelected = false;
      const canSelect = item => action.value === 'detach'
        ? Boolean(project.value && (item.projects || []).some(p => String(p.id) === project.value))
        : item.can_disable === true;
      const refreshSelection = () => {
        const count = selected.size;
        const remaining = maxTargets - count;
        button.disabled = !count;
        selectAll.disabled = remaining <= 0 || !filtered.some(
          ({item, index}) => !selected.has(index) && canSelect(item));
        clear.disabled = !count;
        selectedOnly.disabled = !count;
        selectedOnly.setAttribute('aria-pressed', String(showSelected));
        const insideFilter = count ? filtered.filter(({index}) => selected.has(index)).length : 0;
        const hidden = count - insideFilter;
        button.textContent = count ? 'Проверить план · ' + count : 'Проверить план';
        let message = '';
        if (count) message = 'Выбрано: ' + count + (hidden ? ' · вне фильтра: ' + hidden : '');
        else if (action.value === 'detach' && !project.value) message = 'Выберите проект, чтобы отметить его ресурсы.';
        else if (selectAll.disabled) message = 'В этом срезе нет ресурсов, доступных для выбранного действия.';
        notice.textContent = message;
        if (!remaining) notice.textContent += ' · достигнут предел ' + maxTargets;
      };
      const renderFindings = () => {
        projectField.hidden = action.value !== 'detach';
        scope.textContent = action.value === 'detach'
          ? 'Связь исчезнет только из выбранного проекта.'
          : 'Ресурс перестанет использоваться в Infragram. Управление можно восстановить.';
        const query = search.value.trim().toLocaleLowerCase('ru-RU');
        filtered = indexed.filter(({item, index}) =>
          showSelected ? selected.has(index) : cleanupFindingMatches(item, status.value, query));
        const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
        page = Math.min(page, pageCount - 1);
        status.disabled = search.disabled = showSelected;
        selectAll.hidden = showSelected;
        pages.hidden = pageCount <= 1;
        selectAll.textContent = pageCount > 1 ? 'Выбрать доступные во всём фильтре' : 'Выбрать доступные';
        pageLabel.textContent = 'Страница ' + (page + 1) + ' из ' + pageCount;
        prev.disabled = page === 0;
        next.disabled = page + 1 >= pageCount;
        list.innerHTML = filtered.slice(page * pageSize, (page + 1) * pageSize).map(({item, index}) => {
          const eligible = canSelect(item);
          const memberships = (item.projects || []).map(p => p.name || '#' + p.id).join(', ');
          const outsideProject = action.value === 'detach' && project.value && !eligible && (item.active || memberships);
          const unavailable = outsideProject ? 'Нет связи с выбранным проектом'
            : action.value === 'disable' || !item.active ? item.disable_reason : '';
          const details = [
            labels[item.kind] || 'Ресурс',
            'ID: ' + item.id,
            statusText[item.status] || 'Требуется проверка',
            item.reason,
            memberships ? 'Проекты: ' + memberships : '',
            unavailable,
            item.restore_reason,
          ].filter(Boolean).join(' · ');
          const resume = item.can_resume === true
            ? `<button type="button" class="btn btn-s" data-cleanup-resume="${esc(index)}"
                aria-label="${esc('Настройки бота · ' + title(item) + ' · ID: ' + item.id)}">Настройки бота</button>`
            : '';
          return `<label class="row chk-row" style="min-height:44px;cursor:${eligible ? 'pointer' : 'default'}">
            <input type="checkbox" data-cleanup-target value="${esc(index)}" ${eligible ? '' : 'disabled'} ${selected.has(index) ? 'checked' : ''}>
            <span class="row-body"><span class="row-name" style="display:block;overflow-wrap:anywhere">${esc(title(item))}</span>
            <span class="row-val" style="display:block;white-space:normal">${esc(details)}</span></span></label>${resume}`;
        }).join('');
        if (!filtered.length) list.textContent = 'В этом срезе нет ресурсов.';
        list.scrollTop = 0;
        refreshSelection();
      };
      selectAll.onclick = () => {
        for (const {item, index} of filtered) {
          if (selected.size >= maxTargets) break;
          if (canSelect(item)) selected.add(index);
        }
        renderFindings();
      };
      clear.onclick = () => {
        selected.clear();
        showSelected = false;
        resetPage();
      };
      selectedOnly.onclick = () => { showSelected = !showSelected; resetPage(); };
      list.onclick = event => {
        const button = event.target.closest('[data-cleanup-resume]');
        const item = button && findings[Number(button.dataset.cleanupResume)];
        if (item?.kind !== 'bot' || item.can_resume !== true) return;
        close.click();
        openBot(String(item.id));
      };
      list.onchange = event => {
        const input = event.target;
        if (!input.matches('[data-cleanup-target]') || input.disabled) return;
        const index = Number(input.value);
        if (!input.checked) selected.delete(index);
        else if (selected.size < maxTargets) selected.add(index);
        else input.checked = false;
        if (showSelected) {
          if (!selected.size) showSelected = false;
          renderFindings();
          if (showSelected) list.querySelector('[data-cleanup-target]')?.focus();
          else (selectAll.disabled ? action : selectAll).focus();
        } else refreshSelection();
      };
      const resetPage = () => {
        page = 0;
        renderFindings();
      };
      const resetScope = () => {
        selected.clear();
        showSelected = false;
        resetPage();
      };
      action.onchange = resetScope;
      project.onchange = resetScope;
      status.onchange = resetPage;
      search.oninput = resetPage;
      prev.onclick = () => { page--; renderFindings(); if (prev.disabled) next.focus(); };
      next.onclick = () => { page++; renderFindings(); if (next.disabled) prev.focus(); };
      if (projects.size === 1) project.selectedIndex = 1;
      button.onclick = async () => {
        if (button.disabled) return;
        const targets = [...selected].map(index => {
          const {kind, id} = findings[index];
          return {kind, id: String(id)};
        });
        if (!targets.length || targets.length > maxTargets) return;
        const payload = {action:action.value, targets};
        if (action.value === 'detach') payload.project_id = project.value;
        const enabled = [...body.querySelectorAll('input,select,button')].filter(input => !input.disabled);
        const saved = [...body.childNodes];
        enabled.forEach(input => { input.disabled = true; });
        const restoreSelection = () => {
          body.replaceChildren(...saved);
          enabled.forEach(input => { input.disabled = false; });
          refreshSelection();
          button.focus();
        };
        notice.textContent = 'Проверяем текущее состояние ресурсов…';
        try {
          const preview = await api(path, {method:'POST', body:JSON.stringify({...payload, preview:true})});
          if (overlay.isConnected) renderPreview(payload, preview, restoreSelection);
        } catch (e) {
          if (overlay.isConnected) {
            restoreSelection();
            notice.textContent = errRu(e, 'Не удалось подготовить план');
          }
        }
      };
      renderFindings();
      renderHistory();
      const disabledChannels = findings.filter(item => item.can_restore === true);
      appendRecovery(body, disabledChannels);
    } catch (e) {
      body.innerHTML = '<button type="button" class="btn btn-s" style="width:100%">Повторить загрузку</button>';
      body.querySelector('button').onclick = load;
      notice.textContent = errRu(e, 'Не удалось загрузить результат проверки');
    }
  }

  function renderReview(items, describe, isSuccessful, {result = false, canRecover = false} = {}) {
    const panel = body.querySelector('[data-cleanup-review]');
    if (!items.length) return;
    const successful = items.filter(isSuccessful).length;
    panel.innerHTML = '<div class="field"><label for="opCleanupOutcome">Показать</label>'
      + '<select class="sel" id="opCleanupOutcome">'
      + `<option value="">Все цели · ${items.length}</option>`
      + `<option value="yes">${result ? 'Изменены' : 'Готовы'} · ${successful}</option>`
      + `<option value="no">${result ? 'Без изменений' : 'Пропущены'} · ${items.length - successful}</option></select></div>`
      + '<div class="field"><label for="opCleanupReviewSearch">Найти в списке</label>'
      + '<input type="search" class="inp" id="opCleanupReviewSearch" placeholder="Название или точный ID" autocomplete="off"></div>'
      + `<div class="lst" data-cleanup-review-list style="${listStyle}"></div>`
      + '<div data-cleanup-review-pages role="group" aria-label="Страницы списка" hidden>'
      + '<button type="button" class="btn btn-s" data-cleanup-review-prev>Назад</button> '
      + '<span data-cleanup-review-page role="status"></span> '
      + '<button type="button" class="btn btn-s" data-cleanup-review-next>Вперёд</button></div>';
    const outcome = panel.querySelector('#opCleanupOutcome');
    const search = panel.querySelector('#opCleanupReviewSearch');
    const list = panel.querySelector('[data-cleanup-review-list]');
    const pages = panel.querySelector('[data-cleanup-review-pages]');
    const pageLabel = panel.querySelector('[data-cleanup-review-page]');
    const previous = panel.querySelector('[data-cleanup-review-prev]');
    const next = panel.querySelector('[data-cleanup-review-next]');
    const indexed = items.map((item, index) => ({item, index}));
    let page = 0;
    const render = () => {
      const query = search.value.trim().toLocaleLowerCase('ru-RU');
      const filtered = indexed.filter(({item}) =>
        (!outcome.value || isSuccessful(item) === (outcome.value === 'yes'))
        && cleanupFindingMatches(item, '', query));
      const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
      pages.hidden = pageCount === 1;
      pageLabel.textContent = 'Страница ' + (page + 1) + ' из ' + pageCount;
      previous.disabled = page === 0;
      next.disabled = page + 1 >= pageCount;
      list.innerHTML = filtered.slice(page * pageSize, (page + 1) * pageSize).map(({item, index}) => {
        const recover = canRecover && isSuccessful(item) && ['bot', 'channel', 'group'].includes(item.kind);
        const label = item.kind === 'bot' ? 'Открыть настройки бота' : 'Открыть восстановление';
        const recovery = recover ? `<button type="button" class="btn btn-s" data-cleanup-recover="${index}"
          aria-label="${esc(label + ' · ' + title(item) + ' · ID: ' + item.id)}">${label}</button>` : '';
        return row(item, describe(item), recovery);
      }).join('');
      if (!filtered.length) list.textContent = 'В этом срезе нет ресурсов.';
      list.scrollTop = 0;
    };
    const reset = () => { page = 0; render(); };
    outcome.onchange = reset;
    search.oninput = reset;
    previous.onclick = () => { page--; render(); if (previous.disabled) next.focus(); };
    next.onclick = () => { page++; render(); if (next.disabled) previous.focus(); };
    list.onclick = event => {
      const button = event.target.closest('[data-cleanup-recover]');
      if (!button || !canRecover) return;
      const item = items[Number(button.dataset.cleanupRecover)];
      if (!item || !isSuccessful(item)) return;
      close.click();
      if (item.kind === 'bot') openBot(String(item.id));
      else if (item.kind === 'channel' || item.kind === 'group') openRemovedChannels(String(item.id));
    };
    render();
  }

  function renderPreview(payload, preview, restoreSelection) {
    const items = Array.isArray(preview.items) ? preview.items : [];
    const readyCount = items.filter(item => item.outcome === 'ready').length;
    notice.textContent = '';
    const project = items.flatMap(item => item.projects || []).find(p => String(p.id) === payload.project_id);
    const scope = payload.project_id ? ' · ' + (project?.name || 'Проект #' + payload.project_id) : '';
    const summary = actionText[payload.action] + scope
      + ' · ' + readyCount + ' к применению · ' + (items.length - readyCount) + ' пропущено';
    body.innerHTML = `<div class="field-note" data-cleanup-summary tabindex="-1">${esc(summary)}</div>`
      + '<div data-cleanup-review></div>'
      + (!items.length ? '<div class="field-note">Сейчас нет ресурсов для этого действия.</div>' : '')
      + (readyCount && preview.token ? `<button type="button" class="btn btn-p" data-cleanup-apply style="width:100%;margin-top:12px">Подтвердить · ${readyCount}</button>` : '')
      + '<button type="button" class="btn btn-s" data-cleanup-reload style="width:100%;margin-top:8px">Вернуться к выбору</button>';
    const reload = body.querySelector('[data-cleanup-reload]');
    reload.onclick = restoreSelection;
    const apply = body.querySelector('[data-cleanup-apply]');
    renderReview(items, item => {
      const planned = payload.action === 'detach'
        ? 'Отвязать от проекта · связей: ' + (item.edge_ids || []).length : actionText.disable;
      return [statusText[item.status], item.finding_reason,
        item.outcome === 'ready' ? planned : item.reason || 'Пропущено'].filter(Boolean).join(' · ');
    }, item => item.outcome === 'ready');
    body.querySelector('[data-cleanup-summary]').focus();
    if (!apply) return;
    apply.onclick = async () => {
      if (apply.disabled) return;
      apply.disabled = true;
      reload.disabled = true;
      close.disabled = true;
      notice.textContent = 'Применяем подтверждённый план…';
      try {
        const result = await api(path, {method:'POST', body:JSON.stringify({...payload, token:preview.token})});
        const applied = Array.isArray(result.items) ? result.items : [];
        body.innerHTML = '<div class="field-note" data-cleanup-summary tabindex="-1">'
          + (result.replayed ? 'Сохранённый результат' : 'Результат') + '</div>'
          + '<div data-cleanup-review></div>'
          + (!applied.length ? '<div class="field-note">Сервер не вернул результаты по ресурсам.</div>' : '')
          + '<button type="button" class="btn btn-s" data-cleanup-refresh style="width:100%;margin-top:12px">Обновить список</button>';
        renderReview(applied, item => [outcomeText[item.outcome] || 'Нет подтверждения изменения',
          item.outcome === 'applied' ? '' : item.reason, statusText[item.status], item.finding_reason].filter(Boolean).join(' · '),
        item => item.outcome === 'applied',
        {result:true, canRecover:payload.action === 'disable' && !result.replayed});
        body.querySelector('[data-cleanup-refresh]').onclick = load;
        body.querySelector('[data-cleanup-summary]').focus();
        notice.textContent = '';
      } catch (e) {
        apply.disabled = false;
        apply.textContent = 'Повторить то же подтверждение';
        reload.disabled = false;
        reload.textContent = 'Обновить список';
        reload.onclick = load;
        reload.focus();
        notice.textContent = errRu(e, 'Не удалось получить результат')
          + '. Можно отправить то же подтверждение или обновить список.';
      } finally {
        close.disabled = false;
      }
    };
  }

  await load();
}
