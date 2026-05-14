/**
 * TelegramApp — Mini App controller for Nevesty Models
 * Used by webapp.html (dedicated Mini App entry point) and
 * injected on regular pages (booking.html, catalog.html, etc.)
 */

// ─────────────────────────────────────────────────────────────────────────────
// Class: TelegramApp (for webapp.html)
// ─────────────────────────────────────────────────────────────────────────────
class TelegramApp {
  constructor() {
    /** @type {import('@types/telegram-web-app').WebApp | null} */
    this.tg = window.Telegram?.WebApp || null;
    this.currentPage = 'home';
    this._mainButtonCallback = null;
    this._catalogFilter = 'all';
  }

  // ── init ──────────────────────────────────────────────────────────────────

  /**
   * Initialise the Telegram Web App:
   *  - signals readiness, expands viewport
   *  - applies Telegram theme colours as CSS variables
   *  - wires BackButton
   *  - prefills user greeting on home screen
   */
  init() {
    const tg = this.tg;
    if (!tg) {
      // Running outside Telegram — show a subtle notice and continue
      this._showOutsideTelegramBanner();
    } else {
      tg.ready();
      tg.expand();

      // Apply Telegram theme colours to CSS variables
      this._applyTheme(tg.themeParams);

      // Listen for theme changes (light/dark toggle inside Telegram)
      tg.onEvent('themeChanged', () => this._applyTheme(tg.themeParams));

      // BackButton: go home or close
      if (tg.BackButton) {
        tg.BackButton.onClick(() => {
          if (this.currentPage !== 'home') {
            this.navigate('home');
          } else {
            tg.close();
          }
        });
      }
    }

    // Render user greeting
    this._renderUser();

    // Pre-load catalog on first open so it feels instant
    this.loadCatalog('all');
  }

  // ── getUser ───────────────────────────────────────────────────────────────

  /**
   * Returns the Telegram user object from initDataUnsafe, or null.
   * @returns {{ id: number, first_name: string, last_name?: string, username?: string } | null}
   */
  getUser() {
    return this.tg?.initDataUnsafe?.user || null;
  }

  // ── navigate ──────────────────────────────────────────────────────────────

  /**
   * Shows the target page section and hides all others.
   * Also updates bottom-nav tabs, BackButton visibility, and MainButton.
   * @param {'home'|'catalog'|'booking'|'faq'} page
   */
  navigate(page) {
    const validPages = ['home', 'catalog', 'booking', 'faq'];
    if (!validPages.includes(page)) return;

    // Switch active page
    document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
    const target = document.getElementById(`page-${page}`);
    if (target) target.classList.add('active');

    // Scroll to top
    const pagesEl = document.getElementById('pages');
    if (pagesEl) pagesEl.scrollTop = 0;

    // Update bottom nav
    document.querySelectorAll('.nav-tab').forEach(tab => {
      tab.classList.toggle('active', tab.dataset.nav === page);
    });

    // Show bottom-nav & BackButton only when away from home
    const bottomNav = document.getElementById('bottom-nav');
    if (bottomNav) bottomNav.classList.toggle('visible', true);

    if (this.tg?.BackButton) {
      if (page === 'home') {
        this.tg.BackButton.hide();
      } else {
        this.tg.BackButton.show();
      }
    }

    // Page-specific logic
    if (page === 'booking') {
      this._setupBookingPage();
    } else {
      // Hide MainButton when leaving booking
      this.hideMainButton();
    }

    this.currentPage = page;

    // Haptic
    try { this.tg?.HapticFeedback?.impactOccurred('light'); } catch {}
  }

  // ── showMainButton ────────────────────────────────────────────────────────

  /**
   * Shows Telegram's native MainButton with given text and callback.
   * @param {string} text
   * @param {() => void} callback
   */
  showMainButton(text, callback) {
    const mb = this.tg?.MainButton;
    if (!mb) return;

    // Remove previous listener
    if (this._mainButtonCallback) {
      mb.offClick(this._mainButtonCallback);
    }
    this._mainButtonCallback = callback;

    mb.setText(text);
    mb.color = this._getVar('--gold') || '#C9A84C';
    mb.textColor = '#000000';
    mb.enable();
    mb.show();
    mb.onClick(this._mainButtonCallback);
  }

  /**
   * Hides the MainButton.
   */
  hideMainButton() {
    const mb = this.tg?.MainButton;
    if (!mb) return;
    if (this._mainButtonCallback) {
      mb.offClick(this._mainButtonCallback);
      this._mainButtonCallback = null;
    }
    mb.hide();
  }

  // ── close ─────────────────────────────────────────────────────────────────

