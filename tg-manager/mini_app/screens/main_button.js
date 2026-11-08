// ── Главное действие — в системной кнопке Telegram (MainButton) ─────────
// Лучшие мини-приложения ставят главное действие экрана в системную кнопку
// внизу: она всегда на месте, не уезжает с прокруткой и выглядит как сам
// Telegram. Здесь — для мастеров и форм: кнопка, помеченная data-main, на
// верхнем видимом слое (открытое окно, иначе текущий экран) переезжает в
// MainButton. Своя кнопка на странице прячется, пока её дублирует системная;
// вне Telegram и в старых клиентах ничего не меняется.
//
// Почему опрос, а не события: окна открываются из сотни мест (showModal,
// classList.add('show'), экраны из JS), а кнопки блокируются на время запроса
// (btn.disabled). Опрос раз в 300 мс по нескольким элементам ничего не стоит и
// ловит всё это одинаково.
(function () {
  const W = window.Telegram && window.Telegram.WebApp;
  const MB = W && W.MainButton;
  const supported = !!(MB && W.initData && MB.setParams
    && (!W.isVersionAtLeast || W.isVersionAtLeast('6.1')));
  let cur = null;        // кнопка страницы, которую сейчас дублирует MainButton
  let lastText = '', lastOn = null;

  function _label(el) {
    // Эмодзи в начале подписи системной кнопке не нужны: её узнают по месту.
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    return t.replace(/^[^\p{L}\p{N}]+/u, '').slice(0, 64) || t.slice(0, 64);
  }

  // Видна ли кнопка — без учёта того, что её саму спрятали мы.
  function _shown(el) {
    const p = el.parentElement;
    return !!(p && p.offsetParent !== null) && !el.closest('[hidden]');
  }

  // Верхний слой: последнее открытое окно, иначе последний показанный экран.
  function _topLayer() {
    const modals = [...document.querySelectorAll('.mbg.show, .modal-bg.show, .mbg.open')];
    if (modals.length) return modals[modals.length - 1];
    const screens = [...document.querySelectorAll('.screen.show')];
    return screens.length ? screens[screens.length - 1] : null;
  }

  function mainActionTarget() {
    const layer = _topLayer();
    if (!layer) return null;
    const cands = [...layer.querySelectorAll('[data-main]')].filter(_shown);
    return cands.length ? cands[cands.length - 1] : null;
  }

  function _release() {
    if (cur) cur.classList.remove('mb-mirrored');
    cur = null;
  }

  function sync() {
    if (!supported) return;
    const el = mainActionTarget();
    if (el !== cur) {
      _release();
      if (!el) { MB.hide(); lastText = ''; lastOn = null; return; }
      cur = el;
      cur.classList.add('mb-mirrored');
    }
    if (!cur) return;
    const text = _label(cur), on = !cur.disabled;
    if (text !== lastText || on !== lastOn) {
      MB.setParams({ text, is_active: on, is_visible: true });
      try { if (on) MB.hideProgress && MB.hideProgress(); else MB.showProgress && MB.showProgress(false); } catch (_) {}
      lastText = text; lastOn = on;
    }
  }

  if (supported) {
    MB.onClick(() => {
      if (cur && !cur.disabled) {
        try { W.HapticFeedback && W.HapticFeedback.impactOccurred('medium'); } catch (_) {}
        cur.click();
      }
    });
    setInterval(sync, 300);
    document.addEventListener('click', () => setTimeout(sync, 0), true);
  }

  window.mainActionTarget = mainActionTarget;
  window.syncMainButton = sync;
  window.MAIN_BUTTON_SUPPORTED = supported;
})();
