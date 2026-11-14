'use strict';

// Действия открывают штатные карточки и используют их проверки доступа.
function resourceActionsFor(node, projectId, fromCard = false) {
  const exact = String(node.object_id_text ?? node.object_id ?? '');
  const parsed = Number(exact);
  const id = Number.isSafeInteger(parsed) ? parsed : exact;
  const primary = [];
  const secondary = [];
  if (/^-?[1-9]\d*$/.test(exact) && node.resource_available !== false) {
    const openers = {
      account: () => openAccount(id),
      bot: () => openBot(id),
      channel: () => openChannel(id),
      group: () => openChannel(id),
      chat: () => openChannel(id),
    };
    if (!fromCard && Object.hasOwn(openers, node.type)) {
      primary.push(['Открыть карточку', openers[node.type]]);
    }
    if (node.type === 'channel' && Number.isSafeInteger(projectId) && projectId > 0) {
      primary.push(['Подготовить публикацию', () => openMassPub(projectId, id)]);
    }
    primary.push(['Копировать ID', () => copyText(exact)]);
    if (node.type === 'account') {
      secondary.push(['Диалоги профиля', () => openAccountDialogs(id)]);
      if (node.is_active !== false) secondary.push(['Сеансы аккаунта', () => openAccSessions(id)]);
    }
    if (node.type === 'bot' && node.is_active !== false && node.is_owned !== false) {
      secondary.push(['Команды бота', () => openBotCommands(id)]);
    }
  }
  return {primary, secondary};
}

function projectResourceActions(node, trigger, projectId) {
  const actions = resourceActionsFor(node, projectId);
  actions.secondary.push(['Убрать из проекта', () => removeNetNode(Number(node.id), trigger)]);
  return actions;
}

function openProjectResourceActions(node, trigger, projectId) {
  showResourceActions(node.label || objRu(node.type), projectResourceActions(node, trigger, projectId), trigger);
}

function openNetworkResourceActions(project, trigger) {
  const id = Number(project.id);
  if (!Number.isSafeInteger(id) || id <= 0) return;
  showResourceActions(project.name || 'Проект #' + id, {
    primary: [
      ['Открыть топологию', () => selectNetwork(id)],
      ['Журнал проекта', () => openOps(id)],
    ],
    secondary: [['Копировать ID проекта', () => copyText(String(id))]],
  }, trigger);
}

function resourceMenuButton() {
  return '<button type="button" class="btn btn-s resource-actions-trigger" data-resource-menu aria-label="Действия ресурса" aria-haspopup="dialog">⋯</button>';
}

function catalogResourceId(item, type) {
  if (type === 'account') return String(item.id);
  const key = type === 'bot' ? 'bot_id' : 'channel_id';
  const exact = item[key + '_text'];
  return typeof exact === 'string' ? exact : Number.isSafeInteger(item[key]) ? String(item[key]) : '';
}

function openCatalogResourceActions(item, type, id, trigger, fromCard = false) {
  const kind = type === 'channel' && item.type && item.type !== 'channel' ? 'group' : type;
  const label = item.title || item.first_name || (item.username ? '@' + item.username : objRu(kind));
  const actions = resourceActionsFor({
    type: kind, object_id_text: id, is_active: item.is_active, is_owned: item.is_owned,
  }, null, fromCard);
  if (kind !== 'bot' || item.is_owned !== false) {
    actions.secondary.push(['Добавить в проект', () => addCatalogResourceToProject(kind, id, label)]);
  }
  showResourceActions(label, actions, trigger);
}

function bindCatalogResourceActions(root, type, items) {
  bindResourceActionRows(root, (id, trigger) => {
    const item = items().find(entry => catalogResourceId(entry, type) === id);
    if (item) openCatalogResourceActions(item, type, id, trigger);
  });
}

function prepareResourceCardMenu(buttonId, type, id) {
  const button = document.getElementById(buttonId);
  if (!button) return () => {};
  button.hidden = true;
  button.onclick = null;
  const request = Symbol();
  button.resourceMenuRequest = request;
  return item => {
    if (!item || button.resourceMenuRequest !== request || !/^-?[1-9]\d*$/.test(String(id))) return;
    button.onclick = () => openCatalogResourceActions(item, type, String(id), button, true);
    button.hidden = false;
  };
}

