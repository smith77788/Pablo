// ─── Flash-prevention: apply theme before first paint ─────────────────────────
// This runs immediately (not deferred) so the data-theme is set before CSS renders.
(function () {
  var saved = '';
  try {
    saved = localStorage.getItem('nm-theme') || '';
  } catch (e) {}
  var prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;

  // Site is dark-by-default. Apply light theme only when explicitly requested.
  if (saved === 'light' || (!saved && prefersLight)) {
    document.documentElement.setAttribute('data-theme', 'light');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
})();

// ─── Dark mode manager ─────────────────────────────────────────────────────────
const DarkMode = {
  init() {
    var saved = '';
    try {
      saved = localStorage.getItem('nm-theme') || '';
    } catch (e) {}
    var prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    var isLight = saved === 'light' || (!saved && prefersLight);

    // Update icon on init
    this._updateIcon(isLight);

    // Listen for system changes (only applies if user has no saved preference)
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function (e) {
      try {
        if (!localStorage.getItem('nm-theme')) {
          if (e.matches) {
            document.documentElement.setAttribute('data-theme', 'light');
          } else {
            document.documentElement.removeAttribute('data-theme');
          }
          DarkMode._updateIcon(e.matches);
        }
      } catch (err) {}
    });
  },

  toggle() {
    var isNowLight = document.documentElement.getAttribute('data-theme') !== 'light';
    if (isNowLight) {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    try {
      localStorage.setItem('nm-theme', isNowLight ? 'light' : 'dark');
    } catch (e) {}
    this._updateIcon(isNowLight);
  },

  _updateIcon(isLight) {
    // Support both .theme-toggle buttons (emoji directly inside)
    // and legacy .dark-mode-icon spans
    document.querySelectorAll('.theme-toggle').forEach(function (el) {
      el.textContent = isLight ? '🌙' : '☀️';
      el.setAttribute('aria-label', isLight ? 'Переключить на тёмную тему' : 'Переключить на светлую тему');
    });
    document.querySelectorAll('.dark-mode-icon').forEach(function (el) {
      el.textContent = isLight ? '🌙' : '☀️';
    });
  },
};

// Auto-init icon on DOM ready (data-theme is already set by flash-prevention above)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function () {
    DarkMode.init();
  });
} else {
  DarkMode.init();
}