  /**
   * Closes the Telegram Mini App.
   */
  close() {
    if (this.tg) {
      this.tg.close();
    } else {
      window.close();
    }
  }

  // ── loadCatalog ───────────────────────────────────────────────────────────

  /**
   * Loads models from the API and renders them in the grid.
   * @param {'all'|string} filter
   */
  async loadCatalog(filter = 'all') {
    this._catalogFilter = filter;
    const grid = document.getElementById('model-grid');
    if (!grid) return;

    // Show spinner
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;"><div class="spinner"></div></div>';

    try {
      const res = await fetch('/api/models?limit=20&status=active', {
        headers: { 'Accept': 'application/json' }
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const models = Array.isArray(data) ? data : (data.models || data.data || []);

      // Client-side filter by category tag if not 'all'
      const filtered = filter === 'all'
        ? models
        : models.filter(m => {
            const cats = [m.category, m.type, ...(m.tags || [])].map(t => String(t || '').toLowerCase());
            return cats.some(c => c.includes(filter));
          });

      if (filtered.length === 0) {
        grid.innerHTML = '<div style="grid-column:1/-1;padding:32px 0;text-align:center;color:var(--tg-hint);">Модели не найдены</div>';
        return;
      }

      grid.innerHTML = filtered.map(m => this._modelCardHTML(m)).join('');

      // Click → open model detail (opens full site)
      grid.querySelectorAll('.model-card').forEach(card => {
        card.addEventListener('click', () => {
          const id = card.dataset.id;
          if (id) this._openModelPage(id);
        });
      });

    } catch (err) {
      // Fallback: show placeholder cards
      grid.innerHTML = this._placeholderCards();
    }
  }

  // ── openFullCatalog ───────────────────────────────────────────────────────

  openFullCatalog() {
    const url = (window.SITE_URL || '') + '/catalog.html';
    if (this.tg) {
      try {
        this.tg.openLink(url);
        return;
      } catch {}
    }
    window.open(url, '_blank');
  }

  // ── submitBooking ─────────────────────────────────────────────────────────

  async submitBooking() {
    const submitBtn = document.getElementById('wb-submit');
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Отправляем…'; }
    this.tg?.MainButton?.showProgress(false);

    try {
      const payload = {
        client_name:   document.getElementById('wb-name')?.value.trim()  || '',
        client_phone:  document.getElementById('wb-phone')?.value.trim()  || '',
        event_type:    document.getElementById('wb-event-type')?.value    || '',
        event_date:    document.getElementById('wb-date')?.value          || '',
        comments:      document.getElementById('wb-comment')?.value.trim() || '',
        source:        'telegram_webapp',
        tg_user_id:    this.getUser()?.id || null,
      };

      // Basic validation
      if (!payload.client_name || !payload.client_phone || !payload.event_type || !payload.event_date) {
        this._showError('Пожалуйста, заполните все обязательные поля');
        return;
      }

      const res = await fetch('/api/orders', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify(payload),
      });

      const json = await res.json().catch(() => ({}));

      if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);

      // Success
      this._onBookingSuccess(json.order_number || json.orderNumber || '—');

    } catch (err) {
      this._showError(err.message || 'Ошибка отправки. Попробуйте снова.');
    } finally {
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Отправить заявку'; }
      this.tg?.MainButton?.hideProgress?.();
    }
  }

  // ── syncMainButton ────────────────────────────────────────────────────────

