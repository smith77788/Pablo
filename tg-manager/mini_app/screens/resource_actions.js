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
  actions.groups = [['Состав проекта', [
    ['Убрать из проекта', () => removeNetNode(Number(node.id), trigger), {danger: true}],
  ]]];
  return actions;
}

function openProjectResourceActions(node, trigger, projectId) {
  showResourceActions(node.label || objRu(node.type), projectResourceActions(node, trigger, projectId), trigger);
}

function openNetworkResourceActions(project, trigger) {
  const id = Number(project.id);
  if (!Number.isSafeInteger(id) || id <= 0) return;
  const inProject = run => async () => {
    await selectNetwork(id);
    if (_currentNetId === id && Number(window._lastNetData?.id) === id &&
        STACK.at(-1) === 's-network-builder') run();
  };
  showResourceActions(project.name || 'Проект #' + id, {
    primary: [
      ['Открыть топологию', () => selectNetwork(id)],
      ['Журнал проекта', () => openOps(id)],
    ],
    secondary: [['Копировать ID проекта', () => copyText(String(id))]],
    groups: [['Топология', [
      ['Добавить узел', inProject(openNetAddNodeModal)],
      ['Связать узлы', inProject(openNetAddEdgeModal)],
      ['Посмотреть план развёртывания', inProject(() => showNetDeployPlan())],
    ]]],
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
  const username = String(item.username ?? '').replace(/^@/, '');
  const links = [];
  if (/^[a-z0-9_]{1,32}$/i.test(username)) {
    actions.primary.push(['Копировать ссылку', () => copyText('https://t.me/' + username)]);
    links.push(['Открыть в Telegram', () => openTgUser(username)],
      ['Копировать @имя', () => copyText('@' + username)]);
  }
  const management = [];
  const removal = [];
  if (kind !== 'bot' || item.is_owned !== false) {
    management.push(['Добавить в проект', () => addCatalogResourceToProject(kind, id, label)]);
  }
  if (kind === 'account') {
    management.push(['Каналы и чаты аккаунта', () => openAccountChannels(id)]);
    if (item.is_active === true) management.push(['Изменить профиль', () => openAccProfile(id)]);
    if (fromCard) {
      management.push(['Изменить метку и кластер', () => openAccMeta(id, item)],
        ['Заметка', () => openAccNote(id, item)]);
    }
  } else if (kind === 'bot') {
    if (item.is_owned === true) {
      if (item.is_active === true) management.push(['Изменить профиль', () => openBotProfile(id)]);
      management.push(['Заметка', () => openBotNote(id)]);
      if (typeof item.is_active === 'boolean') {
        management.push([item.is_active ? 'Поставить на паузу' : 'Вернуть в работу',
          () => botPowerConfirm(id, label, item.is_active)]);
      }
      removal.push(['Убрать бота из Infragram', () => botDeleteConfirm(id, label), {danger: true}]);
    }
  } else if (fromCard) {
    // Подтверждения и обновление карточки остаются в штатных обработчиках.
    if (item.acc_id && (item.role === 'owner' || item.role === 'admin')) {
      management.push(['Подготовить публикацию', () => openChPostModal(id)],
        ['Изменить название или описание', () => openChanEdit(id)]);
    }
    if (Number.isSafeInteger(Number(id)) && item.acc_id) {
      if (item.role === 'owner') {
        removal.push(['Удалить канал или чат в Telegram', () => openChDestructive('delete', [id]), {danger: true}]);
      }
      if (item.role === 'member') removal.push(['Покинуть чат или канал', () => runChLeave([id]), {danger: true}]);
    }
    removal.push(['Убрать из Infragram', () => removeChannel(id, trigger, label), {danger: true}]);
  }
  actions.groups = [['Ссылки', links], ['Управление', management], ['Удаление и выход', removal]];
  showResourceActions(label, actions, trigger);
}

function openSelectedResourceActions(type, trigger) {
  const selectors = {account: () => ACC_SEL, bot: () => BOT_SEL, channel: () => CH_SEL};
  if (!Object.hasOwn(selectors, type)) return;
  const current = selectors[type];
  const ids = [...current()];
  const accountScope = type === 'account' ? JSON.stringify(_massSel()) : '';
  const all = type === 'account' && ACC_SEL_ALL_FILTERED;
  const count = all ? ACC_FILTERED_TOTAL || 0 : ids.length;
  if (!count) { toast('Сначала отметьте ресурсы'); return; }
  const exact = !all && ids.every(id => Number.isSafeInteger(id) && id !== 0);
  if (!all && !exact) {
    toast('У части ресурсов нет точного ID. Уточните выбор и повторите.');
    return;
  }
  const guarded = run => () => {
    if (type === 'account' ? JSON.stringify(_massSel()) !== accountScope ||
        (all && (ACC_FILTERED_TOTAL || 0) !== count) :
        current().size !== ids.length || ids.some(id => !current().has(id))) {
      toast('Выбор изменился. Откройте меню ещё раз.');
      return;
    }
    run();
  };
  const actions = {primary: exact ? [['Копировать ID выбранных', guarded(() => copyText(ids.join('\n')))]] : [],
    secondary: [], groups: []};
  const danger = {danger: true};
  if (type === 'account') {
    actions.primary.push(['Проверить аккаунты', guarded(() => runAccMass('check'))]);
    actions.groups = [
      ['Профили и организация', [
        ['Сканировать ресурсы', guarded(() => runAccMass('scan'))],
        ['Изменить профиль', guarded(openMassProfile)], ['Изменить статус', guarded(openMassStage)],
        ['Поставить тег', guarded(() => runAccTag('add_tag'))],
        ['Снять тег', guarded(() => runAccTag('remove_tag'))], ['Добавить в пул', guarded(runAccPool)],
      ]],
      ['Подключение и удаление', [
        ['Снять прокси', guarded(() => runAccMass('detach_proxy'))],
        ['Выйти из всех чатов', guarded(() => runAccMass('leave_all')), danger],
        ['Удалить аккаунты из Infragram', guarded(() => runAccMass('delete')), danger],
      ]],
    ];
  } else if (type === 'bot') {
    actions.groups = [['Управление ботами', [
      ['Подготовить рассылку', guarded(openBotBroadcast)],
      ['Изменить профиль', guarded(openBotProfileEdit)],
      ['Убрать ботов из Infragram', guarded(bulkDeleteBots), danger],
    ]]];
  } else {
    actions.primary.push(['Подготовить публикацию', guarded(() => openChMass('post'))]);
    actions.groups = [
      ['Публикации и настройки', [
        ['Изменить название', guarded(() => openChMass('title'))],
        ['Изменить описание', guarded(() => openChMass('about'))],
        ...(ids.length === 1 ? [['Изменить @имя', guarded(() => openChMass('username'))]] : []),
        ['Назначить администраторов', guarded(() => runChMass('promote'))],
        ['Настроить автопостинг', guarded(() => openChAutopost(ids))],
      ]],
      ['Удаление и выход', [
        ['Очистить посты', guarded(() => openChDestructive('clear', ids)), danger],
        ['Удалить в Telegram', guarded(() => openChDestructive('delete', ids)), danger],
        ['Покинуть каналы и чаты', guarded(() => runChLeave(ids)), danger],
        ['Убрать из Infragram', guarded(bulkDeleteChannels), danger],
      ]],
    ];
  }
  showResourceActions((all ? 'Весь срез: ' : 'Выбрано: ') + count, actions, trigger);
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
  const groups = [['Инструменты', actions.secondary], ...(actions.groups || [])]
    .filter(([, items]) => items.length);
  if (!dialog || dialog.open || !(actions.primary.length || groups.length)) return;
  document.getElementById('resourceActionsTitle').textContent = title;
  let restoreFocus = true;
  const more = document.getElementById('resourceActionsMore');
  const search = document.getElementById('resourceActionsSearch');
  const empty = document.getElementById('resourceActionsEmpty');
  more.hidden = !groups.length;
  more.open = !actions.primary.length;
  search.value = '';
  search.hidden = actions.primary.length + groups.reduce((n, [, items]) => n + items.length, 0) < 7;
  empty.hidden = true;

  function actionButton([label, run, options]) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-s';
    button.classList.toggle('resource-action-danger', options?.danger === true);
    button.textContent = label;
    button.addEventListener('click', () => {
      restoreFocus = false;
      dialog.close();
      if (trigger?.isConnected) trigger.focus();
      run();
    });
    return button;
  }
  const primary = document.getElementById('resourceActionsPrimary');
  primary.replaceChildren(...actions.primary.map(actionButton));
  primary.hidden = !actions.primary.length;
  const sections = groups.map(([label, items]) => {
    const section = document.createElement('section');
    section.className = 'resource-actions-list';
    const heading = document.createElement('h3');
    heading.textContent = label;
    section.append(heading, ...items.map(actionButton));
    return section;
  });
  document.getElementById('resourceActionsSecondary').replaceChildren(...sections);
  search.oninput = () => {
    const query = search.value.trim().toLocaleLowerCase('ru');
    let matched = false;
    for (const section of [primary, ...sections]) {
      const groupName = section.querySelector('h3')?.textContent || '';
      let visible = false;
      for (const button of section.querySelectorAll('button')) {
        button.hidden = !(button.textContent + ' ' + groupName).toLocaleLowerCase('ru').includes(query);
        visible ||= !button.hidden;
      }
      section.hidden = !visible;
      matched ||= visible;
    }
    more.hidden = !sections.some(section => !section.hidden);
    more.open = !more.hidden && (!!query || primary.hidden);
    empty.hidden = matched;
  };
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
