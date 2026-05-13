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
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ'}</span> ${msg}`;
  container.appendChild(t);
  setTimeout(() => t.remove(), 4000);
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

function openModelModal(id) {
  apiFetch(`/models/${id}`)
    .then(m => {
      const photos = m.photos && m.photos.length ? m.photos : [];
      const allPhotos = m.photo_main ? [m.photo_main, ...photos.filter(p => p !== m.photo_main)] : photos;
      const thumbsHtml = allPhotos.slice(0, 6).map((p, i) =>
        `<div class="modal-thumb" onclick="document.getElementById('modalMainImg').src='${p}'">
           <img src="${p}" alt="${m.name} ${i + 1}" />
         </div>`
      ).join('');

      document.getElementById('modalInner').innerHTML = `
        <div class="modal-gallery">
          <div class="modal-main-img">
            ${m.photo_main || allPhotos[0]
              ? `<img id="modalMainImg" src="${m.photo_main || allPhotos[0]}" alt="${m.name}" />`
              : `<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:var(--bg3);font-family:'Playfair Display',serif;font-size:6rem;color:rgba(201,169,110,0.15)">${m.name[0]}</div>`
            }
          </div>
          ${thumbsHtml ? `<div class="modal-thumbs">${thumbsHtml}</div>` : ''}
        </div>
        <div class="modal-info">
          <h2>${m.name}</h2>
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

      const modal = document.getElementById('modelModal');
      modal.classList.add('open');
      document.body.style.overflow = 'hidden';
    })
    .catch(() => toast('Не удалось загрузить данные модели', 'error'));
}

// Make global
window.openModelModal = openModelModal;

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