  /**
   * Called on every input change in the booking form.
   * Enables/disables MainButton based on form validity.
   */
  syncMainButton() {
    const filled = ['wb-name', 'wb-phone', 'wb-event-type', 'wb-date']
      .every(id => (document.getElementById(id)?.value || '').trim() !== '');

    const mb = this.tg?.MainButton;
    if (!mb || !mb.isVisible) return;

    if (filled) {
      mb.enable();
    } else {
      mb.disable();
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Private helpers
  // ─────────────────────────────────────────────────────────────────────────

  _applyTheme(params = {}) {
    if (!params || !Object.keys(params).length) return;
    const root = document.documentElement.style;
    const map = {
      '--tg-bg':       params.bg_color,
      '--tg-surface':  params.secondary_bg_color,
      '--tg-text':     params.text_color,
      '--tg-hint':     params.hint_color,
      '--tg-link':     params.link_color,
      '--tg-button':   params.button_color,
      '--tg-btn-text': params.button_text_color,
    };
    Object.entries(map).forEach(([k, v]) => { if (v) root.setProperty(k, v); });

    // Sync header/bg colour with Telegram's chrome
    try {
      if (params.bg_color) {
        this.tg?.setHeaderColor(params.bg_color);
        this.tg?.setBackgroundColor(params.bg_color);
      }
    } catch {}
  }

  _renderUser() {
    const user = this.getUser();
    const card    = document.getElementById('user-card');
    const nameEl  = document.getElementById('user-name-text');
    const initEl  = document.getElementById('user-avatar-initial');

    if (!card || !user) return;

    const fullName = [user.first_name, user.last_name].filter(Boolean).join(' ');
    if (nameEl) nameEl.textContent = fullName || user.username || 'Гость';
    if (initEl) initEl.textContent = (user.first_name || 'N')[0].toUpperCase();
    card.classList.add('visible');

    // Pre-fill booking form
    const nameInput = document.getElementById('wb-name');
    if (nameInput && fullName) nameInput.value = fullName;

    const tgInput = document.getElementById('wb-tg');
    if (tgInput && user.username) tgInput.value = user.username;
  }

  _setupBookingPage() {
    const notice = document.getElementById('booking-tg-notice');
    if (notice && this.getUser()) notice.style.display = 'block';

    // Show MainButton when booking page opens
    const filled = ['wb-name', 'wb-phone', 'wb-event-type', 'wb-date']
      .every(id => (document.getElementById(id)?.value || '').trim() !== '');

    this.showMainButton('✅ Отправить заявку', () => this.submitBooking());
    if (!filled) this.tg?.MainButton?.disable();
  }

  _onBookingSuccess(orderNumber) {
    const tg = this.tg;
    if (tg) {
      try {
        tg.showPopup({
          title:   '🎉 Заявка оформлена!',
          message: `Номер заявки: ${orderNumber}\nМенеджер свяжется с вами в ближайшее время.`,
          buttons: [{ id: 'close', type: 'close', text: 'Закрыть' }]
        }, (btnId) => {
          if (btnId === 'close') this.navigate('home');
        });
        return;
      } catch {}
    }
    this._showToast('✅ Заявка отправлена! Номер: ' + orderNumber);
    this.navigate('home');
  }

  _showError(msg) {
    const tg = this.tg;
    if (tg) {
      try {
        tg.showAlert(msg);
        try { tg.HapticFeedback.notificationOccurred('error'); } catch {}
        return;
      } catch {}
    }
    this._showToast('❌ ' + msg);
  }

  _showToast(msg, duration = 3000) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => el.classList.remove('show'), duration);
  }

  _showOutsideTelegramBanner() {
    const banner = document.createElement('div');
    banner.style.cssText =
      'background:#1a1a1a;color:#888;text-align:center;font-size:0.72rem;' +
      'padding:8px 16px;letter-spacing:0.05em;';
    banner.textContent = '⚠️ Откройте в Telegram для полного опыта';
    document.body.prepend(banner);
  }

  _modelCardHTML(m) {
    const name  = m.name  || m.first_name || 'Модель';
    const photo = m.photo || m.photo_url  || m.avatar || '';
    const meta  = [m.height ? `${m.height} см` : '', m.age ? `${m.age} л` : ''].filter(Boolean).join(' · ') || 'Портфолио';
    const img   = photo
      ? `<img src="${this._esc(photo)}" alt="${this._esc(name)}" loading="lazy" />`
      : '👤';
    return `
      <div class="model-card" data-id="${this._esc(String(m.id || ''))}">
        <div class="model-card-img">${img}</div>
        <div class="model-card-info">
          <div class="model-card-name">${this._esc(name)}</div>
          <div class="model-card-meta">${this._esc(meta)}</div>
        </div>
      </div>`;
  }

  _placeholderCards() {
    const items = Array.from({ length: 4 }, (_, i) =>
      `<div class="model-card">
        <div class="model-card-img" style="background:linear-gradient(135deg,#1a1a1a,#222);">👤</div>
        <div class="model-card-info">
          <div class="model-card-name" style="color:var(--tg-hint);">Загрузка…</div>
          <div class="model-card-meta">—</div>
        </div>
      </div>`
    );
    return items.join('');
  }

  _openModelPage(id) {
    const url = `/catalog.html#model-${id}`;
    if (this.tg) {
      try { this.tg.openLink(window.location.origin + url); return; } catch {}
    }
    window.open(url, '_blank');
  }

