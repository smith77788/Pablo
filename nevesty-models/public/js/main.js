/* ─── Shared utilities ─────────────────────────────── */
const API = '/api';

function escHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Ошибка запроса');
  return data;
}

function toast(msg, type = 'info') {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  if (!container.getAttribute('aria-live')) {
    container.setAttribute('aria-live', 'polite');
    container.setAttribute('aria-atomic', 'true');
  }
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ'}</span> ${msg}`;
  container.appendChild(t);
  setTimeout(() => t.remove(), 4000);
  // Haptic feedback for Telegram Mini App
  if (type === 'error') window._tgHaptic?.error();
  else if (type === 'success') window._tgHaptic?.success();
}

const CATEGORIES = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };

function placeholderImg(name) {
  return `<div class="model-card-placeholder">${name?.[0] || '?'}</div>`;
}

function modelCard(m, onClick) {
  const safeId = parseInt(m.id, 10) || 0;
  const availDot = m.available ? '<div class="model-avail"></div>' : '<div class="model-avail unavailable"></div>';
  return `
    <div class="model-card" onclick="${escHtml(onClick)}(${safeId})" role="button" tabindex="0"
         onkeydown="if(event.key==='Enter'||event.key===' '){${escHtml(onClick)}(${safeId})}">
      ${availDot}
      <div class="model-card-img">
        ${
          m.photo_main
            ? `<img data-src="${escHtml(m.photo_main)}" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
               alt="${escHtml(m.name)}" class="lazy-img" decoding="async" />`
            : placeholderImg(m.name)
        }
        <div class="model-card-overlay">
          <div class="model-card-tag">${escHtml(CATEGORIES[m.category] || m.category)}</div>
          <div style="font-size:0.8rem;color:#ccc">
            ${escHtml(m.height)}см · ${escHtml(m.bust)}/${escHtml(m.waist)}/${escHtml(m.hips)} · р.${escHtml(m.shoe_size)}
          </div>
        </div>
      </div>
      <div class="model-card-info">
        <div class="model-card-name">${escHtml(m.name)}</div>
        <div class="model-card-meta">${escHtml(m.height)} см · ${escHtml(m.hair_color)} · ${escHtml(m.eye_color)}</div>
      </div>
    </div>`;
}

/* ─── Lazy loading for model photos ───────────────── */
function initLazyImages() {
  if (!('IntersectionObserver' in window)) {
    // Fallback: load all images immediately
    document.querySelectorAll('img.lazy-img[data-src]').forEach(img => {
      img.src = img.dataset.src;
      img.classList.remove('lazy-img');
    });
    return;
  }

  const lazyObserver = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        const img = entry.target;
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
        img.classList.remove('lazy-img');
        img.addEventListener('load', () => img.classList.add('lazy-loaded'), { once: true });
        observer.unobserve(img);
      });
    },
    { rootMargin: '200px 0px' }
  );

  document.querySelectorAll('img.lazy-img[data-src]').forEach(img => lazyObserver.observe(img));

  // Expose so dynamically added cards can also be observed
  window._lazyObserver = lazyObserver;
}

/* ─── Star rating renderer ─────────────────────────── */
function renderStars(rating) {
  const full = Math.round(Math.max(0, Math.min(5, rating || 0)));
  return '★'.repeat(full) + '☆'.repeat(5 - full);
}