async function addCatalogResourceToProject(type, id, label) {
  try {
    const data = await api('/api/miniapp/networks');
    const projects = data.networks || [];
    if (!projects.length) { toast('Сначала создайте проект'); return; }
    const projectId = await askChoice('Добавить ресурс в проект', projects.map(project => ({
      value: project.id, label: project.name, sub: '#' + project.id,
    })));
    if (projectId == null) return;
    const project = projects.find(item => item.id === projectId);
    if (!project) return;
    const result = await api('/api/miniapp/networks/' + project.id + '/nodes', {
      method: 'POST', body: JSON.stringify({type, object_id: id, label: Array.from(label).slice(0, 200).join('')}),
    });
    toast(result.already_linked ? 'Ресурс уже в проекте' : 'Ресурс добавлен в проект');
  } catch (error) {
    toast(errRu(error, 'Не удалось добавить ресурс в проект'));
  }
}

function showResourceActions(title, actions, trigger) {
  const dialog = document.getElementById('resourceActions');
  if (!dialog || dialog.open || !(actions.primary.length || actions.secondary.length)) return;
  document.getElementById('resourceActionsTitle').textContent = title;
  let restoreFocus = true;
  const more = document.getElementById('resourceActionsMore');
  more.hidden = !actions.secondary.length;
  more.open = !actions.primary.length;
  for (const [id, items] of [
    ['resourceActionsPrimary', actions.primary],
    ['resourceActionsSecondary', actions.secondary],
  ]) {
    const buttons = items.map(([label, run]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-s';
      button.textContent = label;
      button.addEventListener('click', () => {
        restoreFocus = false;
        dialog.close();
        if (trigger?.isConnected) trigger.focus();
        run();
      });
      return button;
    });
    document.getElementById(id).replaceChildren(...buttons);
  }
  dialog.addEventListener('close', () => {
    if (restoreFocus && trigger?.isConnected) trigger.focus();
  }, {once: true});
  dialog.showModal();
}

const resourceActionRowHandlers = new WeakMap();
const catalogResourceMenus = {account: false, bot: false, channel: false};

function bindResourceActionRows(root, open) {
  if (resourceActionRowHandlers.has(root)) {
    resourceActionRowHandlers.set(root, open);
    return;
  }
  resourceActionRowHandlers.set(root, open);
  const openRow = row => resourceActionRowHandlers.get(root)(
    row.dataset.resourceNode, row.querySelector('[data-resource-menu]'));
  let press = null;
  let suppressRow = null;
  function cancelPress() {
    if (press) clearTimeout(press.timer);
    press = null;
  }
  root.addEventListener('click', event => {
    const row = event.target.closest('[data-resource-node]');
    if (!row || !root.contains(row)) return;
    const trigger = event.target.closest('[data-resource-menu]');
    if (trigger || row === suppressRow) {
      event.preventDefault();
      event.stopPropagation();
      suppressRow = null;
      if (trigger) openRow(row);
    }
  }, true);
  root.addEventListener('keydown', event => {
    if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return;
    const row = event.target.closest('[data-resource-node]');
    if (!row || !root.contains(row)) return;
    event.preventDefault();
    openRow(row);
  });
  root.addEventListener('contextmenu', event => {
    const row = event.target.closest('[data-resource-node]');
    if (!row || !root.contains(row)) return;
    event.preventDefault();
    cancelPress();
    openRow(row);
  });
  root.addEventListener('pointerdown', event => {
    cancelPress();
    suppressRow = null;
    if (!event.isPrimary || event.pointerType !== 'touch' ||
        event.target.closest('button, a, input, [role=button]')) return;
    const row = event.target.closest('[data-resource-node]');
    if (!row || !root.contains(row)) return;
    press = {x: event.clientX, y: event.clientY, timer: setTimeout(() => {
      press = null;
      if (root.contains(row)) {
        suppressRow = row;
        openRow(row);
      }
    }, 550)};
  });
  root.addEventListener('pointermove', event => {
    if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) > 10) {
      cancelPress();
    }
  });
  for (const name of ['pointerup', 'pointercancel', 'pointerleave']) {
    root.addEventListener(name, cancelPress);
  }
}
