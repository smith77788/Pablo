/* Catalog page — filters: name, category, city, age range, hair, height, availability, sort */
(async function () {
  const grid          = document.getElementById('catalogGrid');
  const skeleton      = document.getElementById('catalogSkeleton');
  const countEl       = document.getElementById('catalogCount');
  const searchInput   = document.getElementById('searchInput');
  const resetBtn      = document.getElementById('resetFilters');
  const minHeightEl   = document.getElementById('minHeight');
  const maxHeightEl   = document.getElementById('maxHeight');
  const ageMinEl      = document.getElementById('age-min');
  const ageMaxEl      = document.getElementById('age-max');
  const citySelect    = document.getElementById('cityFilter');
  const availCheckbox = document.getElementById('availableOnly');
  const sortEl        = document.getElementById('sortSelect');
  const activeFilterBadge = document.getElementById('activeFilterBadge');

  let allModels = [];

  // ── Read URL params ──────────────────────────────────────────────────────────
  function getUrlParams() {
    const p = new URLSearchParams(window.location.search);

    // Support combined format: height=160-165, age=18-22 (for shareable links)
    let minHeight = '', maxHeight = '';
    const heightVal = p.get('height');
    if (heightVal) {
      if (heightVal.endsWith('+')) {
        minHeight = heightVal.slice(0, -1);
      } else {
        const parts = heightVal.split('-');
        minHeight = parts[0] || '';
        maxHeight = parts[1] || '';
      }
    } else {
      // Legacy separate params
      minHeight = p.get('minH') || '';
      maxHeight = p.get('maxH') || '';
    }

    let ageMin = '', ageMax = '';
    const ageVal = p.get('age');
    if (ageVal) {
      if (ageVal.endsWith('+')) {
        ageMin = ageVal.slice(0, -1);
      } else {
        const parts = ageVal.split('-');
        ageMin = parts[0] || '';
        ageMax = parts[1] || '';
      }
    } else {
      // Legacy separate params
      ageMin = p.get('ageMin') || '';
      ageMax = p.get('ageMax') || '';
    }

    return {
      category:      p.get('category') || '',
      hair:          p.get('hair') || '',
      city:          p.get('city') || '',
      ageMin,
      ageMax,
      availableOnly: p.get('available') === '1',
      sort:          p.get('sort') || 'default',
      search:        p.get('q') || '',
      minHeight,
      maxHeight,
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

      // Use human-readable combined format for height and age
      if (filters.minHeight || filters.maxHeight) {
        if (filters.minHeight && !filters.maxHeight) {
          p.set('height', filters.minHeight + '+');
        } else if (filters.minHeight && filters.maxHeight) {
          p.set('height', `${filters.minHeight}-${filters.maxHeight}`);
        } else {
          p.set('height', filters.maxHeight);
        }
      }
      if (filters.ageMin || filters.ageMax) {
        if (filters.ageMin && !filters.ageMax) {
          p.set('age', filters.ageMin + '+');
        } else if (filters.ageMin && filters.ageMax) {
          p.set('age', `${filters.ageMin}-${filters.ageMax}`);
        } else {
          p.set('age', filters.ageMax);
        }
      }

      if (filters.availableOnly) p.set('available', '1');
      if (filters.sort && filters.sort !== 'default') p.set('sort', filters.sort);
      if (filters.search)      p.set('q', filters.search);
      const qs = p.toString();
      const newUrl = qs ? `${window.location.pathname}?${qs}` : window.location.pathname;
      window.history.replaceState({}, '', newUrl);
    }, 300);
  }

  const urlParams = getUrlParams();
  let filters = { ...urlParams };

  // ── Active filter count badge ─────────────────────────────────────────────────
  function updateActiveFilterBadge() {
    if (!activeFilterBadge) return;
    let count = 0;
    if (filters.category) count++;
    if (filters.hair) count++;
    if (filters.city) count++;
    if (filters.ageMin || filters.ageMax) count++;
    if (filters.minHeight || filters.maxHeight) count++;
    if (filters.availableOnly) count++;
    if (filters.search) count++;
    activeFilterBadge.textContent = count > 0 ? count : '';
    activeFilterBadge.style.display = count > 0 ? 'inline-block' : 'none';
    activeFilterBadge.setAttribute('aria-label', count > 0 ? `Активных фильтров: ${count}` : '');
  }

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
    // Height tags
    document.querySelectorAll('#heightFilters .filter-tag').forEach(t => {
      const matches = (t.dataset.hmin || '') === (filters.minHeight || '') &&
                      (t.dataset.hmax || '') === (filters.maxHeight || '');
      t.classList.toggle('active', matches);
    });
  }
  applyUrlParamsToUI();

  // ── Filter-tag helpers ───────────────────────────────────────────────────────
  function bindTagGroup(containerId, onSelect) {
    document.querySelectorAll(`#${containerId} .filter-tag`).forEach(el => {
      el.addEventListener('click', () => {
        document.querySelectorAll(`#${containerId} .filter-tag`).forEach(t => {
          t.classList.remove('active');
          t.setAttribute('aria-pressed', 'false');
        });
        el.classList.add('active');
        el.setAttribute('aria-pressed', 'true');
        onSelect(el);
        render();
      });
    });
  }

  bindTagGroup('categoryFilters', el => {
    filters.category = el.dataset.value;
    if (window.NM?.analytics) NM.analytics.filterCatalog('category', el.dataset.value);
  });
  bindTagGroup('hairFilters', el => {
    filters.hair = el.dataset.value;
    if (window.NM?.analytics) NM.analytics.filterCatalog('hair', el.dataset.value);
  });
  bindTagGroup('ageFilters', el => {
    filters.ageMin = el.dataset.min || '';
    filters.ageMax = el.dataset.max || '';
    // Clear numeric age inputs when a tag is selected
    if (ageMinEl) ageMinEl.value = '';
    if (ageMaxEl) ageMaxEl.value = '';
    if (window.NM?.analytics) NM.analytics.filterCatalog('age', `${el.dataset.min || ''}–${el.dataset.max || ''}`);
  });
  bindTagGroup('heightFilters', el => {
    filters.minHeight = el.dataset.hmin || '';
    filters.maxHeight = el.dataset.hmax || '';
    // Sync numeric height inputs
    if (minHeightEl) minHeightEl.value = filters.minHeight;
    if (maxHeightEl) maxHeightEl.value = filters.maxHeight;
    if (window.NM?.analytics) NM.analytics.filterCatalog('height', `${el.dataset.hmin || ''}–${el.dataset.hmax || ''}`);
  });

  // ── City, availability, sort ─────────────────────────────────────────────────
  citySelect?.addEventListener('change', () => {
    filters.city = citySelect.value;
    if (window.NM?.analytics) NM.analytics.filterCatalog('city', citySelect.value);
    render();
  });
  availCheckbox?.addEventListener('change', () => {
    filters.availableOnly = availCheckbox.checked;
    if (window.NM?.analytics) NM.analytics.filterCatalog('available', String(availCheckbox.checked));
    render();
  });
  sortEl?.addEventListener('change', () => {
    filters.sort = sortEl.value;
    if (window.NM?.analytics) NM.analytics.filterCatalog('sort', sortEl.value);
    render();
  });

  // ── Debounced inputs ─────────────────────────────────────────────────────────
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }
  searchInput?.addEventListener('input', debounce(() => {
    filters.search = searchInput.value.trim();
    if (filters.search && window.NM?.analytics) NM.analytics.filterCatalog('search', filters.search);
    render();
  }, 250));
  minHeightEl?.addEventListener('input', debounce(() => {
    filters.minHeight = minHeightEl.value;
    // Deselect height tags when typing
    document.querySelectorAll('#heightFilters .filter-tag').forEach(t => {
      t.classList.remove('active');
      t.setAttribute('aria-pressed', 'false');
    });
    const anyTag = document.querySelector('#heightFilters .filter-tag[data-hmin=""]');
    if (anyTag && !filters.minHeight && !filters.maxHeight) { anyTag.classList.add('active'); anyTag.setAttribute('aria-pressed', 'true'); }
    render();
  }, 400));
  maxHeightEl?.addEventListener('input', debounce(() => {
    filters.maxHeight = maxHeightEl.value;
    // Deselect height tags when typing
    document.querySelectorAll('#heightFilters .filter-tag').forEach(t => {
      t.classList.remove('active');
      t.setAttribute('aria-pressed', 'false');
    });
    const anyTag = document.querySelector('#heightFilters .filter-tag[data-hmin=""]');
    if (anyTag && !filters.minHeight && !filters.maxHeight) { anyTag.classList.add('active'); anyTag.setAttribute('aria-pressed', 'true'); }
    render();
  }, 400));
  ageMinEl?.addEventListener('input', debounce(() => {
    if (ageMinEl.value) {
      // Numeric range inputs override tag-based age selection
      filters.ageMin = ageMinEl.value;
      document.querySelectorAll('#ageFilters .filter-tag').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-pressed', 'false'); });
    } else {
      filters.ageMin = '';
    }
    render();
  }, 400));
  ageMaxEl?.addEventListener('input', debounce(() => {
    if (ageMaxEl.value) {
      filters.ageMax = ageMaxEl.value;
      document.querySelectorAll('#ageFilters .filter-tag').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-pressed', 'false'); });
    } else {
      filters.ageMax = '';
    }
    render();
  }, 400));

  // ── Reset ────────────────────────────────────────────────────────────────────
  resetBtn?.addEventListener('click', () => {
    filters = { category: '', hair: '', city: '', ageMin: '', ageMax: '', availableOnly: false, sort: 'default', search: '', minHeight: '', maxHeight: '' };
    if (ageMinEl) ageMinEl.value = '';
    if (ageMaxEl) ageMaxEl.value = '';
    if (minHeightEl) minHeightEl.value = '';
    if (maxHeightEl) maxHeightEl.value = '';
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

  // ── Favorites helpers ─────────────────────────────────────────────────────────
  const FAV_KEY = 'nm_favorites';
  function getFavs() {
    try { return JSON.parse(localStorage.getItem(FAV_KEY) || '[]'); } catch { return []; }
  }
  function saveFavs(arr) {
    try { localStorage.setItem(FAV_KEY, JSON.stringify(arr)); } catch {}
  }
  function isFav(id) {
    const favs = getFavs();
    return favs.some(f => (typeof f === 'object' ? f.id === id : f === id));
  }
  function _updateFavBadge() {
    const count = getFavs().length;
    const badge = document.querySelector('#fav-nav-badge');
    if (badge) badge.textContent = count > 0 ? count : '';
    updateFavCounter();
  }
  window._updateFavBadge = _updateFavBadge;

  function updateFavCounter() {
    const count = getFavs().length;
    const el = document.getElementById('fav-counter');
    if (el) {
      el.textContent = count > 0 ? count : '';
      el.style.display = count > 0 ? 'inline' : 'none';
    }
  }

  function showFavorites() {
    const favs = getFavs();
    if (favs.length === 0) {
      alert('Ваш список избранных пуст. Нажмите ❤️ на карточке модели, чтобы добавить.');
      return;
    }
    const favIds = new Set(favs.map(f => (typeof f === 'object' ? f.id : f)));
    const favModels = allModels.filter(m => favIds.has(m.id));
    if (favModels.length === 0) {
      alert(`У вас ${favs.length} избранных моделей. Дождитесь загрузки каталога и попробуйте снова.`);
      return;
    }
    // Store original rendered list
    const originalModels = allModels.filter(() => true);
    // Remove any existing back button
    const existingBack = document.getElementById('fav-back-btn');
    if (existingBack) existingBack.remove();
    // Temporarily replace allModels and render
    const savedAllModels = allModels;
    allModels = favModels;
    render();
    allModels = savedAllModels;
    // Add "back" button above the grid
    const backBtn = document.createElement('button');
    backBtn.id = 'fav-back-btn';
    backBtn.className = 'btn-filter-reset';
    backBtn.textContent = '← Показать всех';
    backBtn.style.cssText = 'margin: 0 auto 16px auto; display: block;';
    backBtn.onclick = () => {
      backBtn.remove();
      render();
    };
    grid.parentNode.insertBefore(backBtn, grid);
  }
  window.showFavorites = showFavorites;

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

    // Update active filter badge
    updateActiveFilterBadge();

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

    grid.innerHTML = '';
    list.forEach(m => {
      const statusClass = m.available ? 'status-free' : 'status-busy';
      const statusText  = m.available ? 'Свободна' : 'Занята';
      const catLabel    = CAT_LABELS[m.category] || m.category || '';
      const measures    = [m.bust, m.waist, m.hips].filter(Boolean).join('/');
      const favd        = isFav(m.id);

      const article = document.createElement('article');
      article.className = 'model-card';
      article.tabIndex = 0;
      article.setAttribute('role', 'button');
      article.setAttribute('aria-label', `Подробнее о модели ${m.name}`);
      article.addEventListener('click', () => openModelModal(m.id));
      article.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModelModal(m.id); }
      });

      article.innerHTML = `
          <div class="model-avail${m.available ? '' : ' unavailable'}" aria-hidden="true"></div>

          <div class="model-card-img" style="position:relative">
            ${m.photo_main
              ? (() => {
                  // Use thumbnail for list view if available (smaller, faster), fall back to full
                  const thumb = m.photo_thumb || m.photo_main;
                  const full = m.photo_main;
                  // thumb is always WebP (generated by sharp); full may be WebP or original
                  const fullWebp = full.replace(/\.(jpg|jpeg|png|gif)$/i, '.webp');
                  return `<picture><source srcset="${escHtml(thumb)}" type="image/webp" /><img src="${escHtml(fullWebp)}" alt="${escHtml(m.name)} — фото модели" loading="lazy" /></picture>`;
                })()
              : `<div class="model-card-placeholder" aria-hidden="true">${escHtml(m.name[0])}</div>`}
            ${m.featured ? '<span class="badge-top">⭐ Топ</span>' : ''}
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
              ${m.order_count > 0 ? `<span class="mc-chip" style="font-size:0.7rem;opacity:0.7" title="Завершённых заказов">📋 ${m.order_count} заказов</span>` : ''}
            </div>
            <a href="/model.html?id=${m.id}"
               class="btn-book-model"
               onclick="event.stopPropagation()"
               aria-label="Открыть профиль модели ${escHtml(m.name)}">
              Подробнее
            </a>
          </div>`;

      // Fav button — positioned top-right on image
      const favBtn = document.createElement('button');
      favBtn.setAttribute('aria-label', favd ? 'Убрать из избранного' : 'В избранное');
      favBtn.title = favd ? 'Убрать из избранного' : 'В избранное';
      favBtn.innerHTML = favd ? '❤️' : '🤍';
      favBtn.style.cssText = `position:absolute;top:8px;right:8px;width:34px;height:34px;border:none;background:rgba(0,0,0,0.55);border-radius:50%;font-size:1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:5;transition:background 0.2s;`;
      favBtn.addEventListener('mouseenter', () => { favBtn.style.background = 'rgba(0,0,0,0.8)'; });
      favBtn.addEventListener('mouseleave', () => { favBtn.style.background = 'rgba(0,0,0,0.55)'; });
      favBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        const favs = getFavs();
        const idx = favs.findIndex(f => (typeof f === 'object' ? f.id === m.id : f === m.id));
        if (idx === -1) {
          if (favs.length >= 50) { alert('Максимум 50 в избранном'); return; }
          // Store full model object for offline display in favorites.html
          favs.push({ id: m.id, name: m.name, city: m.city || '', category: m.category || '', photo: m.photo_main || '', photo_main: m.photo_main || '', height: m.height || 0, age: m.age || 0, available: !!m.available });
          saveFavs(favs);
          favBtn.innerHTML = '❤️';
          favBtn.title = 'Убрать из избранного';
          favBtn.setAttribute('aria-label', 'Убрать из избранного');
        } else {
          favs.splice(idx, 1);
          saveFavs(favs);
          favBtn.innerHTML = '🤍';
          favBtn.title = 'В избранное';
          favBtn.setAttribute('aria-label', 'В избранное');
        }
        _updateFavBadge();
      });

      // Compare button — positioned top-left on image
      const cmpActive = window.isInCompare && window.isInCompare(m.id);
      const cmpBtn = document.createElement('button');
      cmpBtn.className = 'btn-compare-card';
      cmpBtn.setAttribute('data-active', cmpActive ? 'true' : 'false');
      cmpBtn.setAttribute('aria-label', cmpActive ? 'Убрать из сравнения' : 'Добавить к сравнению');
      cmpBtn.title = cmpActive ? 'Убрать из сравнения' : 'Добавить к сравнению';
      cmpBtn.textContent = '⚖️';
      cmpBtn.style.cssText = `position:absolute;top:8px;left:8px;width:34px;height:34px;border:none;background:${cmpActive ? 'rgba(201,169,110,0.85)' : 'rgba(0,0,0,0.55)'};color:${cmpActive ? 'var(--bg)' : 'var(--text-muted)'};border-radius:50%;font-size:0.9rem;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:5;transition:background 0.2s,color 0.2s;`;
      cmpBtn.addEventListener('mouseenter', () => {
        if (cmpBtn.getAttribute('data-active') !== 'true') cmpBtn.style.background = 'rgba(201,169,110,0.4)';
      });
      cmpBtn.addEventListener('mouseleave', () => {
        if (cmpBtn.getAttribute('data-active') !== 'true') cmpBtn.style.background = 'rgba(0,0,0,0.55)';
      });
      cmpBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        if (window.toggleCompare) {
          const added = window.toggleCompare(m.id, m);
          cmpBtn.setAttribute('data-active', added ? 'true' : 'false');
          cmpBtn.title = added ? 'Убрать из сравнения' : 'Добавить к сравнению';
          cmpBtn.setAttribute('aria-label', added ? 'Убрать из сравнения' : 'Добавить к сравнению');
          cmpBtn.style.background = added ? 'rgba(201,169,110,0.85)' : 'rgba(0,0,0,0.55)';
          cmpBtn.style.color = added ? 'var(--bg)' : 'var(--text-muted)';
        }
      });

      // Insert fav and compare buttons into the image container
      const imgContainer = article.querySelector('.model-card-img');
      if (imgContainer) {
        imgContainer.style.position = 'relative';
        imgContainer.appendChild(favBtn);
        imgContainer.appendChild(cmpBtn);
      }

      grid.appendChild(article);
    });

    // Inject dynamic ItemList Schema.org for current filtered list
    injectCatalogSchema(list);
  }

  // ── Schema.org: dynamic ItemList for visible models ──────────────────────────
  function injectCatalogSchema(models) {
    const existing = document.getElementById('schema-catalog-dynamic');
    if (existing) existing.remove();
    if (!models.length) return;

    const baseUrl = window.location.origin;
    const schema = {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "name": "Каталог моделей",
      "url": baseUrl + "/catalog.html",
      "numberOfItems": models.length,
      "itemListElement": models.slice(0, 20).map((m, i) => ({
        "@type": "ListItem",
        "position": i + 1,
        "url": `${baseUrl}/model.html?id=${m.id}`,
        "name": m.name
      }))
    };

    const script = document.createElement('script');
    script.id = 'schema-catalog-dynamic';
    script.type = 'application/ld+json';
    script.textContent = JSON.stringify(schema);
    document.head.appendChild(script);
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

  // Analytics: model_view on card click
  grid.addEventListener('click', function(e) {
    const card = e.target.closest('.model-card[onclick]');
    if (!card) return;
    const match = card.getAttribute('onclick').match(/openModelModal\((\d+)\)/);
    if (!match) return;
    const modelId = match[1];
    const nameEl = card.querySelector('.model-card-name');
    const modelName = nameEl ? nameEl.textContent : '';
    if (window.NM && NM.analytics) {
      NM.analytics.event('model_view', { model_id: modelId, model_name: modelName });
    }
  });

  render();
  _updateFavBadge();
  updateFavCounter();

  // Auto-open model when page loaded via /model/:id SSR route
  const autoOpenId = document.body.getAttribute('data-open-model');
  if (autoOpenId) {
    // Wait for models to load then open the modal
    const waitAndOpen = () => {
      if (typeof openModelModal === 'function') {
        openModelModal(parseInt(autoOpenId));
      } else {
        setTimeout(waitAndOpen, 200);
      }
    };
    setTimeout(waitAndOpen, 300);
  }
})();