/* ─── Lightbox ─────────────────────────────────────── */
(function initLightbox() {
  if (document.getElementById('lightbox')) return;

  const style = document.createElement('style');
  style.textContent = `
    #lightbox { position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.96);display:none;align-items:center;justify-content:center; }
    #lightbox.lb-open { display:flex; }
    #lb-img { max-height:90vh;max-width:90vw;object-fit:contain;user-select:none; }
    #lb-prev,#lb-next {
      position:absolute;top:50%;transform:translateY(-50%);
      background:rgba(201,169,110,0.15);border:1px solid rgba(201,169,110,0.4);color:#c9a96e;
      font-size:1.6rem;width:52px;height:52px;cursor:pointer;
      display:flex;align-items:center;justify-content:center;
      transition:background 0.2s;border-radius:2px;
    }
    #lb-prev:hover,#lb-next:hover { background:rgba(201,169,110,0.3); }
    #lb-prev { left:20px; }
    #lb-next { right:20px; }
    #lb-close {
      position:absolute;top:20px;right:24px;
      background:none;border:none;color:#c9a96e;font-size:1.8rem;
      cursor:pointer;line-height:1;padding:4px 8px;
    }
    #lb-counter {
      position:absolute;bottom:20px;left:50%;transform:translateX(-50%);
      color:rgba(201,169,110,0.6);font-size:0.8rem;letter-spacing:2px;font-family:'Inter',sans-serif;
    }
    #modalMainImg { cursor:zoom-in; }
    /* Lazy image styles */
    img.lazy-img { opacity:0; transition:opacity 0.35s ease; }
    img.lazy-loaded { opacity:1; }
    /* Model modal fade-in */
    @keyframes modalFadeIn {
      from { opacity:0; transform:translateY(24px) scale(0.98); }
      to   { opacity:1; transform:translateY(0) scale(1); }
    }
    .modal-overlay.open .modal { animation: modalFadeIn 0.3s ease forwards; }
    /* Reviews container */
    #reviews-container .testimonial-card { opacity:0; animation: modalFadeIn 0.4s ease forwards; }
  `;
  document.head.appendChild(style);

  const lb = document.createElement('div');
  lb.id = 'lightbox';
  lb.innerHTML = `
    <button id="lb-prev" aria-label="Предыдущее фото">&#8592;</button>
    <img id="lb-img" alt="" />
    <button id="lb-next" aria-label="Следующее фото">&#8594;</button>
    <button id="lb-close" aria-label="Закрыть">&#10005;</button>
    <div id="lb-counter"></div>
  `;
  document.body.appendChild(lb);

  let _photos = [];
  let _idx = 0;

  function lbShow(photos, idx) {
    _photos = photos;
    _idx = idx;
    _render();
    lb.classList.add('lb-open');
    document.body.style.overflow = 'hidden';
    document.getElementById('lb-close').focus();
  }

  function lbClose() {
    lb.classList.remove('lb-open');
    if (!document.getElementById('modelModal')?.classList.contains('open')) {
      document.body.style.overflow = '';
    }
  }

  function _render() {
    document.getElementById('lb-img').src = _photos[_idx];
    const counter = document.getElementById('lb-counter');
    counter.textContent = _photos.length > 1 ? `${_idx + 1} / ${_photos.length}` : '';
    document.getElementById('lb-prev').style.display = _photos.length > 1 ? 'flex' : 'none';
    document.getElementById('lb-next').style.display = _photos.length > 1 ? 'flex' : 'none';
  }

  function lbPrev() {
    _idx = (_idx - 1 + _photos.length) % _photos.length;
    _render();
  }
  function lbNext() {
    _idx = (_idx + 1) % _photos.length;
    _render();
  }

  document.getElementById('lb-close').addEventListener('click', lbClose);
  document.getElementById('lb-prev').addEventListener('click', lbPrev);
  document.getElementById('lb-next').addEventListener('click', lbNext);
  lb.addEventListener('click', e => {
    if (e.target === lb) lbClose();
  });

  document.addEventListener('keydown', e => {
    if (!lb.classList.contains('lb-open')) return;
    if (e.key === 'Escape') lbClose();
    if (e.key === 'ArrowLeft') lbPrev();
    if (e.key === 'ArrowRight') lbNext();
  });

  window._lightbox = { show: lbShow };
})();

