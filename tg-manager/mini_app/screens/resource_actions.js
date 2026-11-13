'use strict';

// Действия открывают штатные карточки и используют их проверки доступа.
function projectResourceActions(node, trigger) {
  const id = Number(node.object_id);
  const primary = [];
  const secondary = [];
  if (Number.isSafeInteger(id) && id !== 0) {
    const openers = {
      account: () => openAccount(id),
      bot: () => openBot(id),
      channel: () => openChannel(id),
      group: () => openChannel(id),
      chat: () => openChannel(id),
    };
    if (Object.hasOwn(openers, node.type)) {
      primary.push(['Открыть карточку', openers[node.type]]);
    }
    primary.push(['Копировать ID', () => copyText(String(node.object_id))]);
    if (node.type === 'account') secondary.push(['Диалоги профиля', () => openAccountDialogs(id)]);
    if (node.type === 'bot') secondary.push(['Команды бота', () => openBotCommands(id)]);
  }
  secondary.push(['Убрать из проекта', () => removeNetNode(Number(node.id), trigger)]);
  return {primary, secondary};
}

function openProjectResourceActions(node, trigger) {
  const dialog = document.getElementById('resourceActions');
  if (!dialog || dialog.open) return;
  const actions = projectResourceActions(node, trigger);
  document.getElementById('resourceActionsTitle').textContent = node.label || objRu(node.type);
  const more = document.getElementById('resourceActionsMore');
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
        dialog.close();
        run();
      });
      return button;
    });
    document.getElementById(id).replaceChildren(...buttons);
  }
  dialog.addEventListener('close', () => {
    if (trigger?.isConnected) trigger.focus();
  }, {once: true});
  dialog.showModal();
}

const resourceActionRowHandlers = new WeakMap();

function bindResourceActionRows(root, open) {
  if (resourceActionRowHandlers.has(root)) {
    resourceActionRowHandlers.set(root, open);
    return;
  }
  resourceActionRowHandlers.set(root, open);
  const openRow = row => resourceActionRowHandlers.get(root)(
    Number(row.dataset.resourceNode), row.querySelector('[data-resource-menu]'));
  let press = null;
  function cancelPress() {
    if (press) clearTimeout(press.timer);
    press = null;
  }
  root.addEventListener('click', event => {
    const trigger = event.target.closest('[data-resource-menu]');
    if (trigger) openRow(trigger.closest('[data-resource-node]'));
  });
  root.addEventListener('contextmenu', event => {
    const row = event.target.closest('[data-resource-node]');
    if (!row) return;
    event.preventDefault();
    cancelPress();
    openRow(row);
  });
  root.addEventListener('pointerdown', event => {
    cancelPress();
    if (!event.isPrimary || event.pointerType !== 'touch' ||
        event.target.closest('button, a, input')) return;
    const row = event.target.closest('[data-resource-node]');
    if (!row) return;
    press = {x: event.clientX, y: event.clientY, timer: setTimeout(() => {
      press = null;
      if (root.contains(row)) openRow(row);
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
