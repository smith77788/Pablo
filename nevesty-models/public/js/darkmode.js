// ─── Flash-prevention: apply theme before first paint ─────────────────────────
// This runs immediately (not deferred) so the html class is set before CSS renders.
(function () {
  var saved = '';
  try { saved = localStorage.getItem('theme') || ''; } catch (e) {}
  var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

  // Site is dark-by-default. Only add class when there's a mismatch.
  if (saved === 'light') {
    document.documentElement.classList.add('light');
    document.documentElement.classList.remove('dark');
  } else if (saved === 'dark' || (!saved && prefersDark)) {
    document.documentElement.classList.add('dark');
    document.documentElement.classList.remove('light');
  }
  // If !saved && !prefersDark → site default dark applies, no class needed
})();

// ─── Dark mode manager ─────────────────────────────────────────────────────────
const DarkMode = {
  init() {
    var saved = '';
    try { saved = localStorage.getItem('theme') || ''; } catch (e) {}
    // Site is dark-by-default: anything except explicitly saved 'light' → dark icon
    var isDark = saved !== 'light';

    // Update icon on init
    this._updateIcon(isDark);

    // Listen for system changes (only applies if user has no saved preference)
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
      try {
        if (!localStorage.getItem('theme')) {
          document.documentElement.classList.toggle('dark', e.matches);
          document.documentElement.classList.toggle('light', !e.matches);
          DarkMode._updateIcon(e.matches);
        }
      } catch (err) {}
    });
  },

  toggle() {
    var isNowLight = document.documentElement.classList.toggle('light');
    document.documentElement.classList.toggle('dark', !isNowLight);
    try { localStorage.setItem('theme', isNowLight ? 'light' : 'dark'); } catch (e) {}
    this._updateIcon(!isNowLight);
  },

  _updateIcon(isDark) {
    document.querySelectorAll('.dark-mode-icon').forEach(function (el) {
      el.textContent = isDark ? '☀️' : '🌙';
    });
  }
};

// Auto-init icon on DOM ready (class is already set by flash-prevention above)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function () { DarkMode.init(); });
} else {
  DarkMode.init();
}