/* ─── Model detail modal ───────────────────────────── */
function openModelModal(id) {
  apiFetch(`/models/${id}`)
    .then(m => {
      // GA4: select_item event (model card clicked in catalog/homepage)
      if (window.gtag) {
        gtag('event', 'select_item', {
          item_id: m.id,
          item_name: m.name,
          item_category: m.category,
          item_variant: m.city,
        });
      }
      if (window.NM?.analytics) NM.analytics.viewModel(m.id, m.name);
      const photos = Array.isArray(m.photos) ? m.photos : [];
      const allPhotos = m.photo_main ? [m.photo_main, ...photos.filter(p => p !== m.photo_main)] : photos;

      window._currentModalPhotos = allPhotos;
      window._currentModalPhotoIdx = 0;

      const thumbsHtml = allPhotos
        .slice(0, 6)
        .map(
          (p, i) =>
            `<div class="modal-thumb ${i === 0 ? 'active' : ''}"
              onclick="switchModalPhoto('${p}',${i})"
              role="button" tabindex="0"
              onkeydown="if(event.key==='Enter'){switchModalPhoto('${p}',${i})}">
           <img src="${p}" alt="${m.name} фото ${i + 1}" />
         </div>`
        )
        .join('');

      const mainImgHtml =
        m.photo_main || allPhotos[0]
          ? `<img id="modalMainImg" src="${m.photo_main || allPhotos[0]}" alt="${m.name}"
               onclick="window._lightbox && window._lightbox.show(window._currentModalPhotos, window._currentModalPhotoIdx || 0)"
               title="Нажмите для просмотра в полном размере" />`
          : `<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
                background:var(--bg3);font-family:'Playfair Display',serif;
                font-size:6rem;color:rgba(201,169,110,0.15)">${m.name[0]}</div>`;

      const availBadge = m.available
        ? '<span style="display:inline-flex;align-items:center;gap:6px;font-size:0.75rem;color:#4caf8a"><span style="width:8px;height:8px;border-radius:50%;background:#4caf8a;display:inline-block"></span>Доступна</span>'
        : '<span style="display:inline-flex;align-items:center;gap:6px;font-size:0.75rem;color:var(--text-muted)"><span style="width:8px;height:8px;border-radius:50%;background:var(--text-muted);display:inline-block"></span>Временно недоступна</span>';

      document.getElementById('modalInner').innerHTML = `
        <div class="modal-gallery">
          <div class="modal-main-img">${mainImgHtml}</div>
          ${thumbsHtml ? `<div class="modal-thumbs">${thumbsHtml}</div>` : ''}
        </div>
        <div class="modal-info">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:4px">
            <h2 id="modalTitle" style="margin:0">${escHtml(m.name)}</h2>
            ${availBadge}
          </div>
          <span class="modal-cat">${escHtml(CATEGORIES[m.category] || m.category)}</span>
          <div class="modal-params">
            ${m.age ? `<div class="modal-param"><label>Возраст</label><span>${escHtml(m.age)} лет</span></div>` : ''}
            ${m.height ? `<div class="modal-param"><label>Рост</label><span>${escHtml(m.height)} см</span></div>` : ''}
            ${m.bust && m.waist && m.hips ? `<div class="modal-param"><label>Параметры</label><span>${escHtml(m.bust)}/${escHtml(m.waist)}/${escHtml(m.hips)}</span></div>` : ''}
            ${m.shoe_size ? `<div class="modal-param"><label>Размер обуви</label><span>${escHtml(m.shoe_size)}</span></div>` : ''}
            ${m.hair_color ? `<div class="modal-param"><label>Цвет волос</label><span>${escHtml(m.hair_color)}</span></div>` : ''}
            ${m.eye_color ? `<div class="modal-param"><label>Цвет глаз</label><span>${escHtml(m.eye_color)}</span></div>` : ''}
            ${m.city ? `<div class="modal-param"><label>Город</label><span>${escHtml(m.city)}</span></div>` : ''}
            ${m.experience ? `<div class="modal-param"><label>Опыт</label><span>${escHtml(m.experience)}</span></div>` : ''}
          </div>
          ${m.bio ? `<div class="modal-bio"><p>${escHtml(m.bio)}</p></div>` : ''}
          ${m.instagram ? `<div class="modal-insta">📸 <a href="https://instagram.com/${encodeURIComponent(m.instagram.replace('@', ''))}" target="_blank" rel="noopener" style="color:var(--gold)">${escHtml(m.instagram)}</a></div>` : ''}
          <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:24px">
            <a href="/booking.html?model=${m.id}" class="btn-primary" style="padding:14px 32px;font-size:0.8rem;display:inline-flex;align-items:center;gap:8px"
               onclick="if(window.NM?.analytics) NM.analytics.startBooking(${m.id}, '${m.category || ''}')">
              📋 Забронировать
            </a>
            <button id="modal-fav-btn" onclick="window._toggleModalFav(${m.id})" class="btn-outline" style="padding:14px 24px;font-size:0.8rem;cursor:pointer;display:inline-flex;align-items:center;gap:6px">
              ${(function () {
                try {
                  const favs = JSON.parse(localStorage.getItem('nm_favorites') || '[]');
                  const isFavd = favs.some(f => (typeof f === 'object' ? f.id === m.id : f === m.id));
                  return isFavd ? '❤️ В избранном' : '🤍 В избранное';
                } catch (e) {
                  return '🤍 В избранное';
                }
              })()}
            </button>
            <button onclick="closeModal()" class="btn-outline" style="padding:14px 24px;font-size:0.8rem;cursor:pointer">
              Закрыть
            </button>
          </div>
        </div>`;

      const modal = document.getElementById('modelModal');
      modal.classList.add('open');
      document.body.style.overflow = 'hidden';
      modal.focus();
    })
    .catch(() => toast('Не удалось загрузить данные модели', 'error'));
}

