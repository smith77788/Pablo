/* ─── Booking form logic — 5-step multi-step form ────────────────────── */
(function () {
  let BOT_USERNAME = '';
  fetch('/api/config').then(r => r.json()).then(cfg => { BOT_USERNAME = cfg.bot_username || ''; }).catch(() => {});

  const DRAFT_KEY = 'nm_booking_draft';
  const TOTAL_STEPS = 5; // steps 1–5, step 6 is the success screen

  /* ─── Persist / restore draft from sessionStorage ─── */
  function saveDraft() {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
        step: state.step,
        model_id: state.model_id,
        model_ids: state.model_ids,
        model_name: state.model_name,
        model_photo: state.model_photo,
        selected_models: state.selected_models,
        event_type: state.event_type,
        event_date: state.event_date,
        event_duration: state.event_duration,
        location: state.location,
        budget: state.budget,
        comments: state.comments,
        client_name: state.client_name,
        client_phone: state.client_phone,
        client_email: state.client_email,
        client_telegram: state.client_telegram,
      }));
    } catch (_) {}
  }

  function loadDraft() {
    try {
      const raw = sessionStorage.getItem(DRAFT_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_) { return null; }
  }

  function clearDraft() {
    try { sessionStorage.removeItem(DRAFT_KEY); } catch (_) {}
  }

  const _draft = loadDraft();

  const state = {
    step: 1,
    model_id:        _draft?.model_id        ?? null,
    model_ids:       _draft?.model_ids        ?? [],
    model_name:      _draft?.model_name      ?? null,
    model_photo:     _draft?.model_photo     ?? null,
    selected_models: _draft?.selected_models  ?? [],  // array of {id, name, photo}
    event_type:      _draft?.event_type      ?? '',
    event_date:      _draft?.event_date      ?? '',
    event_duration:  _draft?.event_duration  ?? '4',
    location:        _draft?.location        ?? '',
    budget:          _draft?.budget          ?? '',
    comments:        _draft?.comments        ?? '',
    client_name:     _draft?.client_name     ?? '',
    client_phone:    _draft?.client_phone    ?? '',
    client_email:    _draft?.client_email    ?? '',
    client_telegram: _draft?.client_telegram ?? '',
    tor_file:        null,  // File object — not persisted in sessionStorage
  };

  const EVENT_LABELS = {
    photo_shoot:  'Фотосессия',
    event:        'Мероприятие',
    fashion_show: 'Показ мод',
    commercial:   'Реклама / Коммерческая съёмка',
    runway:       'Подиум',
    other:        'Другое'
  };

  const EVENT_ICONS = {
    photo_shoot:  '👗',
    event:        '🎪',
    fashion_show: '👠',
    commercial:   '📢',
    runway:       '✦',
    other:        '💍'
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
      let digits = val.replace(/\D/g, '');
      if (digits.startsWith('8')) digits = '7' + digits.slice(1);
      if (!digits.startsWith('7')) digits = '7' + digits;
      digits = digits.slice(0, 11);
      const d = digits.slice(1);
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

    phoneEl.addEventListener('input', () => {
      const selStart = phoneEl.selectionStart;
      const oldVal = phoneEl.value;
      const newVal = formatPhone(oldVal);
      phoneEl.value = newVal;
      const diff = newVal.length - oldVal.length;
      try { phoneEl.setSelectionRange(selStart + diff, selStart + diff); } catch (_) {}
      clearFieldError('client_phone');
    });

    phoneEl.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && phoneEl.value.length <= 4) {
        phoneEl.value = '';
        e.preventDefault();
      }
    });

    phoneEl.addEventListener('blur', () => {
      if (phoneEl.value === '+7 (' || phoneEl.value === '+7') phoneEl.value = '';
      validatePhoneField();
    });
  }

  /* ─── Phone validation on blur ───────────────────── */
  function validatePhoneField() {
    const ph = document.getElementById('client_phone');
    if (!ph) return;
    const val = ph.value.trim();
    if (!val) return;
    if (countDigits(val) < 10) {
      markError('client_phone', 'Номер должен содержать не менее 10 цифр');
    } else {
      clearFieldError('client_phone');
    }
  }

  /* ─── Real-time email validation ────────────────── */
  const emailEl = document.getElementById('client_email');
  if (emailEl) {
    let emailTimeout;
    const emailHint = document.createElement('div');
    emailHint.className = 'field-inline-hint';
    emailHint.style.cssText = 'font-size:0.72rem;margin-top:5px;line-height:1.5;';
    emailEl.parentNode.appendChild(emailHint);

    function checkEmail() {
      const val = emailEl.value.trim();
      if (!val) {
        emailHint.textContent = '';
        emailEl.style.borderColor = '';
        return;
      }
      const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(val);
      emailHint.style.color = valid ? 'var(--gold)' : '#e05c5c';
      emailHint.textContent = valid ? '✓ Email корректный' : '✕ Проверьте формат email (например: name@mail.ru)';
      emailEl.style.borderColor = valid ? 'rgba(201,169,110,0.4)' : 'var(--error, #e05c5c)';
    }

    emailEl.addEventListener('input', () => { clearTimeout(emailTimeout); emailTimeout = setTimeout(checkEmail, 400); });
    emailEl.addEventListener('blur', () => { clearTimeout(emailTimeout); checkEmail(); });
  }

  /* ─── Budget: positive-number validation on blur ─── */
  const budgetEl = document.getElementById('budget');
  if (budgetEl) {
    budgetEl.addEventListener('blur', () => {
      const val = budgetEl.value.trim();
      if (!val) return;
      const num = parseFloat(val.replace(/[^\d.]/g, ''));
      if (isNaN(num) || num <= 0) {
        markError('budget', 'Введите корректный положительный бюджет');
      } else {
        clearFieldError('budget');
      }
    });
    budgetEl.addEventListener('input', () => clearFieldError('budget'));
  }

  /* ─── Event date: validate future on blur ─────── */
  if (dateInput) {
    dateInput.addEventListener('blur', () => {
      const val = dateInput.value;
      if (!val) return;
      if (val < today) {
        markError('event_date', 'Дата мероприятия должна быть в будущем');
      } else {
        clearFieldError('event_date');
      }
    });
    dateInput.addEventListener('input', () => clearFieldError('event_date'));
  }

  /* ─── Client name: real-time validation ─────────── */
  const nameEl = document.getElementById('client_name');
  if (nameEl) {
    nameEl.addEventListener('blur', () => {
      const val = nameEl.value.trim();
      if (val && val.length < 2) {
        markError('client_name', 'Имя должно содержать не менее 2 символов');
      } else {
        clearFieldError('client_name');
      }
    });
    nameEl.addEventListener('input', () => clearFieldError('client_name'));
  }

  /* ─── TOR file input ──────────────────────────── */
  const torFileInput = document.getElementById('tor_file');
  const torFileName = document.getElementById('tor_file_name');
  if (torFileInput) {
    torFileInput.addEventListener('change', () => {
      const file = torFileInput.files[0];
      if (!file) {
        state.tor_file = null;
        if (torFileName) torFileName.textContent = '';
        return;
      }
      // Validate type
      const allowed = ['application/pdf', 'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document'];
      if (!allowed.includes(file.type) && !/\.(pdf|doc|docx)$/i.test(file.name)) {
        markError('tor_file', 'Допустимые форматы: PDF, DOC, DOCX');
        torFileInput.value = '';
        state.tor_file = null;
        if (torFileName) torFileName.textContent = '';
        return;
      }
      // Validate size (5MB)
      if (file.size > 5 * 1024 * 1024) {
        markError('tor_file', 'Файл слишком большой. Максимум 5 МБ');
        torFileInput.value = '';
        state.tor_file = null;
        if (torFileName) torFileName.textContent = '';
        return;
      }
      clearFieldError('tor_file');
      state.tor_file = file;
      if (torFileName) {
        torFileName.textContent = '📎 ' + file.name + ' (' + (file.size / 1024).toFixed(0) + ' КБ)';
      }
    });
  }

  /* ─── Auto-save on input for step 3 & 4 fields ─────── */
  (function attachAutoSave() {
    const step3Fields = ['event_date', 'event_duration', 'location', 'budget', 'comments'];
    const step4Fields = ['client_name', 'client_phone', 'client_email', 'client_telegram'];
    let saveTimer;
    function debouncedSave() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        if (state.step === 3) {
          state.event_date     = document.getElementById('event_date')?.value     || state.event_date;
          state.event_duration = document.getElementById('event_duration')?.value || state.event_duration;
          state.location       = document.getElementById('location')?.value       ?? state.location;
          state.budget         = document.getElementById('budget')?.value         ?? state.budget;
          state.comments       = document.getElementById('comments')?.value       ?? state.comments;
        }
        if (state.step === 4) {
          state.client_name     = document.getElementById('client_name')?.value.trim()  || state.client_name;
          state.client_phone    = document.getElementById('client_phone')?.value.trim() || state.client_phone;
          state.client_email    = document.getElementById('client_email')?.value.trim() || state.client_email;
          state.client_telegram = (document.getElementById('client_telegram')?.value.trim() || '').replace(/^@/, '') || state.client_telegram;
        }
        saveDraft();
      }, 600);
    }
    [...step3Fields, ...step4Fields].forEach(id => {
      const el = document.getElementById(id);
      if (el) {
        el.addEventListener('input', debouncedSave);
        if (el.tagName === 'SELECT') el.addEventListener('change', debouncedSave);
      }
    });
  })();

  /* ─── Load models for selector ───────────────────── */
  apiFetch('/models?available=1').then(models => {
    const grid = document.getElementById('modelSelectGrid');
    if (!grid) return;
    const subset = models.slice(0, 8);
    grid.innerHTML = subset.map(m => `
      <div class="model-select-card" id="mc_${m.id}" role="button" tabindex="0"
           aria-label="${escHtml(m.name)}"
           onclick="_booking.toggleModel(${m.id}, '${escHtml(m.name)}', '${m.photo_main || ''}')"
           onkeydown="if(event.key==='Enter'||event.key===' ')_booking.toggleModel(${m.id}, '${escHtml(m.name)}', '${m.photo_main || ''}')">
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

    initFromUrlParams();
    restoreModelFromDraft();
  }).catch(() => {
    initFromUrlParams();
    restoreModelFromDraft();
  });

  /* ─── Render selected model chips ───────────────── */
  function renderModelChips() {
    const chipsEl = document.getElementById('selectedModelChips');
    if (!chipsEl) return;

    if (state.selected_models.length === 0) {
      chipsEl.innerHTML = '';
      chipsEl.style.display = 'none';
      return;
    }

    chipsEl.style.display = 'flex';
    chipsEl.innerHTML = state.selected_models.map(m => `
      <div class="model-chip" data-id="${m.id}">
        ${m.photo ? `<img src="${escHtml(m.photo)}" alt="${escHtml(m.name)}" class="model-chip-thumb" />` : ''}
        <span>${escHtml(m.name)}</span>
        <button type="button" class="model-chip-remove" aria-label="Убрать ${escHtml(m.name)}"
          onclick="_booking.removeModel(${m.id})">✕</button>
      </div>`).join('');

    // Update noModelOption visibility
    const noOpt = document.getElementById('noModelOption');
    if (noOpt) {
      noOpt.classList.toggle('selected', state.selected_models.length === 0);
    }
  }

  /* ─── Toggle a model in multi-select ──────────── */
  function toggleModel(id, name, photo) {
    const existingIdx = state.selected_models.findIndex(m => m.id === id);
    if (existingIdx >= 0) {
      // Deselect
      state.selected_models.splice(existingIdx, 1);
    } else {
      // Select
      state.selected_models.push({ id, name, photo });
    }

    // Sync legacy model_id/name/photo to first selected (for backward compat)
    if (state.selected_models.length > 0) {
      state.model_id    = state.selected_models[0].id;
      state.model_name  = state.selected_models[0].name;
      state.model_photo = state.selected_models[0].photo;
    } else {
      state.model_id    = null;
      state.model_name  = null;
      state.model_photo = null;
    }

    state.model_ids = state.selected_models.map(m => m.id);

    // Update card UI
    document.querySelectorAll('.model-select-card').forEach(c => {
      const cid = parseInt(c.id.replace('mc_', ''), 10);
      const isSelected = state.selected_models.some(m => m.id === cid);
      c.classList.toggle('selected', isSelected);
      c.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
    });

    // noModelOption
    const noOpt = document.getElementById('noModelOption');
    if (noOpt) noOpt.classList.toggle('selected', state.selected_models.length === 0);

    renderModelChips();
    saveDraft();

    // Clear prefilled card if user manually deselects a pre-filled model
    if (state.selected_models.length === 0) {
      const card = document.getElementById('prefilledModelCard');
      if (card) card.style.display = 'none';
    }
  }

  /* ─── Remove a model from selection ─────────── */
  function removeModel(id) {
    const idx = state.selected_models.findIndex(m => m.id === id);
    if (idx >= 0) state.selected_models.splice(idx, 1);

    state.model_ids = state.selected_models.map(m => m.id);
    if (state.selected_models.length > 0) {
      state.model_id    = state.selected_models[0].id;
      state.model_name  = state.selected_models[0].name;
      state.model_photo = state.selected_models[0].photo;
    } else {
      state.model_id = null; state.model_name = null; state.model_photo = null;
    }

    // Update card visual
    const card = document.getElementById(`mc_${id}`);
    if (card) {
      card.classList.remove('selected');
      card.setAttribute('aria-pressed', 'false');
    }
    const noOpt = document.getElementById('noModelOption');
    if (noOpt) noOpt.classList.toggle('selected', state.selected_models.length === 0);

    renderModelChips();
    saveDraft();
  }

  /* ─── Legacy selectModel (kept for compatibility) ── */
  function selectModel(id, name, photo) {
    if (!id) {
      // "No model" selected
      state.selected_models = [];
      state.model_id    = null;
      state.model_name  = null;
      state.model_photo = null;
      state.model_ids   = [];
      document.querySelectorAll('.model-select-card').forEach(c => {
        c.classList.remove('selected');
        c.setAttribute('aria-pressed', 'false');
      });
      document.getElementById('noModelOption')?.classList.add('selected');
      const card = document.getElementById('prefilledModelCard');
      if (card) card.style.display = 'none';
      renderModelChips();
      saveDraft();
      return;
    }

    // Add as single selection (replacing existing for pre-fill compat)
    if (!state.selected_models.some(m => m.id === id)) {
      state.selected_models.push({ id, name: name || '', photo: photo || '' });
    }
    state.model_id    = id;
    state.model_name  = name  || null;
    state.model_photo = photo || null;
    state.model_ids   = state.selected_models.map(m => m.id);

    document.querySelectorAll('.model-select-card').forEach(c => {
      const cid = parseInt(c.id.replace('mc_', ''), 10);
      const isSel = state.selected_models.some(m => m.id === cid);
      c.classList.toggle('selected', isSel);
      c.setAttribute('aria-pressed', isSel ? 'true' : 'false');
    });
    document.getElementById('noModelOption')?.classList.remove('selected');
    renderModelChips();
    saveDraft();
  }

  /* ─── Restore model selection from draft ─── */
  function restoreModelFromDraft() {
    if (!_draft) return;
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('model') || urlParams.get('model_id')) return;

    if (_draft.selected_models && _draft.selected_models.length > 0) {
      _draft.selected_models.forEach(m => {
        const card = document.getElementById(`mc_${m.id}`);
        if (card) {
          card.classList.add('selected');
          card.setAttribute('aria-pressed', 'true');
        }
      });
      document.getElementById('noModelOption')?.classList.remove('selected');
      renderModelChips();
    } else if (_draft.model_id) {
      const card = document.getElementById(`mc_${_draft.model_id}`);
      if (card) {
        card.classList.add('selected');
        card.setAttribute('aria-pressed', 'true');
        document.getElementById('noModelOption')?.classList.remove('selected');
      }
    }

    const savedStep = (_draft.step && _draft.step > 1) ? _draft.step : 1;
    if (savedStep > 1) showDraftBanner(savedStep);
  }

  /* ─── Draft restore banner ──────────────────────────── */
  function showDraftBanner(savedStep) {
    const wrap = document.querySelector('.booking-form-wrap');
    if (!wrap) return;
    const banner = document.createElement('div');
    banner.id = 'draftBanner';
    banner.style.cssText = 'background:rgba(201,169,110,0.1);border:1px solid var(--gold);padding:12px 20px;margin-bottom:16px;font-size:0.82rem;display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--text-muted);';
    banner.innerHTML = `
      <span>📋 Найден незаполненный черновик. <strong style="color:var(--gold)">Продолжить с шага ${savedStep}?</strong></span>
      <span style="display:flex;gap:8px;flex-shrink:0">
        <button id="draftResume" style="background:var(--gold);color:var(--bg);border:none;padding:6px 14px;font-size:0.78rem;cursor:pointer;font-family:inherit;">Продолжить</button>
        <button id="draftDiscard" style="background:none;border:1px solid var(--border);color:var(--text-muted);padding:6px 14px;font-size:0.78rem;cursor:pointer;font-family:inherit;">Начать заново</button>
      </span>`;
    wrap.insertBefore(banner, wrap.firstChild);

    document.getElementById('draftResume').addEventListener('click', () => {
      banner.remove();
      goToStepInstant(savedStep);
    });
    document.getElementById('draftDiscard').addEventListener('click', () => {
      clearDraft();
      Object.assign(state, {
        model_id: null, model_ids: [], model_name: null, model_photo: null,
        selected_models: [],
        event_type: '', event_date: '', event_duration: '4',
        location: '', budget: '', comments: '',
        client_name: '', client_phone: '', client_email: '', client_telegram: '',
        tor_file: null,
      });
      banner.remove();
      goToStepInstant(1);
    });
  }

  /* ─── Navigate to step without animation (for restore) ─ */
  function goToStepInstant(n) {
    document.querySelectorAll('.booking-section').forEach(s => {
      s.classList.remove('active', 'slide-out', 'slide-in-back', 'slide-out-back');
    });
    document.getElementById(`step${n}`)?.classList.add('active');
    updateStepIndicators(n);
    state.step = n;
    restoreStep(n);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* ─── Update step indicator dots and progress bar ─── */
  function updateStepIndicators(n) {
    document.querySelectorAll('.booking-step').forEach(s => {
      const sn = +s.dataset.step;
      s.classList.remove('active', 'done');
      if (sn < n) s.classList.add('done');
      else if (sn === n) s.classList.add('active');
      if (s.hasAttribute('aria-current')) s.removeAttribute('aria-current');
      if (sn === n) s.setAttribute('aria-current', 'step');
    });

    // Progress bar: (n-1)/(TOTAL_STEPS-1) fills across 5 steps
    const pct = Math.min(((n - 1) / (TOTAL_STEPS - 1)) * 100, 100);
    const progressFill = document.getElementById('progressFill');
    if (progressFill) {
      progressFill.style.width = pct + '%';
      const bar = progressFill.parentElement;
      if (bar) bar.setAttribute('aria-valuenow', Math.round(pct));
    }

    // Connector lines — there are 4 lines (line1–line4)
    for (let i = 1; i <= 4; i++) {
      const line = document.getElementById(`line${i}`);
      if (line) line.classList.toggle('done', i < n);
    }
  }

  /* ─── Parse URL params & pre-select model / event type / city ─── */
  function initFromUrlParams() {
    const urlParams = new URLSearchParams(window.location.search);
    const urlModel     = urlParams.get('model') || urlParams.get('model_id');
    const urlModelName = urlParams.get('model_name');
    const urlEventType = urlParams.get('event_type');
    const urlCity      = urlParams.get('city');
    const urlBudget    = urlParams.get('budget');

    // Show selected model banner if model_name is passed
    if (urlModelName) {
      const banner = document.getElementById('selected-model-banner');
      if (banner) {
        banner.textContent = `📸 Выбрана модель: ${decodeURIComponent(urlModelName)}`;
        banner.style.display = 'block';
      }
    }

    // Pre-fill city / location
    if (urlCity) {
      const locEl = document.getElementById('location');
      if (locEl && !locEl.value) {
        locEl.value = decodeURIComponent(urlCity);
        state.location = locEl.value;
        saveDraft();
      }
    }

    // Pre-fill budget from URL (e.g. redirected from pricing calculator)
    if (urlBudget) {
      const budgetNum = parseFloat(urlBudget.replace(/[^\d.]/g, ''));
      if (budgetNum > 0) {
        // Restore sessionStorage saved calc if available and budget matches
        let calcLabel = urlBudget;
        try {
          const saved = JSON.parse(sessionStorage.getItem('nm_calc_save') || 'null');
          if (saved && saved.price) calcLabel = saved.price;
        } catch {}
        state.budget = calcLabel;
        // Will be rendered when step 3 is shown; also apply immediately if field exists
        const budgetEl = document.getElementById('budget');
        if (budgetEl && !budgetEl.value) budgetEl.value = calcLabel;
        saveDraft();

        // Show a subtle banner when redirected from pricing page
        if (urlParams.get('from') === 'pricing' || urlParams.has('budget')) {
          const wrap = document.querySelector('.booking-form-wrap') || document.body;
          if (wrap && !document.getElementById('pricingCalcBanner')) {
            const banner = document.createElement('div');
            banner.id = 'pricingCalcBanner';
            banner.style.cssText = 'background:rgba(201,169,110,0.1);border:1px solid rgba(201,169,110,0.25);color:var(--gold);font-size:0.8rem;padding:10px 16px;margin-bottom:16px;border-radius:2px;text-align:center';
            banner.textContent = `💰 Расчётная стоимость из калькулятора: ${calcLabel} ₽`;
            wrap.insertBefore(banner, wrap.firstChild);
          }
        }
      }
    }

    // Pre-select event type from URL param — skip step 1 if valid
    if (urlEventType && EVENT_LABELS[urlEventType]) {
      selectService(urlEventType);
      // Navigate directly to step 2 (model selection) if no other draft step
      if (!_draft || _draft.step <= 1) {
        goToStepInstant(2);
      }
    }

    if (!urlModel) return;

    // Pre-select model from URL param
    apiFetch(`/models/${urlModel}`).then(m => {
      selectModel(m.id, m.name, m.photo_main || '');
      showModelInfoCard(m);
      // If event type was also given, skip to step 3
      if (urlEventType && EVENT_LABELS[urlEventType]) {
        if (!_draft || _draft.step <= 2) goToStepInstant(3);
      }
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

  function clearFieldError(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.borderColor = '';
    el.removeAttribute('data-error');
    el.parentNode.querySelectorAll('.field-error-msg').forEach(n => n.remove());
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
    if (n === 1) {
      if (state.event_type) {
        document.querySelectorAll('.service-option-card').forEach(c => {
          const isThis = c.dataset.value === state.event_type;
          c.classList.toggle('selected', isThis);
          c.setAttribute('aria-checked', isThis ? 'true' : 'false');
        });
      }
    }
    if (n === 2) {
      // Restore multi-model highlight
      document.querySelectorAll('.model-select-card').forEach(c => {
        const cid = parseInt(c.id.replace('mc_', ''), 10);
        const isSel = state.selected_models.some(m => m.id === cid);
        c.classList.toggle('selected', isSel);
        c.setAttribute('aria-pressed', isSel ? 'true' : 'false');
      });
      document.getElementById('noModelOption')?.classList.toggle('selected', state.selected_models.length === 0);
      renderModelChips();
    }
    if (n === 3) {
      const dateEl = document.getElementById('event_date');
      if (dateEl) { dateEl.value = state.event_date || ''; dateEl.min = today; }
      const durEl = document.getElementById('event_duration');
      if (durEl) durEl.value = state.event_duration || '4';
      const budgetEl2 = document.getElementById('budget');
      if (budgetEl2) budgetEl2.value = state.budget || '';
      const locEl = document.getElementById('location');
      if (locEl) locEl.value = state.location || '';
      const comm = document.getElementById('comments');
      if (comm) {
        comm.value = state.comments || '';
        const cc = document.getElementById('charCount');
        if (cc) cc.textContent = comm.value.length;
      }
      // Restore TOR file display
      if (torFileName && state.tor_file) {
        torFileName.textContent = '📎 ' + state.tor_file.name;
      }
    }
    if (n === 4) {
      const nameEl2 = document.getElementById('client_name');
      if (nameEl2) nameEl2.value = state.client_name || '';
      const ph = document.getElementById('client_phone');
      if (ph) ph.value = state.client_phone || '';
      const emailEle = document.getElementById('client_email');
      if (emailEle) emailEle.value = state.client_email || '';
      const tgEl = document.getElementById('client_telegram');
      if (tgEl) tgEl.value = state.client_telegram ? '@' + state.client_telegram : '';
    }
  }

  /* ─── Phone digit count helper ───────────────────── */
  function countDigits(str) {
    return (str.match(/\d/g) || []).length;
  }

  /* ─── Next / Prev step ───────────────────────────── */
  function nextStep() {
    clearErrors();

    // Step 1 validation: event type must be selected
    if (state.step === 1) {
      const selectedCard = document.querySelector('.service-option-card.selected');
      if (!selectedCard) {
        markServiceError('Выберите тип события');
        toast('Выберите тип события', 'error');
        return;
      }
      state.event_type = selectedCard.dataset.value;
      saveDraft();
    }

    // Step 3 validation: date must be selected and in future
    if (state.step === 3) {
      const dateVal = document.getElementById('event_date')?.value || '';
      let hasError = false;
      if (!dateVal) {
        markError('event_date', 'Укажите дату мероприятия');
        hasError = true;
      } else if (dateVal < today) {
        markError('event_date', 'Дата мероприятия должна быть в будущем');
        hasError = true;
      }

      // Budget validation (if filled)
      const budgetVal = document.getElementById('budget')?.value.trim() || '';
      if (budgetVal) {
        const num = parseFloat(budgetVal.replace(/[^\d.]/g, ''));
        if (isNaN(num) || num <= 0) {
          markError('budget', 'Введите корректный положительный бюджет');
          hasError = true;
        }
      }

      if (hasError) { toast('Проверьте правильность заполнения полей', 'error'); return; }

      state.event_date     = dateVal;
      state.event_duration = document.getElementById('event_duration')?.value || '4';
      state.budget         = document.getElementById('budget')?.value         || '';
      state.location       = document.getElementById('location')?.value       || '';
      state.comments       = document.getElementById('comments')?.value       || '';
      saveDraft();
    }

    // Step 4 validation: name (min 2 chars) + phone (10+ digits)
    if (state.step === 4) {
      const name  = document.getElementById('client_name')?.value.trim()  || '';
      const phone = document.getElementById('client_phone')?.value.trim() || '';
      const email = document.getElementById('client_email')?.value.trim() || '';
      let hasError = false;

      if (name.length < 2) {
        markError('client_name', name ? 'Имя должно содержать не менее 2 символов' : 'Введите ваше имя');
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

      state.client_name     = name;
      state.client_phone    = phone;
      state.client_email    = email;
      state.client_telegram = (document.getElementById('client_telegram')?.value.trim() || '').replace(/^@/, '');
      saveDraft();
      buildSummary();
    }

    goToStep(state.step + 1);
  }

  function prevStep() { goToStep(state.step - 1, true); }

  function goToStep(n, isBack = false) {
    if (n < 1 || n > TOTAL_STEPS) return;
    const currentEl = document.getElementById(`step${state.step}`);
    const nextEl    = document.getElementById(`step${n}`);

    if (currentEl && currentEl !== nextEl) {
      const outClass = isBack ? 'slide-out-back' : 'slide-out';
      currentEl.classList.add(outClass);
      currentEl.addEventListener('animationend', () => {
        currentEl.classList.remove('active', 'slide-out', 'slide-out-back');
      }, { once: true });
    }

    updateStepIndicators(n);
    state.step = n;
    saveDraft();

    if (nextEl) {
      if (isBack) {
        nextEl.classList.add('slide-in-back');
        nextEl.addEventListener('animationend', () => {
          nextEl.classList.remove('slide-in-back');
          nextEl.classList.add('active');
        }, { once: true });
      } else {
        nextEl.classList.add('active');
      }
    }

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
    saveDraft();
    document.querySelectorAll('#serviceOptions .field-error-msg').forEach(n => n.remove());
  }

  /* ─── Build confirmation summary ─────────────────── */
  function buildSummary() {
    const summaryEl = document.getElementById('orderSummary');
    if (!summaryEl) return;

    // Build model display
    let modelHTML = '';
    if (state.selected_models.length > 0) {
      modelHTML = `<div class="summary-model-card">`;
      state.selected_models.forEach(m => {
        if (m.photo) {
          modelHTML += `<img src="${escHtml(m.photo)}" alt="${escHtml(m.name)}" style="width:48px;height:60px;object-fit:cover;flex-shrink:0;margin-right:8px;" />`;
        }
      });
      const names = state.selected_models.map(m => escHtml(m.name)).join(', ');
      modelHTML += `<span>${names}</span></div>`;
    } else if (state.model_id && state.model_photo) {
      modelHTML = `
        <div class="summary-model-card">
          <img src="${state.model_photo}" alt="${escHtml(state.model_name)}" />
          <span>${escHtml(state.model_name)}</span>
        </div>`;
    }

    const modelDisplay = state.selected_models.length > 0
      ? state.selected_models.map(m => escHtml(m.name)).join(', ')
      : escHtml(state.model_name || 'Менеджер подберёт');

    summaryEl.innerHTML = `
      ${modelHTML}
      <div class="summary-row">
        <label>Событие</label>
        <span>${escHtml(EVENT_ICONS[state.event_type] || '')} ${escHtml(EVENT_LABELS[state.event_type] || state.event_type)}</span>
        <button class="summary-edit-btn" onclick="window._booking.goToStepPublic(1)" title="Изменить">✎</button>
      </div>
      <div class="summary-row">
        <label>Модель${state.selected_models.length > 1 ? 'и' : ''}</label>
        <span>${modelDisplay}</span>
        <button class="summary-edit-btn" onclick="window._booking.goToStepPublic(2)" title="Изменить">✎</button>
      </div>
      ${state.event_date ? `<div class="summary-row"><label>Дата</label><span>${formatDate(state.event_date)}</span><button class="summary-edit-btn" onclick="window._booking.goToStepPublic(3)" title="Изменить">✎</button></div>` : ''}
      <div class="summary-row"><label>Продолжительность</label><span>${state.event_duration} ч</span></div>
      ${state.location ? `<div class="summary-row"><label>Место</label><span>${escHtml(state.location)}</span></div>` : ''}
      ${state.budget   ? `<div class="summary-row"><label>Бюджет</label><span>${escHtml(state.budget)}</span></div>` : ''}
      ${state.tor_file ? `<div class="summary-row"><label>ТЗ / Файл</label><span>📎 ${escHtml(state.tor_file.name)}</span></div>` : ''}
      <div class="summary-divider"></div>
      <div class="summary-row">
        <label>Имя</label>
        <span>${escHtml(state.client_name)}</span>
        <button class="summary-edit-btn" onclick="window._booking.goToStepPublic(4)" title="Изменить">✎</button>
      </div>
      <div class="summary-row"><label>Телефон</label><span>${escHtml(state.client_phone)}</span></div>
      ${state.client_email    ? `<div class="summary-row"><label>Email</label><span>${escHtml(state.client_email)}</span></div>` : ''}
      ${state.client_telegram ? `<div class="summary-row"><label>Telegram</label><span>@${escHtml(state.client_telegram)}</span></div>` : ''}
      ${state.comments ? `<div class="summary-row summary-row--block"><label>Пожелания</label><span>${escHtml(state.comments)}</span></div>` : ''}`;
  }

  function formatDate(d) {
    if (!d) return '';
    const [y, m, day] = d.split('-');
    const months = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];
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

  /* ─── CSRF token helper ──────────────────────────── */
  async function getCsrfToken() {
    try {
      const r = await fetch('/api/csrf-token');
      const d = await r.json();
      return d.token || '';
    } catch { return ''; }
  }

  /* ─── Submit ──────────────────────────────────────── */
  async function submit() {
    const btn = document.getElementById('submitBtn');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = 'Отправка...';
    try {
      const csrfToken = await getCsrfToken();

      // Use first model_id for backward compat with API, send model_ids as comma-separated too
      const modelIdForApi = state.selected_models.length > 0 ? state.selected_models[0].id : (state.model_id || null);
      const modelIdsStr   = state.model_ids.length > 1 ? state.model_ids.join(',') : null;

      const body = {
        client_name:     state.client_name,
        client_phone:    state.client_phone,
        client_email:    state.client_email    || null,
        client_telegram: state.client_telegram || null,
        model_id:        modelIdForApi,
        event_type:      state.event_type,
        event_date:      state.event_date       || null,
        event_duration:  +state.event_duration  || 4,
        location:        state.location         || null,
        budget:          state.budget           || null,
        comments:        state.comments         || null,
      };

      // Include additional model IDs in comments if multiple selected
      if (modelIdsStr) {
        const modelNames = state.selected_models.map(m => m.name).join(', ');
        body.comments = (body.comments ? body.comments + '\n\n' : '') + `[Выбранные модели: ${modelNames}]`;
      }

      // Attach UTM parameters if available
      if (window.NM && NM.analytics) {
        const utm = NM.analytics.getSavedUTM();
        if (utm.source) {
          body.utm_source   = utm.source;
          body.utm_medium   = utm.medium;
          body.utm_campaign = utm.campaign;
        }
      }

      let result;

      // If there's a TOR file, use FormData
      if (state.tor_file) {
        const formData = new FormData();
        Object.entries(body).forEach(([k, v]) => { if (v !== null && v !== undefined) formData.append(k, v); });
        formData.append('tor_file', state.tor_file, state.tor_file.name);
        formData.append('_csrf', csrfToken);
        const resp = await fetch('/api/orders', { method: 'POST', body: formData, headers: { 'x-csrf-token': csrfToken } });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.error || 'Ошибка при отправке заявки');
        }
        result = await resp.json();
      } else {
        result = await apiFetch('/orders', {
          method: 'POST',
          body: JSON.stringify(body),
          headers: { 'x-csrf-token': csrfToken },
        });
      }

      clearDraft();

      if (window.NM && NM.analytics) {
        NM.analytics.event('booking_submitted', {
          event_type: state.event_type,
          model_id:   state.model_id,
          model_count: state.selected_models.length,
          ...NM.analytics.getSavedUTM()
        });
      }

      const orderNum = result.order_number;
      const orderNumDisplay = document.getElementById('orderNumDisplay');
      if (orderNumDisplay) orderNumDisplay.textContent = orderNum;

      // Update status link with order number
      const statusLink = document.getElementById('statusPageLink');
      if (statusLink) statusLink.href = `/order-status.html?number=${encodeURIComponent(orderNum)}`;

      // "Book another model" button — reset to step 1
      const bookAnotherBtn = document.getElementById('bookAnotherBtn');
      if (bookAnotherBtn) {
        bookAnotherBtn.addEventListener('click', () => {
          clearDraft();
          Object.assign(state, {
            model_id: null, model_ids: [], model_name: null, model_photo: null,
            selected_models: [],
            event_type: '', event_date: '', event_duration: '4',
            location: '', budget: '', comments: '',
            client_name: '', client_phone: '', client_email: '', client_telegram: '',
            tor_file: null,
          });
          document.getElementById('step6')?.classList.remove('active');
          goToStepInstant(1);
        });
      }

      // Share booking confirmation
      const shareBtn = document.getElementById('shareBookingBtn');
      if (shareBtn) {
        const shareUrl = `${location.origin}/order-status.html?number=${encodeURIComponent(orderNum)}`;
        const shareText = `Моя заявка ${orderNum} — Nevesty Models`;
        if (navigator.share) {
          shareBtn.style.display = 'inline-flex';
          shareBtn.addEventListener('click', async () => {
            try { await navigator.share({ title: shareText, url: shareUrl }); } catch (_) {}
          });
        } else {
          shareBtn.style.display = 'inline-flex';
          shareBtn.title = 'Скопировать ссылку';
          shareBtn.addEventListener('click', () => {
            navigator.clipboard.writeText(shareUrl).then(() => {
              shareBtn.textContent = '✓ Скопировано';
              setTimeout(() => { shareBtn.textContent = '🔗 Поделиться'; }, 2000);
            }).catch(() => {});
          });
        }
      }

      const cabinetLink     = document.getElementById('cabinetLink');
      const cabinetPhoneHint = document.getElementById('cabinetPhoneHint');
      const cabinetPhoneLink = document.getElementById('cabinetPhoneLink');
      if (state.client_phone && cabinetLink) {
        const phoneDigits = state.client_phone.replace(/\D/g, '');
        cabinetLink.href = `/cabinet.html?phone=${encodeURIComponent(phoneDigits)}`;
        if (cabinetPhoneLink) cabinetPhoneLink.href = `/cabinet.html?phone=${encodeURIComponent(phoneDigits)}`;
        if (cabinetPhoneHint) cabinetPhoneHint.style.display = 'block';
      }

      window._tgHaptic?.success();
      if (window._tgWebAppOnBookingSuccess) window._tgWebAppOnBookingSuccess(orderNum);

      const tgLink = document.getElementById('tgConnectLink');
      const tgBox  = document.getElementById('tgConnectBox');
      if (state.client_telegram) {
        if (tgBox) tgBox.style.display = 'none';
      } else {
        if (tgLink) tgLink.href = `https://t.me/${BOT_USERNAME}?start=${orderNum}`;
      }

      // Mark all steps done
      document.querySelectorAll('.booking-step').forEach(s => s.classList.add('done'));
      for (let i = 1; i <= 4; i++) document.getElementById(`line${i}`)?.classList.add('done');
      const progressFill = document.getElementById('progressFill');
      if (progressFill) {
        progressFill.style.width = '100%';
        progressFill.parentElement?.setAttribute('aria-valuenow', '100');
      }

      // Show success step (step6)
      document.getElementById(`step${state.step}`)?.classList.remove('active');
      document.getElementById('step6')?.classList.add('active');
      window.scrollTo({ top: 0, behavior: 'smooth' });

      // Auto-redirect to homepage after 10 seconds (extended from 5 to give time to interact)
      const countdownEl = document.getElementById('redirectCountdown');
      let countdown = 10;
      if (countdownEl) {
        countdownEl.textContent = countdown;
        const timer = setInterval(() => {
          countdown--;
          countdownEl.textContent = countdown;
          if (countdown <= 0) { clearInterval(timer); window.location.href = '/'; }
        }, 1000);
        // Cancel redirect if user interacts with success page
        ['bookAnotherBtn', 'shareBookingBtn', 'statusPageLink', 'cabinetLink'].forEach(id => {
          document.getElementById(id)?.addEventListener('click', () => clearInterval(timer), { once: true });
        });
      }
    } catch (e) {
      if (typeof toast === 'function') toast(e.message || 'Ошибка при отправке заявки', 'error');
      btn.disabled = false;
      btn.textContent = 'Отправить заявку ✓';
    }
  }

  /* ─── Check order status ─────────────────────────── */
  async function checkStatus() {
    const num = document.getElementById('statusInput')?.value.trim().toUpperCase();
    if (!num) { if (typeof toast === 'function') toast('Введите номер заявки', 'error'); return; }
    const resultEl = document.getElementById('statusResult');
    if (resultEl) resultEl.innerHTML = '<span style="color:var(--text-muted)">Поиск...</span>';
    try {
      const o = await apiFetch(`/orders/status/${num}`);
      const statusLabels = {
        new: '🆕 Новая', reviewing: '🔍 На рассмотрении', confirmed: '✅ Подтверждена',
        in_progress: '▶️ В процессе', completed: '🏁 Завершена', cancelled: '❌ Отменена'
      };
      if (resultEl) resultEl.innerHTML = `
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
    } catch (_) {
      if (resultEl) resultEl.innerHTML = '<span style="color:var(--error)">Заявка не найдена. Проверьте номер.</span>';
    }
  }

  /* ─── Public API ─────────────────────────────────── */
  function goToStepPublic(n) { goToStep(n, n < state.step); }

  window._booking = {
    nextStep, prevStep, selectModel, selectService, submit, checkStatus,
    goToStepPublic, toggleModel, removeModel,
  };
})();
