(function () {
  'use strict';

  const COOKIE_KEY = 'nm_cookie_consent';
  const COOKIE_DURATION_DAYS = 365;

  function getCookie(name) {
    const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
    return match ? decodeURIComponent(match[2]) : null;
  }

  function setCookie(name, value, days) {
    const expires = new Date(Date.now() + days * 864e5).toUTCString();
    document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax`;
  }

  function createBanner() {
    const banner = document.createElement('div');
    banner.id = 'cookie-banner';
    banner.className = 'cookie-banner';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-label', 'Cookie consent');
    banner.setAttribute('aria-live', 'polite');
    banner.innerHTML = `
      <p class="cookie-text">
        🍪 Мы используем cookies для улучшения работы сайта и аналитики.
        Продолжая использовать сайт, вы соглашаетесь с
        <a href="/privacy.html">политикой конфиденциальности</a>.
      </p>
      <div class="cookie-actions">
        <button id="cookie-reject" class="cookie-btn-necessary" type="button">
          Только необходимые
        </button>
        <button id="cookie-accept" class="cookie-btn-accept" type="button">
          Принять все
        </button>
      </div>
    `;
    document.body.appendChild(banner);

    // Slide-up animation: allow paint before adding .visible
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        banner.classList.add('visible');
      });
    });

    document.getElementById('cookie-accept').addEventListener('click', () => {
      setCookie(COOKIE_KEY, 'all', COOKIE_DURATION_DAYS);
      // Also set localStorage key for GA/YM inline loaders that check it
      try {
        localStorage.setItem('cookie_consent', 'accepted');
      } catch (e) {}
      hideBanner(banner);
      // Notify analytics module if available
      window.NM?.analytics?.consent?.('all');
      // Dispatch event so analytics scripts can init without page reload
      try {
        document.dispatchEvent(new CustomEvent('cookieConsentAccepted'));
      } catch (e) {}
    });

    document.getElementById('cookie-reject').addEventListener('click', () => {
      setCookie(COOKIE_KEY, 'necessary', COOKIE_DURATION_DAYS);
      try {
        localStorage.setItem('cookie_consent', 'declined');
      } catch (e) {}
      hideBanner(banner);
      window.NM?.analytics?.consent?.('necessary');
    });
  }

  function hideBanner(banner) {
    banner.classList.remove('visible');
    setTimeout(() => banner.remove(), 450);
  }

  function init() {
    if (getCookie(COOKIE_KEY)) return; // already consented (cookie)
    // Also check localStorage for pages that use the static banner approach
    try {
      if (localStorage.getItem('cookie_consent')) return;
    } catch (e) {}
    // If a static banner already exists in the DOM, let it handle consent
    if (document.getElementById('cookie-banner')) return;

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => {
        if (!document.getElementById('cookie-banner')) createBanner();
      });
    } else {
      // Small delay to avoid flashing on page load
      setTimeout(() => {
        if (!document.getElementById('cookie-banner')) createBanner();
      }, 800);
    }
  }

  // Expose for analytics integration
  window.NM = window.NM || {};
  window.NM.cookieConsent = { hasConsent: () => getCookie(COOKIE_KEY) === 'all' };

  init();
})();