function switchModalPhoto(src, idx) {
  const img = document.getElementById('modalMainImg');
  if (img) {
    img.style.opacity = '0';
    img.src = src;
    img.onload = () => {
      img.style.opacity = '1';
    };
    img.style.transition = 'opacity 0.2s ease';
  }
  window._currentModalPhotoIdx = idx;

  // Update active thumb
  document.querySelectorAll('.modal-thumb').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
  });
}

// Make global
window.openModelModal = openModelModal;
window.switchModalPhoto = switchModalPhoto;

/* ─── Navbar scroll ────────────────────────────────── */
const navbar = document.getElementById('navbar');
if (navbar) {
  window.addEventListener(
    'scroll',
    () => {
      navbar.classList.toggle('scrolled', window.scrollY > 80);
    },
    { passive: true }
  );
  // Apply immediately in case page loads mid-scroll
  navbar.classList.toggle('scrolled', window.scrollY > 80);
}

/* ─── Active nav link highlight ────────────────────── */
(function highlightActiveNav() {
  const current = location.pathname.split('/').pop() || 'index.html';
  document.querySelectorAll('.nav-links a, .nav-mobile a').forEach(a => {
    const href = a.getAttribute('href');
    if (!href) return;
    const hrefPage = href.split('/').pop();
    if (hrefPage === current || (href === '/' && (current === 'index.html' || current === ''))) {
      a.classList.add('active');
    }
  });
})();

/* ─── Smooth scroll for anchor links ──────────────── */
document.querySelectorAll('a[href^="#"]').forEach(link => {
  link.addEventListener('click', e => {
    const href = link.getAttribute('href');
    if (href === '#') return;
    const target = document.querySelector(href);
    if (!target) return;
    e.preventDefault();
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    // Close mobile menu if open
    closeMobileMenu();
  });
});

/* ─── Mobile menu ──────────────────────────────────── */
const burgerBtn = document.getElementById('burgerBtn');
const mobileMenu = document.getElementById('mobileMenu');
const mobileClose = document.getElementById('mobileClose');
const navOverlay = document.getElementById('navOverlay');

function openMobileMenu() {
  mobileMenu?.classList.add('open');
  navOverlay?.classList.add('open');
  burgerBtn?.classList.add('is-open');
  burgerBtn?.setAttribute('aria-expanded', 'true');
  document.body.style.overflow = 'hidden';
}

function closeMobileMenu() {
  mobileMenu?.classList.remove('open');
  navOverlay?.classList.remove('open');
  burgerBtn?.classList.remove('is-open');
  burgerBtn?.setAttribute('aria-expanded', 'false');
  document.body.style.overflow = '';
}

if (burgerBtn) {
  burgerBtn.addEventListener('click', openMobileMenu);
  mobileClose?.addEventListener('click', closeMobileMenu);
  navOverlay?.addEventListener('click', closeMobileMenu);
  mobileMenu?.querySelectorAll('a').forEach(a => a.addEventListener('click', closeMobileMenu));
  // Close on Escape
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && mobileMenu?.classList.contains('open')) closeMobileMenu();
  });
}