  _getVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  _esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Legacy IIFE — injected on existing pages (booking.html, index.html, etc.)
// Runs automatically when the script is included outside webapp.html
// ─────────────────────────────────────────────────────────────────────────────
(function () {
  'use strict';

  // Only activate on pages that are NOT webapp.html
  if (document.getElementById('page-home')) return; // webapp.html handles itself

  const tg = window.Telegram?.WebApp;
  if (!tg) return;

  tg.ready();
  tg.expand();

  try {
    tg.setHeaderColor('#080808');
    tg.setBackgroundColor('#080808');
  } catch {}

  window._tgWebApp  = tg;
  window._tgUser    = tg.initDataUnsafe?.user || null;

  const user = window._tgUser;

  // ── Mini App banner ──────────────────────────────────────────────────────
  function injectBanner() {
    if (document.getElementById('tg-webapp-banner')) return;
    const BANNER_HEIGHT = 28;
    const banner = document.createElement('div');
    banner.id = 'tg-webapp-banner';
    banner.style.cssText =
      'background:linear-gradient(90deg,#229ED9 0%,#1a7bbf 100%);color:#fff;' +
      'text-align:center;font-size:0.75rem;letter-spacing:1px;padding:6px 16px;' +
      'position:fixed;top:0;left:0;right:0;z-index:9999;pointer-events:none;' +
      `height:${BANNER_HEIGHT}px;`;
    banner.textContent = user ? `Telegram Mini App · ${user.first_name}` : 'Telegram Mini App';
    document.body.prepend(banner);
    document.body.style.paddingTop = BANNER_HEIGHT + 'px';
    const navbar = document.querySelector('.navbar');
    if (navbar) navbar.style.top = BANNER_HEIGHT + 'px';
  }

  // ── Back button ──────────────────────────────────────────────────────────
  if (tg.BackButton) {
    tg.BackButton.show();
    tg.BackButton.onClick(() => {
      if (window.history.length > 1) {
        window.history.back();
      } else {
        tg.close();
      }
    });
  }

  // ── Auto-fill booking form ───────────────────────────────────────────────
  function autofillBooking() {
    if (!user) return;
    const fillIfEmpty = (selector, value) => {
      const el = document.querySelector(selector);
      if (el && !el.value && value) el.value = value;
    };
    const fullName = [user.first_name, user.last_name].filter(Boolean).join(' ');
    fillIfEmpty('#client_name',     fullName);
    fillIfEmpty('#client_telegram', user.username || '');

    const form = document.querySelector('.booking-form-wrap, .booking-section');
    if (form && fullName) {
      const notice = document.createElement('div');
      notice.style.cssText =
        'background:rgba(34,158,217,0.12);border:1px solid #229ED9;border-radius:8px;' +
        'padding:10px 14px;margin-bottom:16px;font-size:0.82rem;color:#229ED9;';
      notice.innerHTML = `✅ Данные из Telegram предзаполнены для <strong>${fullName}</strong>`;
      form.prepend(notice);
    }
  }

  // ── Main button for booking submission ───────────────────────────────────
  function setupMainButton() {
    const submitBtn = document.getElementById('submitBtn') || document.querySelector('button[type="submit"]');
    if (!submitBtn || !tg.MainButton) return;

    tg.MainButton.setText('✅ Отправить заявку');
    tg.MainButton.color    = '#C9A84C';
    tg.MainButton.textColor = '#000000';
    tg.MainButton.hide();
    tg.MainButton.onClick(() => submitBtn.click());

    const observer = new MutationObserver(() => {
      submitBtn.disabled ? tg.MainButton.disable() : tg.MainButton.enable();
    });
    observer.observe(submitBtn, { attributes: true, attributeFilter: ['disabled'] });

    const step4 = document.getElementById('step4');
    if (step4) {
      const visObs = new MutationObserver(() => {
        step4.classList.contains('active') ? tg.MainButton.show() : tg.MainButton.hide();
      });
      visObs.observe(step4, { attributes: true, attributeFilter: ['class'] });
    }
  }

  // ── Notify Telegram on successful booking ────────────────────────────────
  window._tgWebAppOnBookingSuccess = function (orderNumber) {
    if (!tg) return;
    try {
      tg.showPopup({
        title:   '🎉 Заявка оформлена!',
        message: `Номер вашей заявки: ${orderNumber}\nМенеджер свяжется с вами в ближайшее время.`,
        buttons: [{ id: 'close', type: 'close', text: 'Закрыть' }]
      }, (btnId) => { if (btnId === 'close') tg.close(); });
    } catch { tg.close(); }
  };

  // ── Haptic feedback helpers ──────────────────────────────────────────────
  window._tgHaptic = {
    light:   () => { try { tg.HapticFeedback.impactOccurred('light');        } catch {} },
    success: () => { try { tg.HapticFeedback.notificationOccurred('success'); } catch {} },
    error:   () => { try { tg.HapticFeedback.notificationOccurred('error');   } catch {} },
  };

  // ── Init on DOM ready ────────────────────────────────────────────────────
  function init() {
    injectBanner();
    const isBooking = document.querySelector('.booking-page') || document.getElementById('bookingForm');
    if (isBooking) {
      autofillBooking();
      setupMainButton();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
