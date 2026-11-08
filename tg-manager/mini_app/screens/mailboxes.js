// Управление только собственными почтовыми подключениями.
async function openMailboxes() {
  push('s-mailboxes');
  const list = document.getElementById('mailboxList');
  list.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const data = await api('/api/miniapp/mailboxes');
    const oauthButtons = document.getElementById('mailboxOAuthButtons');
    oauthButtons.replaceChildren();
    for (const provider of data.oauth || []) {
      const container = document.createElement('div');
      oauthButtons.appendChild(container);
      prepareMailboxOAuth(provider, container);
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Диагностика входа Google';
      details.appendChild(summary);
      const values = document.createElement('pre');
      values.style.whiteSpace = 'pre-wrap';
      values.style.overflowWrap = 'anywhere';
      values.textContent = 'Адрес возврата (redirect_uri):\n' + (provider.redirect_uri || 'Не задан') +
        '\n\nИдентификатор клиента (client_id):\n' + (provider.client_id || 'Не задан') +
        '\n\nСравните эти значения с тем же клиентом в Google Cloud. Они получены от работающего сервера, не из кеша браузера. Секрет клиента здесь не отображается.';
      details.appendChild(values);
      oauthButtons.appendChild(details);
    }
    const select = document.getElementById('mailboxProvider');
    if (!select.options.length) {
      for (const provider of data.providers) {
        const option = document.createElement('option');
        option.value = provider.key;
        option.textContent = provider.label;
        select.appendChild(option);
      }
    }
    list.replaceChildren();
    if (!data.mailboxes.length) list.textContent = 'Почтовые ящики пока не подключены. Добавьте свой ниже.';
    for (const mailbox of data.mailboxes) {
      const row = document.createElement('div');
      row.className = 'li';
      const label = document.createElement('div');
      label.className = 'li-body';
      label.textContent = mailbox.email + (mailbox.is_active ? ' · Подключён' : ' · Отключён');
      const button = document.createElement('button');
      button.className = 'btn btn-s';
      button.textContent = 'Отключить';
      button.onclick = async () => {
        if (!await askConfirm('Удалить подключение ' + mailbox.email + '? Сам почтовый ящик не удаляется.')) return;
        button.disabled = true;
        try {
          await api('/api/miniapp/mailboxes/' + mailbox.id, {method:'DELETE'});
          await openMailboxes();
        } catch(e) { toast(errRu(e, 'Не удалось отключить')); button.disabled = false; }
      };
      row.append(label, button);
      list.appendChild(row);
    }
  } catch(e) { list.innerHTML = errHtml(errRu(e), 'openMailboxes()'); }
}

async function prepareMailboxOAuth(provider, container) {
  container.replaceChildren();
  const status = document.createElement('div');
  status.className = 'field-note';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  status.textContent = 'Готовим безопасную ссылку входа…';
  container.appendChild(status);
  try {
    const result = await api('/api/miniapp/mailboxes/oauth/' + provider.key, {method:'POST'});
    const url = new URL(result.url);
    if (url.protocol !== 'https:' || url.pathname !== '/oauth/email/start') {
      throw new Error('Сервер вернул некорректную ссылку входа. Проверьте настройки OAuth.');
    }
    const preparedAt = Date.now();
    const link = document.createElement('a');
    link.className = 'btn btn-s';
    link.style.width = '100%';
    link.style.boxSizing = 'border-box';
    link.href = url.href;
    link.textContent = provider.label;
    link.onclick = event => {
      event.preventDefault();
      if (Date.now() - preparedAt > 600000) {
        prepareMailboxOAuth(provider, container);
        return;
      }
      status.textContent = 'Открываем Google. После входа вернитесь и обновите список ящиков.';
      // Никакого await между касанием и открытием внешней страницы.
      openMailboxOAuthUrl(url.href);
    };
    container.insertBefore(link, status);
    status.textContent = 'Ссылка готова. Нажмите «' + provider.label + '».';
  } catch(e) {
    status.textContent = errRu(e, 'Не удалось подготовить вход');
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn-s';
    retry.textContent = 'Повторить подготовку входа';
    retry.onclick = () => prepareMailboxOAuth(provider, container);
    container.appendChild(retry);
  }
}

function openMailboxOAuthUrl(url) {
  if (tg && tg.initData && typeof tg.openLink === 'function') {
    tg.openLink(url);
  } else {
    // Переход в той же вкладке не блокируется браузером после await.
    window.location.assign(url);
  }
}

async function connectMailbox(event) {
  event.preventDefault();
  const button = document.getElementById('mailboxConnectBtn');
  if (button.disabled) return;
  const password = document.getElementById('mailboxPassword');
  const result = document.getElementById('mailboxResult');
  button.disabled = true;
  result.textContent = 'Проверяем защищённое подключение. Письмо не отправляется…';
  const body = JSON.stringify({
    email: document.getElementById('mailboxEmail').value.trim(),
    provider: document.getElementById('mailboxProvider').value,
    password: password.value,
  });
  password.value = '';
  try {
    await api('/api/miniapp/mailboxes', {method:'POST', body});
    result.textContent = 'Почта подключена. Ящик доступен для одиночных обращений.';
    await openMailboxes();
  } catch(e) { result.textContent = errRu(e, 'Не удалось подключить почту'); }
  finally { password.value = ''; button.disabled = false; }
}