/* ─── Modal close ──────────────────────────────────── */
const modalOverlay = document.getElementById('modelModal');
document.getElementById('modalClose')?.addEventListener('click', closeModal);
modalOverlay?.addEventListener('click', e => {
  if (e.target === modalOverlay) closeModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && modalOverlay?.classList.contains('open')) closeModal();
});
function closeModal() {
  modalOverlay?.classList.remove('open');
  document.body.style.overflow = '';
}
window.closeModal = closeModal;

/* ─── Modal fav toggle ─────────────────────────────── */
window._toggleModalFav = function (modelId) {
  const FAV_KEY = 'nm_favorites';
  let favs = [];
  try {
    favs = JSON.parse(localStorage.getItem(FAV_KEY) || '[]');
  } catch {}
  const idx = favs.findIndex(f => (typeof f === 'object' ? f.id === modelId : f === modelId));
  const btn = document.getElementById('modal-fav-btn');
  if (idx === -1) {
    if (favs.length >= 50) {
      alert('Максимум 50 в избранном');
      return;
    }
    favs.push(modelId);
    try {
      localStorage.setItem(FAV_KEY, JSON.stringify(favs));
    } catch {}
    if (btn) btn.innerHTML = '❤️ В избранном';
  } else {
    favs.splice(idx, 1);
    try {
      localStorage.setItem(FAV_KEY, JSON.stringify(favs));
    } catch {}
    if (btn) btn.innerHTML = '🤍 В избранное';
  }
  // Sync badge in catalog if available
  if (typeof window._updateFavBadge === 'function') window._updateFavBadge();
};

/* ─── Counter animation ────────────────────────────── */
function animateCounters() {
  document.querySelectorAll('[data-count]').forEach(el => {
    const target = +el.dataset.count;
    let current = 0;
    const step = target / 60;
    const timer = setInterval(() => {
      current = Math.min(current + step, target);
      el.textContent = Math.floor(current) + (target >= 100 ? '+' : '');
      if (current >= target) clearInterval(timer);
    }, 16);
  });
}

const statsBar = document.querySelector('.stats-bar');
if (statsBar) {
  const observer = new IntersectionObserver(
    entries => {
      if (entries[0].isIntersecting) {
        animateCounters();
        observer.disconnect();
      }
    },
    { threshold: 0.3 }
  );
  observer.observe(statsBar);
}

/* ─── Featured models (index page) ─────────────────── */
const featuredGrid = document.getElementById('featuredGrid');
if (featuredGrid) {
  apiFetch('/models?available=1')
    .then(models => {
      const featured = models.slice(0, 3);
      if (!featured.length) {
        featuredGrid.innerHTML = '<p style="color:var(--text-muted)">Модели скоро появятся</p>';
        return;
      }
      featuredGrid.innerHTML = featured.map(m => modelCard(m, 'openModelModal')).join('');
      initLazyImages();
    })
    .catch(() => {
      featuredGrid.innerHTML = '';
    });
}

/* ─── Reviews section ──────────────────────────────── */
const reviewsContainer = document.getElementById('reviews-container');
const reviewsSection = document.getElementById('reviews');
if (reviewsContainer) {
  apiFetch('/reviews?limit=6')
    .then(data => {
      // API may return array directly or {reviews:[...]} object
      const reviews = Array.isArray(data) ? data : data && Array.isArray(data.reviews) ? data.reviews : [];
      if (!reviews.length) {
        // Hide the whole section if no approved reviews
        if (reviewsSection) reviewsSection.style.display = 'none';
        return;
      }
      reviewsContainer.innerHTML = reviews
        .slice(0, 6)
        .map((r, i) => {
          const name = r.client_name || r.author_name || 'Клиент';
          const initials =
            name
              .split(' ')
              .map(w => w[0] || '')
              .slice(0, 2)
              .join('')
              .toUpperCase() || 'К';
          const rating = Math.max(1, Math.min(5, r.rating || 5));
          const starsHtml = Array.from(
            { length: 5 },
            (_, i) => `<span style="color:${i < rating ? '#f5c542' : 'rgba(255,255,255,0.15)'}">★</span>`
          ).join('');
          const dateStr = r.created_at
            ? new Date(r.created_at).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' })
            : '';
          return `
          <div class="testimonial-card" style="animation-delay:${i * 0.07}s">
            <div class="testimonial-stars">${starsHtml}</div>
            <p class="testimonial-text">${r.text ? `«${escHtml(r.text)}»` : ''}</p>
            <div class="testimonial-author">
              <div class="testimonial-avatar">${escHtml(initials)}</div>
              <div>
                <strong>${escHtml(name)}</strong>
                ${r.model_name ? `<span>о модели ${escHtml(r.model_name)}</span>` : ''}
                ${dateStr ? `<span style="display:block;font-size:0.72rem;color:var(--text-dim);margin-top:2px">${escHtml(dateStr)}</span>` : ''}
              </div>
            </div>
          </div>`;
        })
        .join('');
    })
    .catch(() => {
      // Hide section on API error too
      if (reviewsSection) reviewsSection.style.display = 'none';
    });
}

