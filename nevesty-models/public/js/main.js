/* ─── Shared utilities ─────────────────────────────── */
const API = '/api';

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts
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
  const avail = m.available ? '' : '<div class="model-avail unavailable"></div>';
  const availDot = m.available ? '<div class="model-avail"></div>' : '<div class="model-avail unavailable"></div>';
  return `
    <div class="model-card" onclick="${onClick}(${m.id})" role="button" tabindex="0">
      ${availDot}
      <div class="model-card-img">
        ${m.photo_main
          ? `<img src="${m.photo_main}" alt="${m.name}" loading="lazy" />`
          : placeholderImg(m.name)
        }
        <div class="model-card-overlay">
          <div class="model-card-tag">${CATEGORIES[m.category] || m.category}</div>
          <div style="font-size:0.8rem;color:#ccc">
            ${m.height}см · ${m.bust}/${m.waist}/${m.hips} · р.${m.shoe_size}
          </div>
        </div>
      </div>
      <div class="model-card-info">
        <div class="model-card-name">${m.name}</div>
        <div class="model-card-meta">${m.height} см · ${m.hair_color} · ${m.eye_color}</div>
      </div>
    </div>`;
}

/* ─── Lightbox ─────────────────────────────────────── */
(function initLightbox() {
  if (document.getElementById('lightbox')) return;

  // Inject styles
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
  `;
  document.head.appendChild(style);

  // Inject DOM
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
    // restore body overflow only if model modal is also closed
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

  function lbPrev() { _idx = (_idx - 1 + _photos.length) % _photos.length; _render(); }
  function lbNext() { _idx = (_idx + 1) % _photos.length; _render(); }

  document.getElementById('lb-close').addEventListener('click', lbClose);
  document.getElementById('lb-prev').addEventListener('click', lbPrev);
  document.getElementById('lb-next').addEventListener('click', lbNext);
  lb.addEventListener('click', e => { if (e.target === lb) lbClose(); });

  document.addEventListener('keydown', e => {
    if (!lb.classList.contains('lb-open')) return;
    if (e.key === 'Escape') lbClose();
    if (e.key === 'ArrowLeft') lbPrev();
    if (e.key === 'ArrowRight') lbNext();
  });

  // Expose for use in openModelModal
  window._lightbox = { show: lbShow };
})();

function openModelModal(id) {
  apiFetch(`/models/${id}`)
    .then(m => {
      const photos = m.photos && m.photos.length ? m.photos : [];
      const allPhotos = m.photo_main ? [m.photo_main, ...photos.filter(p => p !== m.photo_main)] : photos;
      const thumbsHtml = allPhotos.slice(0, 6).map((p, i) =>
        `<div class="modal-thumb" onclick="switchModalPhoto('${p}',${i})">
           <img src="${p}" alt="${m.name} ${i + 1}" />
         </div>`
      ).join('');

      // Store photos on window for lightbox access from inline handlers
      window._currentModalPhotos = allPhotos;

      document.getElementById('modalInner').innerHTML = `
        <div class="modal-gallery">
          <div class="modal-main-img">
            ${m.photo_main || allPhotos[0]
              ? `<img id="modalMainImg" src="${m.photo_main || allPhotos[0]}" alt="${m.name}"
                   onclick="window._lightbox && window._lightbox.show(window._currentModalPhotos, window._currentModalPhotoIdx || 0)"
                   title="Нажмите для просмотра" />`
              : `<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:var(--bg3);font-family:'Playfair Display',serif;font-size:6rem;color:rgba(201,169,110,0.15)">${m.name[0]}</div>`
            }
          </div>
          ${thumbsHtml ? `<div class="modal-thumbs">${thumbsHtml}</div>` : ''}
        </div>
        <div class="modal-info">
          <h2 id="modalTitle">${m.name}</h2>
          <span class="modal-cat">${CATEGORIES[m.category] || m.category}</span>
          <div class="modal-params">
            <div class="modal-param"><label>Возраст</label><span>${m.age || '—'} лет</span></div>
            <div class="modal-param"><label>Рост</label><span>${m.height || '—'} см</span></div>
            <div class="modal-param"><label>Параметры</label><span>${m.bust}/${m.waist}/${m.hips}</span></div>
            <div class="modal-param"><label>Размер обуви</label><span>${m.shoe_size || '—'}</span></div>
            <div class="modal-param"><label>Цвет волос</label><span>${m.hair_color || '—'}</span></div>
            <div class="modal-param"><label>Цвет глаз</label><span>${m.eye_color || '—'}</span></div>
          </div>
          ${m.bio ? `<div class="modal-bio">${m.bio}</div>` : ''}
          ${m.instagram ? `<div class="modal-insta">📸 ${m.instagram}</div>` : ''}
          <div style="display:flex;gap:12px;flex-wrap:wrap">
            <a href="/booking.html?model=${m.id}" class="btn-primary" style="padding:12px 28px;font-size:0.75rem">Забронировать</a>
            ${!m.available ? '<span style="font-size:0.8rem;color:var(--text-muted);align-self:center">Временно недоступна</span>' : ''}
          </div>
        </div>`;

      window._currentModalPhotoIdx = 0;

      const modal = document.getElementById('modelModal');
      modal.classList.add('open');
      document.body.style.overflow = 'hidden';
      modal.focus();
    })
    .catch(() => toast('Не удалось загрузить данные модели', 'error'));
}

function switchModalPhoto(src, idx) {
  const img = document.getElementById('modalMainImg');
  if (img) img.src = src;
  window._currentModalPhotoIdx = idx;
}

// Make global
window.openModelModal = openModelModal;
window.switchModalPhoto = switchModalPhoto;

/* ─── Navbar scroll ────────────────────────────────── */
const navbar = document.getElementById('navbar');
if (navbar && !navbar.classList.contains('scrolled')) {
  window.addEventListener('scroll', () => {
    navbar.classList.toggle('scrolled', window.scrollY > 60);
  });
}

/* ─── Mobile menu ──────────────────────────────────── */
const burgerBtn = document.getElementById('burgerBtn');
const mobileMenu = document.getElementById('mobileMenu');
const mobileClose = document.getElementById('mobileClose');
if (burgerBtn) {
  burgerBtn.addEventListener('click', () => mobileMenu.classList.add('open'));
  mobileClose?.addEventListener('click', () => mobileMenu.classList.remove('open'));
  mobileMenu?.querySelectorAll('a').forEach(a => a.addEventListener('click', () => mobileMenu.classList.remove('open')));
}

/* ─── Modal close ──────────────────────────────────── */
const modalOverlay = document.getElementById('modelModal');
document.getElementById('modalClose')?.addEventListener('click', closeModal);
modalOverlay?.addEventListener('click', e => { if (e.target === modalOverlay) closeModal(); });
function closeModal() {
  modalOverlay?.classList.remove('open');
  document.body.style.overflow = '';
}

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

// Trigger counter when stats bar visible
const statsBar = document.querySelector('.stats-bar');
if (statsBar) {
  const observer = new IntersectionObserver(entries => {
    if (entries[0].isIntersecting) { animateCounters(); observer.disconnect(); }
  }, { threshold: 0.3 });
  observer.observe(statsBar);
}

/* ─── Featured models (index page) ─────────────────── */
const featuredGrid = document.getElementById('featuredGrid');
if (featuredGrid) {
  apiFetch('/models?available=1')
    .then(models => {
      const featured = models.slice(0, 3);
      if (!featured.length) { featuredGrid.innerHTML = '<p style="color:var(--text-muted)">Модели скоро появятся</p>'; return; }
      featuredGrid.innerHTML = featured.map(m => modelCard(m, 'openModelModal')).join('');
    })
    .catch(() => { featuredGrid.innerHTML = ''; });
}
