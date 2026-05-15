/* Catalog page — filters: name, category, city, age range, hair, height, availability, sort */
(async function () {
  const grid          = document.getElementById('catalogGrid');
  const skeleton      = document.getElementById('catalogSkeleton');
  const countEl       = document.getElementById('catalogCount');
  const searchInput   = document.getElementById('searchInput');
  const resetBtn      = document.getElementById('resetFilters');
  const minHeightEl   = document.getElementById('minHeight');
  const maxHeightEl   = document.getElementById('maxHeight');
  const citySelect    = document.getElementById('cityFilter');
  const availCheckbox = document.getElementById('availableOnly');
  const sortEl        = document.getElementById('sortSelect');

  let allModels = [];

  // ── Read URL params ──────────────────────────────────────────────────────────
  function getUrlParams() {
    const p = new URLSearchParams(window.location.search);
    return {
      category:      p.get('category') || '',
      hair:          p.get('hair') || '',
      city:          p.get('city') || '',
      ageMin:        p.get('ageMin') || '',
      ageMax:        p.get('ageMax') || '',
      availableOnly: p.get('available') === '1',
      sort:          p.get('sort') || 'default',
      search:        p.get('q') || '',
      minHeight:     p.get('minH') || '',
      maxHeight:     p.get('maxH') || '',
    };
  }

  // ── Write URL params (debounced) ─────────────────────────────────────────────
  let urlUpdateTimer;
  function updateUrl(filters) {
    clearTimeout(urlUpdateTimer);
    urlUpdateTimer = setTimeout(() => {
      const p = new URLSearchParams();
      if (filters.category)    p.set('category', filters.category);
      if (filters.hair)        p.set('hair', filters.hair);
      if (filters.city)        p.set('city', filters.city);
      if (filters.ageMin)      p.set('ageMin', filters.ageMin);
      if (filters.ageMax)      p.set('ageMax', filters.ageMax);
      if (filters.availableOnly) p.set('available', '1');
      if (filters.sort && filters.sort !== 'default') p.set('sort', filters.sort);
      if (filters.search)      p.set('q', filters.search);
      if (filters.minHeight)   p.set('minH', filters.minHeight);
      if (filters.maxHeight)   p.set('maxH', filters.maxHeight);
      const qs = p.toString();
      const newUrl = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
      window.history.replaceState({}, '', newUrl);
    }, 300);
  }

  const urlParams = getUrlParams();
  let filters = { ...urlParams };

  // ── Show skeleton, load models ───────────────────────────────────────────────
  if (skeleton) skeleton.style.display = '';
  if (grid)     grid.style.display = 'none';
  countEl.textContent = 'Загрузка...';

  try {
    const res = await fetch('/api/models');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    allModels = await res.json();
  } catch (e) {
    if (skeleton) skeleton.style.display = 'none';
    if (grid) grid.style.display = '';
    grid.innerHTML = '<p class="no-results">Ошибка загрузки. Попробуйте обновить страницу.</p>';
    countEl.textContent = '';
    return;
  }

  // Hide skeleton, show grid
  if (skeleton) skeleton.style.display = 'none';
  if (grid) grid.style.display = '';

  // ── Populate city dropdown dynamically ───────────────────────────────────────
  const cities = [...new Set(allModels.map(m => m.city).filter(Boolean))].sort((a, b) =>
    a.localeCompare(b, 'ru')
  );
  cities.forEach(city => {
    const opt = document.createElement('option');
    opt.value = city;
    opt.textContent = city;
    citySelect.appendChild(opt);
  });

  // ── Apply initial values from URL ────────────────────────────────────────────
  function applyUrlParamsToUI() {
    if (searchInput)   searchInput.value = filters.search || '';
    if (minHeightEl)   minHeightEl.value = filters.minHeight || '';
    if (maxHeightEl)   maxHeightEl.value = filters.maxHeight || '';
    if (citySelect)    citySelect.value  = filters.city || '';
    if (availCheckbox) availCheckbox.checked = filters.availableOnly;
    if (sortEl)        sortEl.value      = filters.sort || 'default';

    // Category tags
    document.querySelectorAll('#categoryFilters .filter-tag').forEach(t => {
      t.classList.toggle('active', t.dataset.value === (filters.category || ''));
    });
    // Hair tags
    document.querySelectorAll('#hairFilters .filter-tag').forEach(t => {
      t.classList.toggle('active', t.dataset.value === (filters.hair || ''));
    });
    // Age tags
    document.querySelectorAll('#ageFilters .filter-tag').forEach(t => {
      const matches = (t.dataset.min || '') === (filters.ageMin || '') &&
                      (t.dataset.max || '') === (filters.ageMax || '');
      t.classList.toggle('active', matches);
    });
  }
  applyUrlParamsToUI();

  // ── Filter-tag helpers ───────────────────────────────────────────────────────
  function bindTagGroup(containerId, onSelect) {
    document.querySelectorAll(`#${containerId} .filter-tag`).forEach(el => {
      el.addEventListener('click', () => {
        document.querySelectorAll(`#${containerId} .filter-tag`).forEach(t => t.classList.remove('active'));
        el.classList.add('active');
        onSelect(el);
        render();
      });
    });
  }

  bindTagGroup('categoryFilters', el => { filters.category = el.dataset.value; });
  bindTagGroup('hairFilters',     el => { filters.hair = el.dataset.value; });
  bindTagGroup('ageFilters',      el => {
    filters.ageMin = el.dataset.min || '';
    filters.ageMax = el.dataset.max || '';
  });

  // ── City, availability, sort ─────────────────────────────────────────────────
  citySelect?.addEventListener('change', () => { filters.city = citySelect.value; render(); });
  availCheckbox?.addEventListener('change', () => { filters.availableOnly = availCheckbox.checked; render(); });
  sortEl?.addEventListener('change', () => { filters.sort = sortEl.value; render(); });

  // ── Debounced inputs ─────────────────────────────────────────────────────────
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }
  searchInput?.addEventListener('input', debounce(() => {
    filters.search = searchInput.value.trim();
    render();
  }, 250));
  minHeightEl?.addEventListener('input', debounce(() => {
    filters.minHeight = minHeightEl.value;
    render();
  }, 400));
  maxHeightEl?.addEventListener('input', debounce(() => {
    filters.maxHeight = maxHeightEl.value;
    render();
  }, 400));

  // ── Reset ────────────────────────────────────────────────────────────────────
  resetBtn?.addEventListener('click', () => {
    filters = { category: '', hair: '', city: '', ageMin: '', ageMax: '', availableOnly: false, sort: 'default', search: '', minHeight: '', maxHeight: '' };
    applyUrlParamsToUI();
    render();
  });

  // ── Experience label ─────────────────────────────────────────────────────────
  function experienceBadge(m) {
    if (!m.age) return '';
    if (m.age < 21)  return '<span class="exp-badge exp-junior">Начинающая</span>';
    if (m.age <= 25) return '<span class="exp-badge exp-mid">Опыт</span>';
    return '<span class="exp-badge exp-senior">Профи</span>';
  }

  const CAT_LABELS = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };

  // ── Render ───────────────────────────────────────────────────────────────────
  function render() {
    const q    = (filters.search || searchInput?.value || '').toLowerCase().trim();
    const minH = parseInt(filters.minHeight || minHeightEl?.value) || 0;
    const maxH = parseInt(filters.maxHeight || maxHeightEl?.value) || 999;
    const minA = filters.ageMin ? parseInt(filters.ageMin) : 0;
    const maxA = filters.ageMax ? parseInt(filters.ageMax) : 999;

    let list = allModels.filter(m => {
      if (filters.category && m.category !== filters.category) return false;
      if (filters.hair && m.hair_color !== filters.hair) return false;
      if (filters.city && m.city !== filters.city) return false;
      if (filters.availableOnly && !m.available) return false;
      if (q && !m.name.toLowerCase().includes(q)) return false;
      if (minH && m.height < minH) return false;
      if (maxH < 999 && m.height > maxH) return false;
      if (minA && m.age < minA) return false;
      if (maxA < 999 && m.age > maxA) return false;
      return true;
    });

    // Sort
    switch (filters.sort) {
      case 'featured':    list = [...list].sort((a, b) => (b.available - a.available) || (b.id - a.id)); break;
      case 'name_asc':    list = [...list].sort((a, b) => a.name.localeCompare(b.name, 'ru')); break;
      case 'newest':      list = [...list].sort((a, b) => b.id - a.id); break;
      case 'available':   list = [...list].sort((a, b) => b.available - a.available); break;
      case 'height_asc':  list = [...list].sort((a, b) => (a.height || 0) - (b.height || 0)); break;
      case 'height_desc': list = [...list].sort((a, b) => (b.height || 0) - (a.height || 0)); break;
      case 'age_asc':     list = [...list].sort((a, b) => (a.age || 99) - (b.age || 99)); break;
      default: break;
    }

    // Update URL
    updateUrl(filters);

    // Count
    countEl.textContent = `Найдено ${list.length} ${plural(list.length)}`;

    if (!list.length) {
      grid.innerHTML = `
        <div class="no-results">
          <p style="font-size:2rem;margin-bottom:12px">🔍</p>
          <p>Модели не найдены<br>
          <span style="font-size:0.8rem;color:var(--text-dim)">Попробуйте изменить фильтры</span></p>
        </div>`;
      return;
    }

    grid.innerHTML = list.map(m => {
      const statusClass = m.available ? 'status-free' : 'status-busy';
      const statusText  = m.available ? 'Свободна' : 'Занята';
      const catLabel    = CAT_LABELS[m.category] || m.category || '';
      const measures    = [m.bust, m.waist, m.hips].filter(Boolean).join('/');

      return `
        <article class="model-card" tabindex="0" role="button"
          aria-label="Подробнее о модели ${escHtml(m.name)}"
          onclick="openModelModal(${m.id})"
          onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openModelModal(${m.id})}">

          <div class="model-avail${m.available ? '' : ' unavailable'}" aria-hidden="true"></div>

          <div class="model-card-img">
            ${m.photo_main
              ? `<img src="${escHtml(m.photo_main)}" alt="${escHtml(m.name)}" loading="lazy" />`
              : `<div class="model-card-placeholder" aria-hidden="true">${escHtml(m.name[0])}</div>`}
            <div class="model-card-overlay" aria-hidden="true">
              <div class="model-card-tag">${escHtml(catLabel)}</div>
              ${measures ? `<div style="font-size:0.78rem;color:#ccc;margin-top:4px">${escHtml(measures)}</div>` : ''}
            </div>
          </div>

          <div class="model-card-info">
            <div class="model-card-name">${escHtml(m.name)}</div>
            <div class="model-card-chips">
              ${m.height ? `<span class="mc-chip">${m.height} см</span>` : ''}
              ${m.age    ? `<span class="mc-chip">${m.age} лет</span>` : ''}
              ${m.city   ? `<span class="mc-chip mc-city">${escHtml(m.city)}</span>` : ''}
            </div>
            <div class="model-card-bottom">
              ${experienceBadge(m)}
              <span class="model-status-badge ${statusClass}">${statusText}</span>
            </div>
            <a href="/model.html?id=${m.id}"
               class="btn-book-model"
               onclick="event.stopPropagation()"
               aria-label="Открыть профиль модели ${escHtml(m.name)}">
              Подробнее
            </a>
          </div>
        </article>`;
    }).join('');
  }

  // ── Helpers ──────────────────────────────────────────────────────────────────
  function plural(n) {
    const m10 = n % 10, m100 = n % 100;
    if (m100 >= 11 && m100 <= 19) return 'моделей';
    if (m10 === 1)                 return 'модель';
    if (m10 >= 2 && m10 <= 4)     return 'модели';
    return 'моделей';
  }

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  render();
})();
