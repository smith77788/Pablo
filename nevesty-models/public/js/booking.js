/* ─── Booking form logic ────────────────────────────── */
(function () {
  let BOT_USERNAME = '';
  fetch('/api/config').then(r => r.json()).then(cfg => { BOT_USERNAME = cfg.bot_username || ''; }).catch(() => {});

  const state = {
    step: 1,
    model_id: null,
    model_name: null,
    event_type: '',
    event_date: '',
    event_duration: '4',
    location: '',
    budget: '',
    comments: '',
    client_name: '',
    client_phone: '',
    client_email: '',
    client_telegram: '',
  };

  const EVENT_LABELS = {
    fashion_show: 'Показ мод', photo_shoot: 'Фотосессия',
    event: 'Корпоратив / Мероприятие', commercial: 'Коммерческая съёмка',
    runway: 'Подиум', other: 'Другое'
  };

  // Load models for selector
  apiFetch('/models?available=1').then(models => {
    const grid = document.getElementById('modelSelectGrid');
    if (!grid) return;
    grid.innerHTML = models.slice(0, 8).map(m => `
      <div class="model-select-card" id="mc_${m.id}" onclick="_booking.selectModel(${m.id}, '${m.name}')">
        <div class="model-select-thumb">
          ${m.photo_main
            ? `<img src="${m.photo_main}" alt="${m.name}" />`
            : `<div style="width:100%;height:100%;background:var(--bg3);display:flex;align-items:center;justify-content:center;color:rgba(201,169,110,0.3);font-size:1.5rem">${m.name[0]}</div>`
          }
        </div>
        <div class="model-select-info">
          <strong>${m.name}</strong>
          <span>${m.height}см · ${m.hair_color}</span>
        </div>
      </div>`).join('');
  }).catch(() => {});

  // Pre-select model from URL
  const urlParams = new URLSearchParams(window.location.search);
  const urlModel = urlParams.get('model');
  if (urlModel) {
    apiFetch(`/models/${urlModel}`).then(m => {
      selectModel(m.id, m.name);
    }).catch(() => {});
  }

  function selectModel(id, name) {
    state.model_id = id;
    state.model_name = name;
    document.querySelectorAll('.model-select-card').forEach(c => c.classList.remove('selected'));
    document.getElementById('noModelOption')?.classList.remove('selected');
    if (id) {
      document.getElementById(`mc_${id}`)?.classList.add('selected');
    } else {
      document.getElementById('noModelOption')?.classList.add('selected');
    }
  }

  /* ─── Validation helpers ──────────────────────────── */
  function clearErrors() {
    document.querySelectorAll('.field-error-msg').forEach(el => el.remove());
    document.querySelectorAll('.form-control[data-error]').forEach(el => {
      el.style.borderColor = '';
      el.removeAttribute('data-error');
    });
  }

  function markError(id, msg) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.borderColor = 'var(--error, #e05c5c)';
    el.setAttribute('data-error', '1');
    // Remove any existing error message for this field
    el.parentNode.querySelectorAll('.field-error-msg').forEach(n => n.remove());
    const hint = document.createElement('div');
    hint.className = 'field-error-msg';
    hint.style.cssText = 'color:var(--error,#e05c5c);font-size:0.78rem;margin-top:4px';
    hint.textContent = msg;
    el.insertAdjacentElement('afterend', hint);
    // Clear error on user input
    el.addEventListener('input', () => {
      el.style.borderColor = '';
      el.removeAttribute('data-error');
      hint.remove();
    }, { once: true });
  }

  /* ─── Restore step fields from state ─────────────── */
  function restoreStep(n) {
    if (n === 2) {
      document.getElementById('event_type').value = state.event_type || '';
      document.getElementById('event_date').value = state.event_date || '';
      document.getElementById('event_duration').value = state.event_duration || '4';
      document.getElementById('location').value = state.location || '';
      document.getElementById('budget').value = state.budget || '';
      document.getElementById('comments').value = state.comments || '';
    }
    if (n === 3) {
      document.getElementById('client_name').value = state.client_name || '';
      document.getElementById('client_phone').value = state.client_phone || '';
      document.getElementById('client_email').value = state.client_email || '';
      document.getElementById('client_telegram').value = state.client_telegram ? '@' + state.client_telegram : '';
    }
  }

  function nextStep() {
    if (state.step === 2) {
      clearErrors();
      const type = document.getElementById('event_type').value;
      if (!type) {
        markError('event_type', 'Выберите тип мероприятия');
        toast('Выберите тип мероприятия', 'error');
        return;
      }
      state.event_type = type;
      state.event_date = document.getElementById('event_date').value;
      state.event_duration = document.getElementById('event_duration').value;
      state.location = document.getElementById('location').value;
      state.budget = document.getElementById('budget').value;
      state.comments = document.getElementById('comments').value;
    }
    if (state.step === 3) {
      clearErrors();
      const name = document.getElementById('client_name').value.trim();
      const phone = document.getElementById('client_phone').value.trim();
      const email = document.getElementById('client_email').value.trim();
      let hasError = false;
      if (!name) {
        markError('client_name', 'Введите ваше имя');
        hasError = true;
      }
      if (!phone) {
        markError('client_phone', 'Введите номер телефона');
        hasError = true;
      } else if (!/^[\d\s\+\-\(\)]{7,}$/.test(phone)) {
        markError('client_phone', 'Введите корректный номер телефона');
        hasError = true;
      }
      if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        markError('client_email', 'Введите корректный email');
        hasError = true;
      }
      if (hasError) { toast('Проверьте правильность заполнения полей', 'error'); return; }
      state.client_name = name;
      state.client_phone = phone;
      state.client_email = email;
      state.client_telegram = document.getElementById('client_telegram').value.trim().replace('@', '');
      buildSummary();
    }
    goToStep(state.step + 1);
  }

  function prevStep() { goToStep(state.step - 1); }

  function goToStep(n) {
    document.getElementById(`step${state.step}`)?.classList.remove('active');
    document.querySelectorAll('.booking-step').forEach(s => {
      const sn = +s.dataset.step;
      s.classList.remove('active', 'done');
      if (sn < n) s.classList.add('done');
      else if (sn === n) s.classList.add('active');
    });
    for (let i = 1; i <= 3; i++) {
      const line = document.getElementById(`line${i}`);
      if (line) line.classList.toggle('done', i < n);
    }
    state.step = n;
    document.getElementById(`step${n}`)?.classList.add('active');
    restoreStep(n);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function buildSummary() {
    document.getElementById('orderSummary').innerHTML = `
      <div class="summary-row"><label>Модель</label><span>${state.model_name || 'Менеджер подберёт'}</span></div>
      <div class="summary-row"><label>Мероприятие</label><span>${EVENT_LABELS[state.event_type] || state.event_type}</span></div>
      ${state.event_date ? `<div class="summary-row"><label>Дата</label><span>${formatDate(state.event_date)}</span></div>` : ''}
      <div class="summary-row"><label>Продолжительность</label><span>${state.event_duration} ч</span></div>
      ${state.location ? `<div class="summary-row"><label>Место</label><span>${state.location}</span></div>` : ''}
      ${state.budget ? `<div class="summary-row"><label>Бюджет</label><span>${state.budget}</span></div>` : ''}
      <div class="summary-row"><label>Имя</label><span>${state.client_name}</span></div>
      <div class="summary-row"><label>Телефон</label><span>${state.client_phone}</span></div>
      ${state.client_email ? `<div class="summary-row"><label>Email</label><span>${state.client_email}</span></div>` : ''}
      ${state.client_telegram ? `<div class="summary-row"><label>Telegram</label><span>@${state.client_telegram}</span></div>` : ''}`;
  }

  function formatDate(d) {
    if (!d) return '';
    const [y, m, day] = d.split('-');
    return `${day}.${m}.${y}`;
  }

  async function submit() {
    const btn = document.getElementById('submitBtn');
    btn.disabled = true;
    btn.textContent = 'Отправка...';
    try {
      const result = await apiFetch('/orders', {
        method: 'POST',
        body: JSON.stringify({
          client_name: state.client_name,
          client_phone: state.client_phone,
          client_email: state.client_email || null,
          client_telegram: state.client_telegram || null,
          model_id: state.model_id || null,
          event_type: state.event_type,
          event_date: state.event_date || null,
          event_duration: +state.event_duration || 4,
          location: state.location || null,
          budget: state.budget || null,
          comments: state.comments || null,
        })
      });

      document.getElementById('orderNumDisplay').textContent = result.order_number;

      // Notify Telegram Mini App if running inside it
      if (window._tgWebAppOnBookingSuccess) {
        window._tgWebAppOnBookingSuccess(result.order_number);
      }

      // Telegram connect link
      const tgLink = document.getElementById('tgConnectLink');
      const tgBox = document.getElementById('tgConnectBox');
      if (state.client_telegram) {
        tgBox.style.display = 'none';
      } else {
        tgLink.href = `https://t.me/${BOT_USERNAME}?start=${result.order_number}`;
      }

      document.querySelectorAll('.booking-step').forEach(s => s.classList.add('done'));
      for (let i = 1; i <= 3; i++) document.getElementById(`line${i}`)?.classList.add('done');
      document.getElementById(`step${state.step}`)?.classList.remove('active');
      document.getElementById('step5').classList.add('active');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (e) {
      toast(e.message || 'Ошибка при отправке заявки', 'error');
      btn.disabled = false;
      btn.textContent = 'Отправить заявку ✓';
    }
  }

  async function checkStatus() {
    const num = document.getElementById('statusInput').value.trim().toUpperCase();
    if (!num) { toast('Введите номер заявки', 'error'); return; }
    const resultEl = document.getElementById('statusResult');
    resultEl.innerHTML = '<span style="color:var(--text-muted)">Поиск...</span>';
    try {
      const o = await apiFetch(`/orders/status/${num}`);
      const statusLabels = {
        new: '🆕 Новая', reviewing: '🔍 На рассмотрении', confirmed: '✅ Подтверждена',
        in_progress: '▶️ В процессе', completed: '🏁 Завершена', cancelled: '❌ Отменена'
      };
      resultEl.innerHTML = `
        <div style="background:var(--bg3);border:1px solid var(--border);padding:20px;margin-top:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:12px">
            <strong style="color:var(--gold)">${o.order_number}</strong>
            <span>${statusLabels[o.status] || o.status}</span>
          </div>
          <div style="font-size:0.85rem;color:var(--text-muted)">
            ${o.event_type ? `Мероприятие: ${EVENT_LABELS[o.event_type] || o.event_type}<br>` : ''}
            ${o.event_date ? `Дата: ${formatDate(o.event_date)}<br>` : ''}
            ${o.model_name ? `Модель: ${o.model_name}` : ''}
          </div>
        </div>`;
    } catch {
      resultEl.innerHTML = '<span style="color:var(--error)">Заявка не найдена. Проверьте номер.</span>';
    }
  }

  window._booking = { nextStep, prevStep, selectModel, submit, checkStatus };
})();
