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
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-label', 'Cookie consent');
    banner.setAttribute('aria-live', 'polite');
    banner.style.cssText = `
      position: fixed; bottom: 0; left: 0; right: 0; z-index: 9999;
      background: rgba(15, 15, 15, 0.97); border-top: 1px solid rgba(201,169,110,0.2);
      padding: 16px 24px; display: flex; align-items: center; gap: 16px;
      flex-wrap: wrap; backdrop-filter: blur(10px);
      transform: translateY(100%); transition: transform 0.3s ease;
    `;
    banner.innerHTML = `
      <div style="flex:1;min-width:200px">
        <p style="margin:0;color:#e0d5c5;font-size:0.85rem;line-height:1.5">
          🍪 Мы используем cookies для улучшения работы сайта и аналитики.
          Продолжая использовать сайт, вы соглашаетесь с
          <a href="/privacy.html" style="color:#c9a96e;text-decoration:underline">политикой конфиденциальности</a>.
        </p>
      </div>
      <div style="display:flex;gap:10px;flex-shrink:0">
        <button id="cookie-reject" style="padding:8px 16px;background:transparent;border:1px solid rgba(201,169,110,0.4);color:#c9a96e;border-radius:6px;font-size:0.82rem;cursor:pointer;transition:all 0.2s">
          Только необходимые
        </button>
        <button id="cookie-accept" style="padding:8px 20px;background:#c9a96e;color:#000;border:none;border-radius:6px;font-size:0.82rem;font-weight:600;cursor:pointer;transition:opacity 0.2s">
          Принять все
        </button>
      </div>
    `;
    document.body.appendChild(banner);
    // Animate in
    setTimeout(() => { banner.style.transform = 'translateY(0)'; }, 100);

    document.getElementById('cookie-accept').addEventListener('click', () => {
      setCookie(COOKIE_KEY, 'all', COOKIE_DURATION_DAYS);
      // Also set localStorage key for GA/YM inline loaders that check it
      try { localStorage.setItem('cookie_consent', 'accepted'); } catch(e) {}
      hideBanner(banner);
      // Notify analytics module if available
      window.NM?.analytics?.consent?.('all');
      // Dispatch event so analytics scripts can init without page reload
      try { document.dispatchEvent(new CustomEvent('cookieConsentAccepted')); } catch(e) {}
    });

    document.getElementById('cookie-reject').addEventListener('click', () => {
      setCookie(COOKIE_KEY, 'necessary', COOKIE_DURATION_DAYS);
      try { localStorage.setItem('cookie_consent', 'declined'); } catch(e) {}
      hideBanner(banner);
      window.NM?.analytics?.consent?.('necessary');
    });
  }

  function hideBanner(banner) {
    banner.style.transform = 'translateY(100%)';
    setTimeout(() => banner.remove(), 400);
  }

  function init() {
    if (getCookie(COOKIE_KEY)) return; // already consented (cookie)
    // Also check localStorage for pages that use the static banner approach
    if (localStorage.getItem('cookie_consent')) return;
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
