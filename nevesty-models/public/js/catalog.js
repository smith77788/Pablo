/* Catalog page */
(async function () {
  const grid    = document.getElementById('catalogGrid');
  const count   = document.getElementById('catalogCount');
  const search  = document.getElementById('searchInput');
  const reset   = document.getElementById('resetFilters');
  const minH    = document.getElementById('minHeight');
  const maxH    = document.getElementById('maxHeight');

  let allModels = [];
  let filters   = { category: '', hair: '', avail: '', sort: 'default' };

  // Load
  try {
    allModels = await (await fetch('/api/models')).json();
  } catch (e) {
    grid.innerHTML = '<p class="no-results">Ошибка загрузки. Попробуйте обновить страницу.</p>';
    return;
  }

  // Category filter
  document.querySelectorAll('#categoryFilters .filter-tag').forEach(el => {
    el.addEventListener('click', () => {
      document.querySelectorAll('#categoryFilters .filter-tag').forEach(t => t.classList.remove('active'));
      el.classList.add('active');
      filters.category = el.dataset.value;
      render();
    });
  });

  // Hair filter
  document.querySelectorAll('#hairFilters .filter-tag').forEach(el => {
    el.addEventListener('click', () => {
      document.querySelectorAll('#hairFilters .filter-tag').forEach(t => t.classList.remove('active'));
      el.classList.add('active');
      filters.hair = el.dataset.value;
      render();
    });
  });

  // Avail filter
  document.querySelectorAll('#availFilters .filter-tag').forEach(el => {
    el.addEventListener('click', () => {
      document.querySelectorAll('#availFilters .filter-tag').forEach(t => t.classList.remove('active'));
      el.classList.add('active');
      filters.avail = el.dataset.value;
      render();
    });
  });

  // Sort
  const sortEl = document.getElementById('sortSelect');
  if (sortEl) sortEl.addEventListener('change', () => { filters.sort = sortEl.value; render(); });

  // Search & height
  search?.addEventListener('input', debounce(render, 250));
  minH?.addEventListener('input', debounce(render, 400));
  maxH?.addEventListener('input', debounce(render, 400));

  // Reset
  reset?.addEventListener('click', () => {
    filters = { category: '', hair: '', avail: '', sort: 'default' };
    document.querySelectorAll('.filter-tag').forEach(t => {
      t.classList.toggle('active', t.dataset.value === '');
    });
    if (search) search.value = '';
    if (minH)  minH.value = '';
    if (maxH)  maxH.value = '';
    if (sortEl) sortEl.value = 'default';
    render();
  });

  function debounce(fn, ms) {
    let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }

  const CAT_LABELS = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };

  function render() {
    const q    = (search?.value || '').toLowerCase().trim();
    const minv = parseInt(minH?.value) || 0;
    const maxv = parseInt(maxH?.value) || 999;

    let list = allModels.filter(m => {
      if (filters.category && m.category !== filters.category) return false;
      if (filters.hair && m.hair_color !== filters.hair) return false;
      if (filters.avail === '1' && !m.available) return false;
      if (q && !m.name.toLowerCase().includes(q)) return false;
      if (minv && m.height < minv) return false;
      if (maxv < 999 && m.height > maxv) return false;
      return true;
    });

    // Sort
    if (filters.sort === 'height_asc')  list = [...list].sort((a,b) => a.height - b.height);
    if (filters.sort === 'height_desc') list = [...list].sort((a,b) => b.height - a.height);
    if (filters.sort === 'name_asc')    list = [...list].sort((a,b) => a.name.localeCompare(b.name,'ru'));
    if (filters.sort === 'available')   list = [...list].sort((a,b) => b.available - a.available);

    count.textContent = `Найдено: ${list.length} ${plural(list.length)}`;

    if (!list.length) {
      grid.innerHTML = '<div class="no-results"><p style="font-size:2rem;margin-bottom:12px">🔍</p><p>Модели не найдены<br><span style="font-size:0.8rem;color:var(--text-dim)">Попробуйте изменить фильтры</span></p></div>';
      return;
    }

    grid.innerHTML = list.map(m => `
      <div class="model-card" onclick="openModelModal(${m.id})" role="button" tabindex="0"
           onkeydown="if(event.key==='Enter')openModelModal(${m.id})">
        <div class="model-avail${m.available ? '' : ' unavailable'}"></div>
        <div class="model-card-img">
          ${m.photo_main
            ? `<img src="${m.photo_main}" alt="${m.name}" loading="lazy" />`
            : `<div class="model-card-placeholder">${m.name[0]}</div>`}
          <div class="model-card-overlay">
            <div class="model-card-tag">${CAT_LABELS[m.category] || m.category}</div>
            <div style="font-size:0.8rem;color:#ccc;margin-top:4px">
              ${m.height}см · ${[m.bust,m.waist,m.hips].filter(Boolean).join('/')}
            </div>
          </div>
        </div>
        <div class="model-card-info">
          <div class="model-card-name">${m.name}</div>
          <div class="model-card-meta">${m.height} см · ${m.hair_color || ''} · ${m.available ? '<span style="color:#4caf50">Свободна</span>' : '<span style="color:#f44336">Занята</span>'}</div>
        </div>
      </div>`).join('');
  }

  function plural(n) {
    const m10 = n%10, m100 = n%100;
    if(m100>=11&&m100<=19) return 'моделей';
    if(m10===1) return 'модель';
    if(m10>=2&&m10<=4) return 'модели';
    return 'моделей';
  }

  render();
})();
