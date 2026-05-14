/* Catalog page — filters: name, category, city, age range, hair, height, availability */
(async function () {
  const grid          = document.getElementById('catalogGrid');
  const countEl       = document.getElementById('catalogCount');
  const searchInput   = document.getElementById('searchInput');
  const resetBtn      = document.getElementById('resetFilters');
  const minHeightEl   = document.getElementById('minHeight');
  const maxHeightEl   = document.getElementById('maxHeight');
  const citySelect    = document.getElementById('cityFilter');
  const availCheckbox = document.getElementById('availableOnly');
  const sortEl        = document.getElementById('sortSelect');

  let allModels = [];
  let filters = {
    category: '',
    hair: '',
    city: '',
    ageMin: '',
    ageMax: '',
    availableOnly: false,
    sort: 'default',
  };

  // ── Load models ──────────────────────────────────────────────────────────────
  try {
    const res = await fetch('/api/models');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    allModels = await res.json();
  } catch (e) {
    grid.innerHTML = '<p class="no-results">Ошибка загрузки. Попробуйте обновить страницу.</p>';
    return;
  }

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
  searchInput?.addEventListener('input', debounce(render, 250));
  minHeightEl?.addEventListener('input', debounce(render, 400));
  maxHeightEl?.addEventListener('input', debounce(render, 400));

  // ── Reset ────────────────────────────────────────────────────────────────────
  resetBtn?.addEventListener('click', () => {
    filters = { category: '', hair: '', city: '', ageMin: '', ageMax: '', availableOnly: false, sort: 'default' };

    // Deactivate all tags, then activate the "all/any" defaults
    document.querySelectorAll('#categoryFilters .filter-tag').forEach(t => t.classList.toggle('active', t.dataset.value === ''));
    document.querySelectorAll('#hairFilters .filter-tag').forEach(t => t.classList.toggle('active', t.dataset.value === ''));
    document.querySelectorAll('#ageFilters .filter-tag').forEach(t => t.classList.toggle('active', t.dataset.min === '' && t.dataset.max === ''));

    if (searchInput)   searchInput.value    = '';
    if (minHeightEl)   minHeightEl.value    = '';
    if (maxHeightEl)   maxHeightEl.value    = '';
    if (citySelect)    citySelect.value     = '';
    if (availCheckbox) availCheckbox.checked = false;
    if (sortEl)        sortEl.value         = 'default';
    render();
  });

  // ── Experience label ─────────────────────────────────────────────────────────
  function experienceBadge(m) {
    // Derive experience from bio length / category as a heuristic
    // In absence of a dedicated field, use age: <21 = Junior, 21-25 = Middle, 26+ = Senior
    if (!m.age) return '';
    if (m.age < 21)  return '<span class="exp-badge exp-junior">Начинающая</span>';
    if (m.age <= 25) return '<span class="exp-badge exp-mid">Опыт</span>';
    return '<span class="exp-badge exp-senior">Профи</span>';
  }

  const CAT_LABELS = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };

  // ── Render ───────────────────────────────────────────────────────────────────
  function render() {
    const q    = (searchInput?.value || '').toLowerCase().trim();
    const minH = parseInt(minHeightEl?.value) || 0;
    const maxH = parseInt(maxHeightEl?.value) || 999;
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
      case 'height_asc':  list = [...list].sort((a, b) => (a.height || 0) - (b.height || 0)); break;
      case 'height_desc': list = [...list].sort((a, b) => (b.height || 0) - (a.height || 0)); break;
      case 'name_asc':    list = [...list].sort((a, b) => a.name.localeCompare(b.name, 'ru')); break;
      case 'available':   list = [...list].sort((a, b) => b.available - a.available); break;
      case 'age_asc':     list = [...list].sort((a, b) => (a.age || 99) - (b.age || 99)); break;
      default: break;
    }

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
