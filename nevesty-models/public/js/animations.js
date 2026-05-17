/**
 * animations.js — Nevesty Models
 * IntersectionObserver for [data-animate] elements + scroll-progress bar
 * + scroll-reveal, counters, typing effect, parallax, stagger children
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
      var scrollTop = window.scrollY || document.documentElement.scrollTop;
      var docHeight = document.documentElement.scrollHeight - window.innerHeight;
      var pct = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
      bar.style.width = pct.toFixed(2) + '%';
    }

    window.addEventListener('scroll', updateProgress, { passive: true });
    updateProgress(); // run once on load
  }

  /* ─── INTERSECTION OBSERVER — [data-animate] ──── */
  function initFadeInUp() {
    // Guard: if main.js already initialized scroll animations, skip to avoid double-init
    if (window._nm_scroll_anim_init) return;
    window._nm_scroll_anim_init = true;

    if (!('IntersectionObserver' in window)) {
      // Fallback: just make everything visible immediately
      document.querySelectorAll('[data-animate]').forEach(function (el) {
        el.classList.add('is-visible', 'section-visible');
      });
      return;
    }

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            var el = entry.target;
            el.classList.add('is-visible', 'section-visible');
            observer.unobserve(el); // animate only once
          }
        });
      },
      {
        threshold: 0.12, // trigger when 12% of the element is visible
        rootMargin: '0px 0px -40px 0px', // slight bottom offset so it fires a bit before
      }
    );

    document.querySelectorAll('[data-animate]').forEach(function (el) {
      observer.observe(el);
    });
  }

  /* ─── SCROLL REVEAL — .reveal, .reveal-left, .reveal-right, .reveal-stagger ── */
  function initScrollReveal() {
    if (!('IntersectionObserver' in window)) {
      // Fallback: show everything immediately
      document.querySelectorAll('.reveal, .reveal-left, .reveal-right, .reveal-stagger').forEach(function (el) {
        el.classList.add('visible');
        if (el.classList.contains('reveal-stagger')) {
          Array.from(el.children).forEach(function (child) {
            child.style.transitionDelay = '0ms';
          });
        }
      });
      return;
    }

    var revealObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            var el = entry.target;
            el.classList.add('visible');

            // For stagger: apply incremental delay to children
            if (el.classList.contains('reveal-stagger')) {
              Array.from(el.children).forEach(function (child, i) {
                child.style.transitionDelay = i * 80 + 'ms';
              });
            }

            revealObserver.unobserve(el);
          }
        });
      },
      {
        threshold: 0.15,
        rootMargin: '0px 0px -30px 0px',
      }
    );

    document.querySelectorAll('.reveal, .reveal-left, .reveal-right, .reveal-stagger').forEach(function (el) {
      revealObserver.observe(el);
    });
  }

  /* ─── COUNTER ANIMATION — [data-counter] ─────── */
  function initCounters() {
    var counters = document.querySelectorAll('[data-counter]');
    if (!counters.length) return;
    // If reduced motion: show final values immediately, skip animation
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      counters.forEach(function (el) {
        var target = parseFloat(el.getAttribute('data-counter'));
        if (!isNaN(target)) el.textContent = target + (el.getAttribute('data-counter-suffix') || '');
      });
      return;
    }

    function easeOutCubic(t) {
      return 1 - Math.pow(1 - t, 3);
    }

    function animateCounter(el) {
      var target = parseFloat(el.getAttribute('data-counter'));
      if (isNaN(target)) return;

      var suffix = el.getAttribute('data-counter-suffix') || '';
      var duration = 1500; // ms
      var startTime = null;

      function step(timestamp) {
        if (!startTime) startTime = timestamp;
        var elapsed = timestamp - startTime;
        var progress = Math.min(elapsed / duration, 1);
        var eased = easeOutCubic(progress);
        var current = Math.round(eased * target);

        el.textContent = current + suffix;

        if (progress < 1) {
          requestAnimationFrame(step);
        } else {
          el.textContent = target + suffix;
        }
      }

      requestAnimationFrame(step);
    }

    if (!('IntersectionObserver' in window)) {
      counters.forEach(animateCounter);
      return;
    }

    var counterObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            animateCounter(entry.target);
            counterObserver.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.3 }
    );

    counters.forEach(function (el) {
      counterObserver.observe(el);
    });
  }

  /* ─── TYPING EFFECT — [data-typing] ──────────── */
  function initTyping() {
    var typingEls = document.querySelectorAll('[data-typing]');
    if (!typingEls.length) return;
    // If reduced motion: show text immediately without typing animation
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      typingEls.forEach(function (el) {
        var text = el.getAttribute('data-typing') || el.textContent.trim();
        el.textContent = text;
      });
      return;
    }

    typingEls.forEach(function (el) {
      var text = el.getAttribute('data-typing') || el.textContent.trim();
      var speed = parseInt(el.getAttribute('data-typing-speed') || '55', 10); // ms per char

      // Clear and start hidden
      el.textContent = '';
      el.setAttribute('aria-label', text);

      // Add blinking cursor element
      var cursor = document.createElement('span');
      cursor.className = 'typing-cursor';
      cursor.setAttribute('aria-hidden', 'true');
      cursor.textContent = '|';

      var charIndex = 0;

      function typeNext() {
        if (charIndex < text.length) {
          el.textContent = text.slice(0, charIndex + 1);
          el.appendChild(cursor);
          charIndex++;
          setTimeout(typeNext, speed);
        } else {
          // Typing done — keep cursor blinking
          el.appendChild(cursor);
          cursor.classList.add('blinking');
        }
      }

      // Start typing when element enters viewport
      if (!('IntersectionObserver' in window)) {
        typeNext();
        return;
      }

      var typingObserver = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (entry.isIntersecting) {
              typeNext();
              typingObserver.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.5 }
      );

      typingObserver.observe(el);
    });
  }

  /* ─── PARALLAX — .parallax-slow / .parallax-fast ─ */
  function initParallax() {
    // Only on desktop
    if (window.innerWidth < 1024) return;
    // Respect prefers-reduced-motion
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

    var slowEls = document.querySelectorAll('.parallax-slow');
    var fastEls = document.querySelectorAll('.parallax-fast');

    // Also animate the existing hero-bg parallax
    var heroBg = document.querySelector('.hero-bg');

    if (!slowEls.length && !fastEls.length && !heroBg) return;

    var ticking = false;

    function updateParallax() {
      var scrollY = window.scrollY;

      slowEls.forEach(function (el) {
        // 30% scroll speed
        el.style.transform = 'translateY(' + scrollY * 0.3 + 'px)';
      });

      fastEls.forEach(function (el) {
        // 60% scroll speed
        el.style.transform = 'translateY(' + scrollY * 0.6 + 'px)';
      });

      // Hero background parallax
      if (heroBg) {
        heroBg.style.transform = 'translateY(' + scrollY * 0.35 + 'px)';
      }

      ticking = false;
    }

    window.addEventListener(
      'scroll',
      function () {
        if (!ticking) {
          requestAnimationFrame(updateParallax);
          ticking = true;
        }
      },
      { passive: true }
    );

    // Initial call
    updateParallax();
  }

  /* ─── STAGGER CHILDREN — .stagger-children > * ── */
  function initStaggerChildren() {
    document.querySelectorAll('.stagger-children').forEach(function (parent) {
      Array.from(parent.children).forEach(function (child, i) {
        child.style.animationDelay = i * 0.1 + 's';
      });
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
    type = type || 'info'; // 'success' | 'error' | 'info'
    durationMs = durationMs || 4000;

    var icons = { success: '✓', error: '✕', info: 'ℹ' };
    var container = document.querySelector('.toast-container');
    if (!container) {
      container = document.createElement('div');
      container.className = 'toast-container';
      document.body.appendChild(container);
    }

    var toast = document.createElement('div');
    // Use 'toast-<type>' class to match the enhanced CSS variant system
    toast.className = 'toast toast-' + type;
    toast.innerHTML =
      '<span class="toast-icon">' +
      (icons[type] || icons.info) +
      '</span>' +
      '<div class="toast-body">' +
      (title ? '<div class="toast-title">' + title + '</div>' : '') +
      (msg ? '<div class="toast-msg">' + msg + '</div>' : '') +
      '</div>' +
      '<button class="toast-close" aria-label="Dismiss">&times;</button>';

    container.appendChild(toast);

    function dismiss() {
      toast.classList.add('removing');
      toast.addEventListener('animationend', function () {
        toast.remove();
      });
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

  // Additional animations — triggered on DOMContentLoaded
  document.addEventListener('DOMContentLoaded', function () {
    initScrollReveal();
    initCounters();
    initTyping();
    initParallax();
    initStaggerChildren();
  });
})();
