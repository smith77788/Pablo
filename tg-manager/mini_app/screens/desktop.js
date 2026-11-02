// 🖥 Широкий экран: меню разделов слева, работа справа (две колонки).
//
// Раскладка включается по ширине окна (body.wide, от 960px), а не по
// платформе: узкое окно Telegram Desktop остаётся телефонной раскладкой.
// Стили — в index.html рядом с таббаром («Широкий экран: две колонки»).
//
// Окно мини-аппа на компьютере по умолчанию узкое. Telegram 8.0 умеет
// развернуть его во весь экран (tg.requestFullscreen) — строка «Во весь
// экран» в настройках показывается только там, где это есть и имеет смысл.
const WIDE_MIN = 960;
const _DESKTOP_PLATFORMS = ['tdesktop', 'macos', 'weba', 'webk', 'web', 'unigram'];

function _isWide() {
  try { return (window.innerWidth || 0) >= WIDE_MIN; } catch (_) { return false; }
}
function applyWideLayout() {
  const on = _isWide();
  const was = document.body.classList.contains('wide');
  document.body.classList.toggle('wide', on);
  if (on) {
    // Меню всегда на экране: его надо построить заранее, а не по ☰.
    try { buildDrawer(); markDrawerActive(); } catch (_) {}
    // Оверлей-затемнение от телефонного режима не должен остаться висеть.
    try { document.getElementById('navOverlay')?.classList.remove('on'); } catch (_) {}
  } else if (was) {
    // Ушли с широкого на узкое: меню снова прячется за ☰.
    try { closeDrawer(); } catch (_) {}
  }
  return on;
}

// Подсветка текущего раздела в постоянном меню: goTab меняет вкладку, меню
// должно это видеть. Обёртка вызывает оригинал как есть.
(function wrapGoTab() {
  if (typeof window.goTab !== 'function' || window.goTab._wide) return;
  const orig = window.goTab;
  const wrapped = function () {
    const r = orig.apply(this, arguments);
    if (document.body.classList.contains('wide')) {
      try { markDrawerActive(); } catch (_) {}
    }
    return r;
  };
  wrapped._wide = true;
  window.goTab = wrapped;
})();

function _fsSupported() {
  try {
    return !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast('8.0')
      && tg.requestFullscreen && _DESKTOP_PLATFORMS.includes(tg.platform));
  } catch (_) { return false; }
}
function renderFullscreenRow() {
  const row = document.getElementById('set-full-row');
  const lbl = document.getElementById('set-full-status');
  const btn = document.getElementById('set-full-btn');
  if (!row || !lbl || !btn) return;
  if (!_fsSupported()) { row.style.display = 'none'; return; }
  row.style.display = 'flex';
  if (tg.isFullscreen) {
    lbl.textContent = 'Меню разделов слева, работа справа';
    btn.textContent = 'Свернуть';
  } else {
    lbl.textContent = 'На компьютере: меню слева, работа справа';
    btn.textContent = 'Развернуть';
  }
}
function toggleAppFullscreen() {
  if (!_fsSupported()) { renderFullscreenRow(); return; }
  try {
    if (tg.isFullscreen) tg.exitFullscreen(); else tg.requestFullscreen();
  } catch (_) { toast('⚠️ Telegram не развернул окно'); }
}

try {
  window.addEventListener('resize', applyWideLayout);
  if (tg && tg.onEvent) {
    tg.onEvent('fullscreenChanged', () => { applyWideLayout(); renderFullscreenRow(); });
    tg.onEvent('fullscreenFailed', e => {
      const why = e && e.error === 'UNSUPPORTED' ? 'этот клиент Telegram не умеет' : 'Telegram отказал';
      toast('⚠️ Во весь экран не вышло: ' + why);
    });
    tg.onEvent('viewportChanged', applyWideLayout);
  }
} catch (_) {}
applyWideLayout();