/* ─── Contact section dynamic data ─────────────────── */
(function loadContacts() {
  const phoneEl = document.getElementById('contacts-phone');
  const emailEl = document.getElementById('contacts-email');
  if (!phoneEl && !emailEl) return;

  apiFetch('/settings')
    .then(s => {
      if (s.contacts_phone && phoneEl) {
        const clean = s.contacts_phone.replace(/[^+\d]/g, '');
        phoneEl.href = `tel:${clean}`;
        phoneEl.textContent = s.contacts_phone;
      }
      if (s.contacts_email && emailEl) {
        emailEl.href = `mailto:${s.contacts_email}`;
        emailEl.textContent = s.contacts_email;
      }
    })
    .catch(() => {});
})();

/* ─── Dynamic settings: about-text, hero-title/subtitle ── */
(function loadDynamicSettings() {
  apiFetch('/settings')
    .then(s => {
      const aboutEl = document.getElementById('about-text');
      if (aboutEl && s.about) {
        aboutEl.textContent = s.about;
      }

      const heroTitle = document.getElementById('hero-title');
      if (heroTitle && s.greeting) {
        // greeting replaces only the text before the <em> tag if present
        const em = heroTitle.querySelector('em');
        if (em) {
          heroTitle.childNodes.forEach(node => {
            if (node.nodeType === Node.TEXT_NODE) node.textContent = s.greeting + '\n';
          });
        } else {
          heroTitle.textContent = s.greeting;
        }
      }

      const heroSubtitle = document.getElementById('hero-subtitle');
      if (heroSubtitle && s.hero_subtitle) {
        heroSubtitle.textContent = s.hero_subtitle;
      }

      const pricingText = document.getElementById('pricing-text');
      if (pricingText && s.pricing_text) {
        pricingText.textContent = s.pricing_text;
      }
    })
    .catch(() => {});
})();

/* ─── FAQ accordion ────────────────────────────────── */
(function initFaqAccordion() {
  const faqSection = document.getElementById('faq-section');
  if (!faqSection) return;

  faqSection.querySelectorAll('.faq-item').forEach(item => {
    const btn = item.querySelector('.faq-question');
    const answer = item.querySelector('.faq-answer');
    if (!btn || !answer) return;

    btn.addEventListener('click', () => {
      const isOpen = btn.getAttribute('aria-expanded') === 'true';

      // Close all other items (accordion behaviour)
      faqSection.querySelectorAll('.faq-item').forEach(other => {
        const otherBtn = other.querySelector('.faq-question');
        const otherAns = other.querySelector('.faq-answer');
        const otherIcon = other.querySelector('.faq-icon');
        if (otherBtn && otherAns) {
          otherBtn.setAttribute('aria-expanded', 'false');
          otherAns.hidden = true;
          otherAns.setAttribute('aria-hidden', 'true');
          if (otherIcon) otherIcon.textContent = '+';
        }
      });

      // Toggle clicked item
      if (!isOpen) {
        btn.setAttribute('aria-expanded', 'true');
        answer.hidden = false;
        answer.setAttribute('aria-hidden', 'false');
        const icon = btn.querySelector('.faq-icon');
        if (icon) icon.textContent = '−';
      }
    });
  });
})();

