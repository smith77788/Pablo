/**
 * animations.js — Nevesty Models
 * IntersectionObserver for [data-animate] elements + scroll-progress bar
 */

(function () {
  'use strict';

  /* ─── SCROLL PROGRESS BAR ─────────────────────── */
  function initScrollProgress() {
    var bar = document.getElementById('scroll-progress');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'scroll-progress';
      document.body.prepend(bar);
    }

    function updateProgress() {
      var scrollTop  = window.scrollY || document.documentElement.scrollTop;
      var docHeight  = document.documentElement.scrollHeight - window.innerHeight;
      var pct = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
      bar.style.width = pct.toFixed(2) + '%';
    }

    window.addEventListener('scroll', updateProgress, { passive: true });
    updateProgress(); // run once on load
  }

  /* ─── INTERSECTION OBSERVER — [data-animate] ──── */
  function initFadeInUp() {
    if (!('IntersectionObserver' in window)) {
      // Fallback: just make everything visible immediately
      document.querySelectorAll('[data-animate]').forEach(function (el) {
        el.classList.add('is-visible');
      });
      return;
    }

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            var el = entry.target;
            el.classList.add('is-visible');
            observer.unobserve(el); // animate only once
          }
        });
      },
      {
        threshold: 0.12,      // trigger when 12% of the element is visible
        rootMargin: '0px 0px -40px 0px' // slight bottom offset so it fires a bit before
      }
    );

    document.querySelectorAll('[data-animate]').forEach(function (el) {
      observer.observe(el);
    });
  }

  /* ─── HAMBURGER MENU — toggle class ──────────────
     Pairs with .nav-burger.is-open CSS transitions  */
  function initHamburger() {
    var burger = document.querySelector('.nav-burger');
    if (!burger) return;

    burger.addEventListener('click', function () {
      var isOpen = document.querySelector('.nav-mobile.open');
      burger.classList.toggle('is-open', !isOpen);
    });

    // When mobile menu closes, also remove is-open from burger
    var closeBtn = document.querySelector('.nav-mobile-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', function () {
        burger.classList.remove('is-open');
      });
    }
  }

  /* ─── TOAST HELPER (global) ───────────────────── */
  window.showToast = function (title, msg, type, durationMs) {
    type        = type        || 'info';      // 'success' | 'error' | 'info'
    durationMs  = durationMs  || 4000;

    var icons   = { success: '✓', error: '✕', info: 'ℹ' };
    var container = document.querySelector('.toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }

    var toast = document.createElement('div');
    toast.className = 'toast ' + type;
    toast.innerHTML =
      '<span class="toast-icon">' + (icons[type] || icons.info) + '</span>' +
      '<div class="toast-body">' +
        (title ? '<div class="toast-title">' + title + '</div>' : '') +
        (msg   ? '<div class="toast-msg">'   + msg   + '</div>' : '') +
      '</div>' +
      '<button class="toast-close" aria-label="Dismiss">&times;</button>';

    container.appendChild(toast);

    function dismiss() {
      toast.classList.add('removing');
      toast.addEventListener('animationend', function () { toast.remove(); });
    }

    toast.querySelector('.toast-close').addEventListener('click', dismiss);
    setTimeout(dismiss, durationMs);
  };

  /* ─── INIT ────────────────────────────────────── */
  function init() {
    initScrollProgress();
    initFadeInUp();
    initHamburger();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
