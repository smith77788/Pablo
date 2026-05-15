/* ─── Booking form logic ────────────────────────────── */
(function () {
  let BOT_USERNAME = '';
  fetch('/api/config').then(r => r.json()).then(cfg => { BOT_USERNAME = cfg.bot_username || ''; }).catch(() => {});

  const state = {
    step: 1,
    model_id: null,
    model_name: null,
    model_photo: null,
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

  const EVENT_ICONS = {
    fashion_show: '👗', photo_shoot: '📸',
    event: '🥂', commercial: '🎬',
    runway: '✦', other: '📋'
  };

  /* ─── Set min date on date input ─────────────────── */
  const today = new Date().toISOString().split('T')[0];
  const dateInput = document.getElementById('event_date');
  if (dateInput) dateInput.min = today;

  /* ─── Character counter for comments ──────────────── */
  const commentsEl = document.getElementById('comments');
  const charCountEl = document.getElementById('charCount');
  if (commentsEl && charCountEl) {
    commentsEl.addEventListener('input', () => {
      const len = commentsEl.value.length;
      charCountEl.textContent = len;
      charCountEl.style.color = len > 450 ? 'var(--gold)' : '';
    });
  }

  /* ─── Phone mask: +7 (xxx) xxx-xx-xx ───────────── */
  const phoneEl = document.getElementById('client_phone');
  if (phoneEl) {
    function formatPhone(val) {
      // Strip everything except digits and leading +
      let digits = val.replace(/\D/g, '');
      // Normalize: 8 → 7, ensure 7 prefix
      if (digits.startsWith('8')) digits = '7' + digits.slice(1);
      if (!digits.startsWith('7')) digits = '7' + digits;
      // Keep at most 11 digits
      digits = digits.slice(0, 11);
      const d = digits.slice(1); // digits after country code
      let result = '+7';
      if (d.length > 0) result += ' (' + d.slice(0, 3);
      if (d.length >= 3) result += ') ' + d.slice(3, 6);
      if (d.length >= 6) result += '-' + d.slice(6, 8);
      if (d.length >= 8) result += '-' + d.slice(8, 10);
      return result;
    }

    phoneEl.addEventListener('focus', () => {
      if (!phoneEl.value.trim()) phoneEl.value = '+7 (';
    });

    phoneEl.addEventListener('input', (e) => {
      const selStart = phoneEl.selectionStart;
      const oldVal = phoneEl.value;
      const newVal = formatPhone(oldVal);
      phoneEl.value = newVal;
      // Keep cursor near its position
      const diff = newVal.length - oldVal.length;
      try { phoneEl.setSelectionRange(selStart + diff, selStart + diff); } catch {}
    });

    phoneEl.addEventListener('keydown', (e) => {
      // Allow backspace to delete formatted chars naturally
      if (e.key === 'Backspace' && phoneEl.value.length <= 4) {
        phoneEl.value = '';
        e.preventDefault();
      }
    });

    phoneEl.addEventListener('blur', () => {
      if (phoneEl.value === '+7 (' || phoneEl.value === '+7') phoneEl.value = '';
    });
  }

  /* ─── Real-time email validation ────────────────── */
  const emailEl = document.getElementById('client_email');
  if (emailEl) {
    let emailTimeout;
    const emailHint = document.createElement('div');
    emailHint.style.cssText = 'font-size:0.72rem;margin-top:5px;line-height:1.5;';
    emailEl.parentNode.appendChild(emailHint);

    emailEl.addEventListener('input', () => {
      clearTimeout(emailTimeout);
      emailTimeout = setTimeout(() => {
        const val = emailEl.value.trim();
        if (!val) {
          emailHint.textContent = '';
          emailEl.style.borderColor = '';
          return;
        }
        const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(val);
        if (valid) {
          emailHint.style.color = 'var(--gold)';
          emailHint.textContent = '✓ Email корректный';
          emailEl.style.borderColor = 'rgba(201,169,110,0.4)';
        } else {
          emailHint.style.color = '#e05c5c';
          emailHint.textContent = '✕ Проверьте формат email (например: name@mail.ru)';
          emailEl.style.borderColor = 'var(--error, #e05c5c)';
        }
      }, 400);
    });

    emailEl.addEventListener('blur', () => {
      clearTimeout(emailTimeout);
      const val = emailEl.value.trim();
      if (val && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(val)) {
        emailHint.style.color = '#e05c5c';
        emailHint.textContent = '✕ Проверьте формат email';
        emailEl.style.borderColor = 'var(--error, #e05c5c)';
      } else if (val) {
        emailHint.style.color = 'var(--gold)';
        emailHint.textContent = '✓ Email корректный';
        emailEl.style.borderColor = 'rgba(201,169,110,0.4)';
      } else {
        emailHint.textContent = '';
        emailEl.style.borderColor = '';
      }
    });
  }

  /* ─── Load models for selector ───────────────────── */
  apiFetch('/models?available=1').then(models => {
    const grid = document.getElementById('modelSelectGrid');
    if (!grid) return;
    const subset = models.slice(0, 8);
    grid.innerHTML = subset.map(m => `
      <div class="model-select-card" id="mc_${m.id}" role="button" tabindex="0"
           onclick="_booking.selectModel(${m.id}, '${escHtml(m.name)}', '${m.photo_main || ''}')"
           onkeydown="if(event.key==='Enter'||event.key===' ')_booking.selectModel(${m.id}, '${escHtml(m.name)}', '${m.photo_main || ''}')">
        <div class="model-select-thumb">
          ${m.photo_main
            ? `<img src="${m.photo_main}" alt="${escHtml(m.name)}" loading="lazy" />`
            : `<div class="model-select-thumb-placeholder">${escHtml(m.name[0])}</div>`
          }
        </div>
        <div class="model-select-info">
          <strong>${escHtml(m.name)}</strong>
          <span>${m.height ? m.height + 'см' : ''}${m.hair_color ? ' · ' + escHtml(m.hair_color) : ''}</span>
        </div>
        <div class="model-select-check" aria-hidden="true">✓</div>
      </div>`).join('');

    /* After grid is populated, handle URL model param */
    initFromUrlParams();
  }).catch(() => {
    initFromUrlParams();
  });

  /* ─── Parse URL params & pre-select model ─────────── */
  function initFromUrlParams() {
    const urlParams = new URLSearchParams(window.location.search);
    const urlModel = urlParams.get('model');
    if (!urlModel) return;

    apiFetch(`/models/${urlModel}`).then(m => {
      selectModel(m.id, m.name, m.photo_main || '');
      showModelInfoCard(m);
      // Scroll to step 1 grid so user sees the selection
      document.getElementById('modelSelectGrid')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }).catch(() => {});
  }

  /* ─── Model info card (shown when pre-filled from URL) ─ */
  function showModelInfoCard(m) {
    const container = document.getElementById('prefilledModelCard');
    if (!container) return;
    container.innerHTML = `
      <div class="prefilled-model-card">
        <div class="prefilled-model-thumb">
          ${m.photo_main
            ? `<img src="${m.photo_main}" alt="${escHtml(m.name)}" />`
            : `<div class="model-select-thumb-placeholder">${escHtml(m.name[0])}</div>`}
        </div>
        <div class="prefilled-model-info">
          <div class="prefilled-model-tag">Выбранная модель</div>
          <strong>${escHtml(m.name)}</strong>
          <span>${[m.height ? m.height + ' см' : '', m.hair_color, m.eye_color].filter(Boolean).join(' · ')}</span>
        </div>
        <button class="prefilled-model-clear" onclick="_booking.selectModel(null)" title="Отменить выбор">✕</button>
      </div>`;
    container.style.display = 'block';
  }

  function selectModel(id, name, photo) {
    state.model_id = id;
    state.model_name = name || null;
    state.model_photo = photo || null;
    document.querySelectorAll('.model-select-card').forEach(c => c.classList.remove('selected'));
    document.getElementById('noModelOption')?.classList.remove('selected');
    const card = document.getElementById('prefilledModelCard');
    if (id) {
      document.getElementById(`mc_${id}`)?.classList.add('selected');
      if (card && !card.querySelector('strong')) {
        // Only clear prefilled card if it's empty (model card not yet shown)
      }
    } else {
      document.getElementById('noModelOption')?.classList.add('selected');
      if (card) card.style.display = 'none';
    }
  }

  /* ─── Validation helpers ──────────────────────────── */
  function clearErrors() {
    document.querySelectorAll('.field-error-msg').forEach(el => el.remove());
    document.querySelectorAll('.form-control[data-error]').forEach(el => {
      el.style.borderColor = '';
      el.removeAttribute('data-error');
    });
    document.querySelectorAll('.service-option-card[data-error]').forEach(el => {
      el.style.borderColor = '';
      el.removeAttribute('data-error');
    });
  }

  function markError(id, msg) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.borderColor = 'var(--error, #e05c5c)';
    el.setAttribute('data-error', '1');
    el.parentNode.querySelectorAll('.field-error-msg').forEach(n => n.remove());
    const hint = document.createElement('div');
    hint.className = 'field-error-msg';
    hint.style.cssText = 'color:var(--error,#e05c5c);font-size:0.78rem;margin-top:4px';
    hint.setAttribute('role', 'alert');
    hint.textContent = msg;
    el.insertAdjacentElement('afterend', hint);
    el.addEventListener('input', () => {
      el.style.borderColor = '';
      el.removeAttribute('data-error');
      hint.remove();
    }, { once: true });
  }

  function markServiceError(msg) {
    const wrap = document.getElementById('serviceOptions');
    if (!wrap) return;
    wrap.querySelectorAll('.field-error-msg').forEach(n => n.remove());
    const hint = document.createElement('div');
    hint.className = 'field-error-msg';
    hint.style.cssText = 'color:var(--error,#e05c5c);font-size:0.78rem;margin-top:8px';
    hint.setAttribute('role', 'alert');
    hint.textContent = msg;
    wrap.appendChild(hint);
  }

  /* ─── Restore step fields from state ─────────────── */
  function restoreStep(n) {
    if (n === 2) {
      // Restore service card selection
      if (state.event_type) {
        document.querySelectorAll('.service-option-card').forEach(c => {
          c.classList.toggle('selected', c.dataset.value === state.event_type);
          c.setAttribute('aria-checked', c.dataset.value === state.event_type ? 'true' : 'false');
        });
      }
      const dateEl = document.getElementById('event_date');
      if (dateEl) { dateEl.value = state.event_date || ''; dateEl.min = today; }
      document.getElementById('event_duration').value = state.event_duration || '4';
      document.getElementById('location').value = state.location || '';
      document.getElementById('budget').value = state.budget || '';
      const comm = document.getElementById('comments');
      if (comm) {
        comm.value = state.comments || '';
        const cc = document.getElementById('charCount');
        if (cc) cc.textContent = comm.value.length;
      }
    }
    if (n === 3) {
      document.getElementById('client_name').value = state.client_name || '';
      const ph = document.getElementById('client_phone');
      if (ph) ph.value = state.client_phone || '';
      document.getElementById('client_email').value = state.client_email || '';
      document.getElementById('client_telegram').value = state.client_telegram ? '@' + state.client_telegram : '';
    }
  }

  /* ─── Phone digit count helper ───────────────────── */
  function countDigits(str) {
    return (str.match(/\d/g) || []).length;
  }

  function nextStep() {
    clearErrors();

    if (state.step === 2) {
      // Service type from radio cards
      const selectedCard = document.querySelector('.service-option-card.selected');
      if (!selectedCard) {
        markServiceError('Выберите тип мероприятия');
        toast('Выберите тип мероприятия', 'error');
        return;
      }
      state.event_type = selectedCard.dataset.value;
      state.event_date = document.getElementById('event_date').value;
      state.event_duration = document.getElementById('event_duration').value;
      state.location = document.getElementById('location').value;
      state.budget = document.getElementById('budget').value;
      state.comments = document.getElementById('comments').value;
    }

    if (state.step === 3) {
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
      } else if (countDigits(phone) < 10) {
        markError('client_phone', 'Номер должен содержать не менее 10 цифр');
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
      state.client_telegram = document.getElementById('client_telegram').value.trim().replace(/^@/, '');
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
    // Update progress bar fill
    const pct = Math.min(((n - 1) / 3) * 100, 100);
    const progressFill = document.getElementById('progressFill');
    if (progressFill) progressFill.style.width = pct + '%';
    for (let i = 1; i <= 3; i++) {
      const line = document.getElementById(`line${i}`);
      if (line) line.classList.toggle('done', i < n);
    }
    state.step = n;
    document.getElementById(`step${n}`)?.classList.add('active');
    restoreStep(n);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* ─── Service card selection ─────────────────────── */
  function selectService(value) {
    document.querySelectorAll('.service-option-card').forEach(c => {
      const isThis = c.dataset.value === value;
      c.classList.toggle('selected', isThis);
      c.setAttribute('aria-checked', isThis ? 'true' : 'false');
    });
    state.event_type = value;
    // Clear any service error
    document.querySelectorAll('#serviceOptions .field-error-msg').forEach(n => n.remove());
  }

  /* ─── Build confirmation summary ─────────────────── */
  function buildSummary() {
    const summaryEl = document.getElementById('orderSummary');
    if (!summaryEl) return;

    let modelHTML = '';
    if (state.model_id && state.model_photo) {
      modelHTML = `
        <div class="summary-model-card">
          <img src="${state.model_photo}" alt="${escHtml(state.model_name)}" />
          <span>${escHtml(state.model_name)}</span>
        </div>`;
    }

    summaryEl.innerHTML = `
      ${modelHTML}
      <div class="summary-row"><label>Модель</label><span>${escHtml(state.model_name || 'Менеджер подберёт')}</span></div>
      <div class="summary-row"><label>Мероприятие</label><span>${escHtml(EVENT_ICONS[state.event_type] || '')} ${escHtml(EVENT_LABELS[state.event_type] || state.event_type)}</span></div>
      ${state.event_date ? `<div class="summary-row"><label>Дата</label><span>${formatDate(state.event_date)}</span></div>` : ''}
      <div class="summary-row"><label>Продолжительность</label><span>${state.event_duration} ч</span></div>
      ${state.location ? `<div class="summary-row"><label>Место</label><span>${escHtml(state.location)}</span></div>` : ''}
      ${state.budget ? `<div class="summary-row"><label>Бюджет</label><span>${escHtml(state.budget)}</span></div>` : ''}
      <div class="summary-divider"></div>
      <div class="summary-row"><label>Имя</label><span>${escHtml(state.client_name)}</span></div>
      <div class="summary-row"><label>Телефон</label><span>${escHtml(state.client_phone)}</span></div>
      ${state.client_email ? `<div class="summary-row"><label>Email</label><span>${escHtml(state.client_email)}</span></div>` : ''}
      ${state.client_telegram ? `<div class="summary-row"><label>Telegram</label><span>@${escHtml(state.client_telegram)}</span></div>` : ''}
      ${state.comments ? `<div class="summary-row summary-row--block"><label>Пожелания</label><span>${escHtml(state.comments)}</span></div>` : ''}`;
  }

  function formatDate(d) {
    if (!d) return '';
    const [y, m, day] = d.split('-');
    const months = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
    return `${+day} ${months[+m - 1]} ${y}`;
  }

  function escHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* ─── Submit ──────────────────────────────────────── */
  async function submit() {
    const btn = document.getElementById('submitBtn');
    btn.disabled = true;
    btn.textContent = 'Отправка...';
    try {
      const body = {
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
      };

      // Attach UTM parameters if available
      if (window.NM && NM.analytics) {
        const utm = NM.analytics.getSavedUTM();
        if (utm.source) {
          body.utm_source = utm.source;
          body.utm_medium = utm.medium;
          body.utm_campaign = utm.campaign;
        }
      }

      const result = await apiFetch('/orders', {
        method: 'POST',
        body: JSON.stringify(body)
      });

      // Analytics: track booking conversion
      if (window.NM && NM.analytics) {
        NM.analytics.event('booking_submitted', {
          event_type: state.event_type,
          model_id: state.model_id,
          ...NM.analytics.getSavedUTM()
        });
      }

      const orderNum = result.order_number;
      document.getElementById('orderNumDisplay').textContent = orderNum;
      // Update link to status page with order number
      const statusLink = document.getElementById('statusPageLink');
      if (statusLink) statusLink.href = `/order-status.html?number=${encodeURIComponent(orderNum)}`;

      // Haptic success feedback in Telegram Mini App
      window._tgHaptic?.success();

      // Notify Telegram Mini App if running inside it
      if (window._tgWebAppOnBookingSuccess) {
        window._tgWebAppOnBookingSuccess(orderNum);
      }

      // Telegram connect link
      const tgLink = document.getElementById('tgConnectLink');
      const tgBox = document.getElementById('tgConnectBox');
      if (state.client_telegram) {
        if (tgBox) tgBox.style.display = 'none';
      } else {
        if (tgLink) tgLink.href = `https://t.me/${BOT_USERNAME}?start=${orderNum}`;
      }

      // Animate step indicators to done
      document.querySelectorAll('.booking-step').forEach(s => s.classList.add('done'));
      for (let i = 1; i <= 3; i++) document.getElementById(`line${i}`)?.classList.add('done');
      const progressFill = document.getElementById('progressFill');
      if (progressFill) progressFill.style.width = '100%';

      // Show success step
      document.getElementById(`step${state.step}`)?.classList.remove('active');
      document.getElementById('step5').classList.add('active');
      window.scrollTo({ top: 0, behavior: 'smooth' });

      // Auto-redirect to homepage after 5 seconds
      const countdownEl = document.getElementById('redirectCountdown');
      let countdown = 5;
      if (countdownEl) {
        countdownEl.textContent = countdown;
        const timer = setInterval(() => {
          countdown--;
          countdownEl.textContent = countdown;
          if (countdown <= 0) {
            clearInterval(timer);
            window.location.href = '/';
          }
        }, 1000);
      }
    } catch (e) {
      toast(e.message || 'Ошибка при отправке заявки', 'error');
      btn.disabled = false;
      btn.textContent = 'Отправить заявку ✓';
    }
  }

  /* ─── Check order status ─────────────────────────── */
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
            <strong style="color:var(--gold)">${escHtml(o.order_number)}</strong>
            <span>${statusLabels[o.status] || escHtml(o.status)}</span>
          </div>
          <div style="font-size:0.85rem;color:var(--text-muted)">
            ${o.event_type ? `Мероприятие: ${escHtml(EVENT_LABELS[o.event_type] || o.event_type)}<br>` : ''}
            ${o.event_date ? `Дата: ${formatDate(o.event_date)}<br>` : ''}
            ${o.model_name ? `Модель: ${escHtml(o.model_name)}` : ''}
          </div>
        </div>`;
    } catch {
      resultEl.innerHTML = '<span style="color:var(--error)">Заявка не найдена. Проверьте номер.</span>';
    }
  }

  window._booking = { nextStep, prevStep, selectModel, selectService, submit, checkStatus };
})();