/* ─── Scroll animation for [data-animate] sections ─── */
(function initScrollAnimations() {
  // Guard: if animations.js already initialized, skip to avoid double-init
  if (window._nm_scroll_anim_init) return;
  window._nm_scroll_anim_init = true;

  if (!('IntersectionObserver' in window)) return;
  const observer = new IntersectionObserver(
    entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('section-visible', 'is-visible');
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.08 }
  );

  document.querySelectorAll('[data-animate]').forEach(el => {
    el.classList.add('section-hidden');
    observer.observe(el);
  });
})();

/* ─── Analytics: CTA click tracking (homepage) ─────── */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('a[href*="booking"]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (window.NM && NM.analytics) {
        NM.analytics.event('cta_click', { location: 'homepage' });
      }
      // GA4: standard click event with CTA category
      if (window.gtag) {
        gtag('event', 'click', {
          event_category: 'CTA',
          event_label: 'homepage_cta',
          page_location: window.location.pathname,
        });
      }
    });
  });
});

/* ─── Init lazy images for any pre-rendered cards ─── */
initLazyImages();

/* ─── PWA Install Prompt ────────────────────────── */
(function initInstallPrompt() {
  let deferredPrompt = null;

  // Don't show if already installed (standalone mode)
  if (window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone) return;

  window.addEventListener('beforeinstallprompt', e => {
    e.preventDefault();
    deferredPrompt = e;

    // Show install banner after 30 seconds (only once per session)
    if (sessionStorage.getItem('nm-install-dismissed')) return;
    setTimeout(() => {
      if (!deferredPrompt) return;
      const banner = document.getElementById('install-banner');
      if (banner) banner.style.display = 'flex';
    }, 30000);
  });

  // Install button click
  document.addEventListener('click', async e => {
    const btn = e.target.closest('#install-btn');
    if (!btn || !deferredPrompt) return;
    const prompt = deferredPrompt;
    deferredPrompt = null;
    prompt.prompt();
    const { outcome } = await prompt.userChoice;
    if (outcome === 'accepted') {
      document.getElementById('install-banner')?.remove();
    }
  });

  // Dismiss button — hide for session
  document.addEventListener('click', e => {
    const closeBtn = e.target.closest('[data-dismiss="install-banner"]');
    if (!closeBtn) return;
    document.getElementById('install-banner')?.remove();
    sessionStorage.setItem('nm-install-dismissed', '1');
  });

  // Hide banner when app is installed
  window.addEventListener('appinstalled', () => {
    document.getElementById('install-banner')?.remove();
    deferredPrompt = null;
  });
})();

/* ─── Track recently visited pages for offline.html ─── */
(function trackRecentPages() {
  try {
    const KEY = 'nm-recent-pages';
    const MAX = 10;
    const url = location.pathname + location.search;
    const title = document.title || url;
    const now = Date.now();

    let pages = [];
    try {
      pages = JSON.parse(localStorage.getItem(KEY) || '[]');
      if (!Array.isArray(pages)) pages = [];
    } catch (_) {
      pages = [];
    }

    // Remove duplicate URL if present
    pages = pages.filter(p => p.url !== url);

    // Prepend current page
    pages.unshift({ url, title, ts: now });

    // Keep only newest MAX entries
    if (pages.length > MAX) pages = pages.slice(0, MAX);

    localStorage.setItem(KEY, JSON.stringify(pages));
  } catch (_) {
    // localStorage may be unavailable (private mode, quota exceeded)
  }
})();

/* ─── Card shine mouse tracking ─────────────────────────────── */
document.querySelectorAll('.card-shine').forEach(card => {
  card.addEventListener('mousemove', e => {
    const rect = card.getBoundingClientRect();
    const x = (((e.clientX - rect.left) / rect.width) * 100).toFixed(1);
    const y = (((e.clientY - rect.top) / rect.height) * 100).toFixed(1);
    card.style.setProperty('--mouse-x', x + '%');
    card.style.setProperty('--mouse-y', y + '%');
  });
});
