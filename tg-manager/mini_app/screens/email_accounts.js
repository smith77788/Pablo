// Экран «Почтовые аккаунты»: список, добавление, проверка, отправка.

async function openEmailAccounts() {
  showScreen('s-email');
  await loadEmailAccounts();
}

async function loadEmailAccounts() {
  const list = document.getElementById('emailList');
  const empty = document.getElementById('emailEmpty');
  if (!list) return;
  list.innerHTML = '<div class="ld"></div>';
  try {
    const r = await api('/api/miniapp/email_accounts');
    const accs = r.accounts || [];
    const cnt = document.getElementById('m-email-cnt');
    if (cnt) cnt.textContent = accs.length || '';
    if (!accs.length) {
      list.innerHTML = '';
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';
    list.innerHTML = accs.map(a => `
      <div class="card" style="padding:12px;margin-bottom:8px;opacity:${a.is_active?1:0.5}">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <b>${esc(a.email)}</b>
            ${a.display_name ? '<span style="color:var(--hint);margin-left:6px;font-size:12px">'+esc(a.display_name)+'</span>' : ''}
          </div>
          <span style="font-size:11px;color:var(--hint)">${a.smtp_host}:${a.smtp_port}</span>
        </div>
        <div style="display:flex;gap:6px;margin-top:8px;font-size:12px;color:var(--hint)">
          <span>📨 ${a.sent_today}/${a.daily_limit} сегодня</span>
          <span>${a.is_active ? '🟢 Активен' : '🔴 Отключён'}</span>
        </div>
        <div style="display:flex;gap:6px;margin-top:8px">
          <button class="btn btn-sm" onclick="emailToggle(${a.id}, ${!a.is_active})">${a.is_active?'⏸ Выкл':'▶️ Вкл'}</button>
          <button class="btn btn-sm" onclick="emailTestById(${a.id})">🔌 Тест</button>
          <button class="btn btn-sm" onclick="openEmailSendWith(${a.id})">✉️ Письмо</button>
          <button class="btn btn-sm btn-danger" onclick="emailRemove(${a.id})">🗑</button>
        </div>
      </div>
    `).join('');
  } catch(e) {
    list.innerHTML = '<div class="empty-inline">Ошибка загрузки: '+esc(errRu(e))+'</div>';
  }
}

function openEmailAdd() {
  showScreen('s-email-add');
  ['eaEmail','eaHost','eaUser','eaPass','eaName'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  const port = document.getElementById('eaPort');
  if (port) port.value = '587';
  const limit = document.getElementById('eaLimit');
  if (limit) limit.value = '50';
  const hint = document.getElementById('eaProviderHint');
  if (hint) hint.style.display = 'none';
  const st = document.getElementById('eaStatus');
  if (st) st.style.display = 'none';
}

async function emailDetectProvider(addr) {
  const hint = document.getElementById('eaProviderHint');
  if (!hint) return;
  if (!addr || !addr.includes('@')) { hint.style.display = 'none'; return; }
  try {
    const r = await api('/api/miniapp/email/providers?email=' + encodeURIComponent(addr));
    if (r.detected) {
      const p = r.detected;
      document.getElementById('eaHost').value = p.smtp_host;
      document.getElementById('eaPort').value = p.smtp_port;
      if (!document.getElementById('eaUser').value) {
        document.getElementById('eaUser').value = addr;
      }
      hint.textContent = p.hint || ('Провайдер: ' + p.name);
      hint.style.display = '';
    } else {
      hint.style.display = 'none';
    }
  } catch(e) { /* тихо */ }
}

async function emailTestConn() {
  const st = document.getElementById('eaStatus');
  if (st) { st.textContent = '⏳ Проверка…'; st.style.display = ''; st.style.color = 'var(--hint)'; }
  try {
    const r = await api('/api/miniapp/email_accounts/test', {
      method: 'POST',
      body: JSON.stringify({
        smtp_host: document.getElementById('eaHost').value,
        smtp_port: parseInt(document.getElementById('eaPort').value) || 587,
        smtp_user: document.getElementById('eaUser').value,
        smtp_pass: document.getElementById('eaPass').value,
      })
    });
    if (r.ok) {
      if (st) { st.textContent = '✅ Подключение успешно!'; st.style.color = 'var(--green, #4caf50)'; }
    } else {
      if (st) { st.textContent = '❌ ' + (r.error || 'Ошибка'); st.style.color = 'var(--red, #e53935)'; }
    }
  } catch(e) {
    if (st) { st.textContent = '❌ ' + errRu(e, 'Ошибка проверки'); st.style.color = 'var(--red, #e53935)'; }
  }
}

async function emailSaveAccount() {
  const email = document.getElementById('eaEmail').value.trim();
  const host = document.getElementById('eaHost').value.trim();
  const port = parseInt(document.getElementById('eaPort').value) || 587;
  const user = document.getElementById('eaUser').value.trim();
  const pass = document.getElementById('eaPass').value;
  const name = document.getElementById('eaName').value.trim();
  const limit = parseInt(document.getElementById('eaLimit').value) || 50;
  if (!email) { toast('⚠️ Укажите email'); return; }
  if (!host) { toast('⚠️ Укажите SMTP-сервер'); return; }
  if (!user) { toast('⚠️ Укажите логин'); return; }
  if (!pass) { toast('⚠️ Укажите пароль'); return; }
  try {
    await api('/api/miniapp/email_accounts/add', {
      method: 'POST',
      body: JSON.stringify({
        email, smtp_host: host, smtp_port: port,
        smtp_user: user, smtp_pass: pass,
        display_name: name, daily_limit: limit
      })
    });
    toast('✅ Аккаунт добавлен');
    back();
    await loadEmailAccounts();
  } catch(e) { toast('⚠️ ' + errRu(e, 'Ошибка')); }
}

async function emailToggle(id, active) {
  try {
    await api('/api/miniapp/email_accounts/toggle', {
      method: 'POST', body: JSON.stringify({ id, is_active: active })
    });
    toast(active ? '✅ Включён' : '⏸ Отключён');
    await loadEmailAccounts();
  } catch(e) { toast('⚠️ ' + errRu(e)); }
}

async function emailTestById(id) {
  toast('⏳ Проверка…');
  try {
    const r = await api('/api/miniapp/email_accounts/test', {
      method: 'POST', body: JSON.stringify({ id })
    });
    toast(r.ok ? '✅ Подключение работает' : '❌ ' + (r.error || 'Ошибка'));
  } catch(e) { toast('⚠️ ' + errRu(e)); }
}

async function emailRemove(id) {
  if (!await askConfirm('Удалить этот почтовый аккаунт?')) return;
  try {
    await api('/api/miniapp/email_accounts/remove', {
      method: 'POST', body: JSON.stringify({ id })
    });
    toast('🗑 Удалён');
    await loadEmailAccounts();
  } catch(e) { toast('⚠️ ' + errRu(e)); }
}

async function openEmailSendWith(accountId) {
  showScreen('s-email-send');
  const sel = document.getElementById('emFrom');
  const st = document.getElementById('emStatus');
  if (st) st.style.display = 'none';
  ['emTo','emSubj','emBody'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  try {
    const r = await api('/api/miniapp/email_accounts');
    const accs = (r.accounts || []).filter(a => a.is_active);
    if (sel) {
      sel.innerHTML = accs.map(a =>
        `<option value="${a.id}"${a.id===accountId?' selected':''}>${esc(a.email)}</option>`
      ).join('');
    }
  } catch(e) { /* тихо */ }
}

async function emailDoSend() {
  const st = document.getElementById('emStatus');
  const accId = parseInt(document.getElementById('emFrom').value);
  const to = document.getElementById('emTo').value.trim();
  const subj = document.getElementById('emSubj').value.trim();
  const body = document.getElementById('emBody').value.trim();
  if (!to) { toast('⚠️ Укажите получателя'); return; }
  if (!body) { toast('⚠️ Пустое письмо'); return; }
  if (st) { st.textContent = '⏳ Отправка…'; st.style.display = ''; st.style.color = 'var(--hint)'; }
  try {
    const r = await api('/api/miniapp/email/send', {
      method: 'POST',
      body: JSON.stringify({ account_id: accId, to, subject: subj, body })
    });
    if (r.ok) {
      if (st) { st.textContent = '✅ Письмо отправлено!'; st.style.color = 'var(--green, #4caf50)'; }
      toast('✅ Письмо отправлено');
    } else {
      if (st) { st.textContent = '❌ ' + (r.error || 'Ошибка'); st.style.color = 'var(--red, #e53935)'; }
    }
  } catch(e) {
    if (st) { st.textContent = '❌ ' + errRu(e, 'Ошибка'); st.style.color = 'var(--red, #e53935)'; }
  }
}
