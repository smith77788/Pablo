/* ─── Catalog page logic ────────────────────────────── */
(function () {
  let allModels = [];
  let filters = { category: '', hair_color: '', min_height: '', max_height: '', available: '', search: '' };

  async function loadModels() {
    try {
      allModels = await apiFetch('/models');
      renderCatalog();
    } catch {
      document.getElementById('catalogGrid').innerHTML = '<div class="no-results">Не удалось загрузить каталог</div>';
    }
  }

  function renderCatalog() {
    const grid = document.getElementById('catalogGrid');
    const countEl = document.getElementById('catalogCount');
    let items = allModels;

    if (filters.category) items = items.filter(m => m.category === filters.category);
    if (filters.hair_color) items = items.filter(m => m.hair_color === filters.hair_color);
    if (filters.min_height) items = items.filter(m => m.height >= +filters.min_height);
    if (filters.max_height) items = items.filter(m => m.height <= +filters.max_height);
    if (filters.available === '1') items = items.filter(m => m.available === 1);
    if (filters.search) {
      const q = filters.search.toLowerCase();
      items = items.filter(m => m.name.toLowerCase().includes(q) || (m.bio || '').toLowerCase().includes(q));
    }

    countEl.textContent = `Найдено: ${items.length} ${plural(items.length, 'модель', 'модели', 'моделей')}`;

    if (!items.length) {
      grid.innerHTML = '<div class="no-results"><div style="font-size:3rem;margin-bottom:16px;opacity:0.3">💃</div><p>По вашему запросу ничего не найдено</p><p style="margin-top:8px;font-size:0.8rem">Попробуйте изменить параметры фильтрации</p></div>';
      return;
    }

    grid.innerHTML = items.map(m => modelCard(m, 'openModelModal')).join('');
  }

  function plural(n, one, few, many) {
    const mod10 = n % 10, mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 19) return many;
    if (mod10 === 1) return one;
    if (mod10 >= 2 && mod10 <= 4) return few;
    return many;
  }

  // Search
  let searchTimer;
  document.getElementById('searchInput')?.addEventListener('input', e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { filters.search = e.target.value.trim(); renderCatalog(); }, 300);
  });

  // Category filters
  document.getElementById('categoryFilters')?.querySelectorAll('.filter-tag').forEach(tag => {
    tag.addEventListener('click', () => {
      document.getElementById('categoryFilters').querySelectorAll('.filter-tag').forEach(t => t.classList.remove('active'));
      tag.classList.add('active');
      filters.category = tag.dataset.value;
      renderCatalog();
    });
  });

  // Hair filters
  document.getElementById('hairFilters')?.querySelectorAll('.filter-tag').forEach(tag => {
    tag.addEventListener('click', () => {
      document.getElementById('hairFilters').querySelectorAll('.filter-tag').forEach(t => t.classList.remove('active'));
      tag.classList.add('active');
      filters.hair_color = tag.dataset.value;
      renderCatalog();
    });
  });

  // Avail filters
  document.getElementById('availFilters')?.querySelectorAll('.filter-tag').forEach(tag => {
    tag.addEventListener('click', () => {
      document.getElementById('availFilters').querySelectorAll('.filter-tag').forEach(t => t.classList.remove('active'));
      tag.classList.add('active');
      filters.available = tag.dataset.value;
      renderCatalog();
    });
  });

  // Height range
  let heightTimer;
  ['minHeight', 'maxHeight'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => {
      clearTimeout(heightTimer);
      heightTimer = setTimeout(() => {
        filters.min_height = document.getElementById('minHeight').value;
        filters.max_height = document.getElementById('maxHeight').value;
        renderCatalog();
      }, 400);
    });
  });

  // Reset
  document.getElementById('resetFilters')?.addEventListener('click', () => {
    filters = { category: '', hair_color: '', min_height: '', max_height: '', available: '', search: '' };
    document.getElementById('searchInput').value = '';
    document.getElementById('minHeight').value = '';
    document.getElementById('maxHeight').value = '';
    document.querySelectorAll('.filter-tag').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.filter-tag[data-value=""]').forEach(t => t.classList.add('active'));
    renderCatalog();
  });

  loadModels();
})();
