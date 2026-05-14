(function () {
  'use strict';

  const tg = window.Telegram?.WebApp;
  if (!tg) return; // Not running inside Telegram

  // Expand to full height and configure theme
  tg.ready();
  tg.expand();

  try {
    tg.setHeaderColor('#080808');
    tg.setBackgroundColor('#080808');
  } catch {}

  // Expose for other scripts
  window._tgWebApp = tg;
  window._tgUser = tg.initDataUnsafe?.user || null;

  const user = window._tgUser;

  // ── Mini App banner ─────────────────────────────────────────────────────────
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
    // Push down body content (non-fixed elements)
    document.body.style.paddingTop = BANNER_HEIGHT + 'px';
    // Push down the fixed navbar so it sits below the banner, not behind it
    const navbar = document.querySelector('.navbar');
    if (navbar) {
      navbar.style.top = BANNER_HEIGHT + 'px';
    }
  }

  // ── Back button ─────────────────────────────────────────────────────────────
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

  // ── Auto-fill booking form ──────────────────────────────────────────────────
  function autofillBooking() {
    if (!user) return;

    const fillIfEmpty = (selector, value) => {
      const el = document.querySelector(selector);
      if (el && !el.value && value) el.value = value;
    };

    const fullName = [user.first_name, user.last_name].filter(Boolean).join(' ');
    // Use correct IDs that match booking.html (underscore-separated)
    fillIfEmpty('#client_name', fullName);
    if (user.username) fillIfEmpty('#client_telegram', user.username);

    // Show notice
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

  // ── Main button for booking submission ──────────────────────────────────────
  function setupMainButton() {
    const submitBtn = document.getElementById('submitBtn') || document.querySelector('button[type="submit"]');
    if (!submitBtn || !tg.MainButton) return;

    tg.MainButton.setText('✅ Отправить заявку');
    tg.MainButton.color = '#C9A84C';
    tg.MainButton.textColor = '#000000';
    // Start hidden — show only when step 4 (confirmation) is active
    tg.MainButton.hide();

    tg.MainButton.onClick(() => submitBtn.click());

    // Sync disabled state via MutationObserver on the button attribute
    const observer = new MutationObserver(() => {
      if (submitBtn.disabled) {
        tg.MainButton.disable();
      } else {
        tg.MainButton.enable();
      }
    });
    observer.observe(submitBtn, { attributes: true, attributeFilter: ['disabled'] });

    // Watch the step4 section visibility to show/hide the Main Button
    const step4 = document.getElementById('step4');
    if (step4) {
      const visibilityObserver = new MutationObserver(() => {
        if (step4.classList.contains('active')) {
          tg.MainButton.show();
        } else {
          tg.MainButton.hide();
        }
      });
      visibilityObserver.observe(step4, { attributes: true, attributeFilter: ['class'] });
    }
  }

  // ── Notify Telegram on successful booking ──────────────────────────────────
  window._tgWebAppOnBookingSuccess = function (orderNumber) {
    if (!tg) return;
    try {
      tg.showPopup({
        title: '🎉 Заявка оформлена!',
        message: `Номер вашей заявки: ${orderNumber}\nМенеджер свяжется с вами в ближайшее время.`,
        buttons: [{ id: 'close', type: 'close', text: 'Закрыть' }]
      }, (btnId) => {
        if (btnId === 'close') tg.close();
      });
    } catch {
      tg.close();
    }
  };

  // ── Haptic feedback helpers ──────────────────────────────────────────────────
  window._tgHaptic = {
    light: () => { try { tg.HapticFeedback.impactOccurred('light'); } catch {} },
    success: () => { try { tg.HapticFeedback.notificationOccurred('success'); } catch {} },
    error: () => { try { tg.HapticFeedback.notificationOccurred('error'); } catch {} },
  };

  // ── Init on DOM ready ────────────────────────────────────────────────────────
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
