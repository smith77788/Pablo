// Управление только собственными почтовыми подключениями.
async function openMailboxes() {
  push('s-mailboxes');
  const list = document.getElementById('mailboxList');
  list.innerHTML = '<div class="spin-wrap"><div class="spin"></div></div>';
  try {
    const data = await api('/api/miniapp/mailboxes');
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
