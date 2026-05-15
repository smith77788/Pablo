// Dark mode manager
const DarkMode = {
  init() {
    // Check saved preference or system preference
    const saved = localStorage.getItem('theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const isDark = saved === 'dark' || (!saved && prefersDark);

    if (isDark) {
      document.documentElement.classList.add('dark');
    }

    // Update icon on init
    this._updateIcon(isDark);

    // Listen for system changes
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
      if (!localStorage.getItem('theme')) {
        e.matches ? document.documentElement.classList.add('dark')
                  : document.documentElement.classList.remove('dark');
        this._updateIcon(e.matches);
      }
    });
  },

  toggle() {
    const isDark = document.documentElement.classList.toggle('dark');
    localStorage.setItem('theme', isDark ? 'dark' : 'light');
    this._updateIcon(isDark);
  },

  _updateIcon(isDark) {
    document.querySelectorAll('.dark-mode-icon').forEach(el => {
      el.textContent = isDark ? '☀️' : '🌙';
    });
  }
};

// Auto-init on DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => DarkMode.init());
} else {
  DarkMode.init();
}
