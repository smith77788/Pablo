/* ─── Admin panel shared utilities ─────────────────── */
const API = '/api';

let _adminToken = localStorage.getItem('admin_token');
if (!_adminToken && !window.location.pathname.includes('login')) {
  window.location.href = '/admin/login.html';
}

const adminUser = JSON.parse(localStorage.getItem('admin_user') || '{}');

// ─── Token refresh ────────────────────────────────────
let _refreshing = null;
async function _tryRefresh() {
  if (_refreshing) return _refreshing;
  const rt = localStorage.getItem('admin_refresh_token');
  if (!rt) {
    localStorage.clear();
    window.location.href = '/admin/login.html';
    return null;
  }
  _refreshing = fetch(API + '/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: rt }),
  })
    .then(async r => {
      if (!r.ok) {
        localStorage.clear();
        window.location.href = '/admin/login.html';
        return null;
      }
      const d = await r.json();
      _adminToken = d.token;
      localStorage.setItem('admin_token', d.token);
      if (d.refresh_token) localStorage.setItem('admin_refresh_token', d.refresh_token);
      return d.token;
    })
    .catch(() => {
      localStorage.clear();
      window.location.href = '/admin/login.html';
      return null;
    })
    .finally(() => {
      _refreshing = null;
    });
  return _refreshing;
}

async function apiFetch(path, opts = {}, _retry = true) {
  const res = await fetch(API + path, {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${_adminToken}`,
      ...opts.headers,
    },
    ...opts,
  });
  if (res.status === 401 && _retry) {
    const newToken = await _tryRefresh();
    if (newToken) return apiFetch(path, opts, false);
    return;
  }
  if (res.status === 401) {
    localStorage.clear();
    window.location.href = '/admin/login.html';
    return;
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function apiFetchForm(path, formData, method = 'POST', _retry = true) {
  const res = await fetch(API + path, {
    method,
    headers: { Authorization: `Bearer ${_adminToken}` },
    body: formData,
  });
  if (res.status === 401 && _retry) {
    const newToken = await _tryRefresh();
    if (newToken) return apiFetchForm(path, formData, method, false);
    return;
  }
  if (res.status === 401) {
    localStorage.clear();
    window.location.href = '/admin/login.html';
    return;
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ─── Toast notifications ──────────────────────────────
function toast(msg, type = 'info') {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  container.setAttribute('aria-live', 'polite');
  container.setAttribute('aria-atomic', 'true');
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  const icon = type === 'success' ? '✓' : type === 'error' ? '✕' : type === 'warning' ? '⚠' : 'ℹ';
  t.innerHTML = `<span style="font-weight:700">${icon}</span> <span>${msg}</span>`;
  container.appendChild(t);
  setTimeout(() => {
    t.style.transition = 'opacity 0.4s';
    t.style.opacity = '0';
  }, 3500);
  setTimeout(() => t.remove(), 4000);
}

// ─── Confirm dialog ───────────────────────────────────
function adminConfirm(title, msg, onOk) {
  let overlay = document.getElementById('confirmOverlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'confirmOverlay';
    overlay.className = 'confirm-overlay';
    overlay.innerHTML = `
      <div class="confirm-box">
        <h4 id="confirmTitle"></h4>
        <p id="confirmMsg"></p>
        <div class="confirm-btns">
          <button class="btn btn-md btn-ghost" id="confirmCancel">Отмена</button>
          <button class="btn btn-md btn-danger" id="confirmOk">Подтвердить</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    document.getElementById('confirmCancel').addEventListener('click', () => overlay.classList.remove('open'));
    overlay.addEventListener('click', e => {
      if (e.target === overlay) overlay.classList.remove('open');
    });
  }
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmMsg').textContent = msg;
  overlay.classList.add('open');
  const okBtn = document.getElementById('confirmOk');
  const newBtn = okBtn.cloneNode(true);
  okBtn.parentNode.replaceChild(newBtn, okBtn);
  newBtn.addEventListener('click', () => {
    overlay.classList.remove('open');
    onOk();
  });
}

function logout() {
  const rt = localStorage.getItem('admin_refresh_token');
  if (rt) {
    fetch(API + '/auth/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    }).catch(() => {});
  }
  localStorage.clear();
  window.location.href = '/admin/login.html';
}

// ─── HTML escape helper (XSS protection) ──────────────
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(
    /[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  );
}

// ─── Formatters ───────────────────────────────────────
const STATUS_LABELS = {
  new: 'Новая',
  reviewing: 'На рассмотрении',
  confirmed: 'Подтверждена',
  in_progress: 'В процессе',
  completed: 'Завершена',
  cancelled: 'Отменена',
};
const STATUS_OPTIONS = Object.entries(STATUS_LABELS)
  .map(([v, l]) => `<option value="${v}">${l}</option>`)
  .join('');
const CATEGORIES = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };
const EVENT_LABELS = {
  fashion_show: 'Показ мод',
  photo_shoot: 'Фотосессия',
  event: 'Мероприятие',
  commercial: 'Коммерческая',
  runway: 'Подиум',
  other: 'Другое',
};

function statusBadge(status) {
  return `<span class="badge badge-${status}">${STATUS_LABELS[status] || status}</span>`;
}
function formatDate(d) {
  if (!d) return '—';
  try {
    return new Date(d).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
  } catch {
    return d;
  }
}
function formatDateTime(d) {
  if (!d) return '—';
  try {
    return new Date(d).toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return d;
  }
}

// ─── Sidebar user info ────────────────────────────────
document.querySelectorAll('.sidebar-user-name').forEach(el => (el.textContent = adminUser.username || '—'));
document
  .querySelectorAll('.sidebar-user-role')
  .forEach(el => (el.textContent = adminUser.role === 'superadmin' ? 'Суперадмин' : 'Менеджер'));
document
  .querySelectorAll('.sidebar-user-letter')
  .forEach(el => (el.textContent = (adminUser.username || 'A')[0].toUpperCase()));

// ─── Mobile sidebar toggle ────────────────────────────
(function () {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;

  // Create hamburger button and overlay dynamically
  const hamburger = document.createElement('button');
  hamburger.id = 'sidebarToggle';
  hamburger.innerHTML = '☰';
  hamburger.style.cssText =
    'display:none;background:none;border:none;color:var(--text);font-size:1.4rem;cursor:pointer;padding:4px 8px;';

  const overlay = document.createElement('div');
  overlay.id = 'sidebarOverlay';
  overlay.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:99;';
  document.body.appendChild(overlay);

  const topbarLeft = document.querySelector('.topbar');
  if (topbarLeft) topbarLeft.insertBefore(hamburger, topbarLeft.firstChild);

  function showOnMobile() {
    if (window.innerWidth <= 900) {
      hamburger.style.display = 'inline-flex';
    } else {
      hamburger.style.display = 'none';
      sidebar.classList.remove('open');
      overlay.style.display = 'none';
    }
  }

  hamburger.addEventListener('click', () => {
    const isOpen = sidebar.classList.toggle('open');
    overlay.style.display = isOpen ? 'block' : 'none';
  });
  overlay.addEventListener('click', () => {
    sidebar.classList.remove('open');
    overlay.style.display = 'none';
  });

  window.addEventListener('resize', showOnMobile);
  showOnMobile();
})();

// ─── Active nav link ──────────────────────────────────
const currentPath = window.location.pathname.replace(/\/$/, '') || '/';
document.querySelectorAll('.sidebar-nav a').forEach(a => {
  const href = (a.getAttribute('href') || '').replace(/\/$/, '');
  if (href && currentPath.endsWith(href)) a.classList.add('active');
});

// ─── Logout buttons ───────────────────────────────────
document.querySelectorAll('[data-action="logout"]').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    logout();
  });
});

// ─── New orders badge polling ─────────────────────────
async function pollNewOrders() {
  try {
    const stats = await apiFetch('/admin/stats');
    document.querySelectorAll('#newOrdersBadge').forEach(el => {
      el.textContent = stats.new_orders;
      el.style.display = stats.new_orders > 0 ? '' : 'none';
    });
    // Unread messages badge
    document.querySelectorAll('#unreadBadge').forEach(el => {
      el.textContent = stats.unread_messages;
      el.style.display = stats.unread_messages > 0 ? '' : 'none';
    });
  } catch {}
}
pollNewOrders();
setInterval(pollNewOrders, 20000);

// ─── Export helper ────────────────────────────────────
function downloadCSV(url) {
  const a = document.createElement('a');
  a.href = API + url + `&_auth=${encodeURIComponent(_adminToken)}`;
  // Use fetch with auth header and create blob URL
  fetch(API + url, { headers: { Authorization: `Bearer ${_adminToken}` } })
    .then(r => r.blob())
    .then(blob => {
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `orders_${Date.now()}.csv`;
      link.click();
      URL.revokeObjectURL(link.href);
    })
    .catch(() => toast('Ошибка экспорта', 'error'));
}

// ─── Currency formatter ───────────────────────────────
function formatCurrency(n) {
  if (n == null || n === '') return '—';
  const num = parseInt(n) || 0;
  return num.toLocaleString('ru-RU') + ' ₽';
}

// ─── Copy to clipboard ────────────────────────────────
function copyToClipboard(text) {
  navigator.clipboard
    ?.writeText(text)
    .then(() => {
      toast('Скопировано: ' + text, 'success');
    })
    .catch(() => {
      const el = document.createElement('textarea');
      el.value = text;
      el.style.position = 'fixed';
      el.style.opacity = '0';
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      document.body.removeChild(el);
      toast('Скопировано', 'success');
    });
}

// ─── Debounce utility ─────────────────────────────────
function debounce(fn, delay = 400) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

const api = apiFetch;

// ─── Ripple effect on buttons ─────────────────────────
(function initRipple() {
  function addRipple(e) {
    const btn = e.currentTarget;
    const r = document.createElement('span');
    r.className = 'ripple-effect';
    const rect = btn.getBoundingClientRect();
    r.style.left = e.clientX - rect.left + 'px';
    r.style.top = e.clientY - rect.top + 'px';
    btn.appendChild(r);
    r.addEventListener('animationend', () => r.remove());
  }
  function attachRipple() {
    document.querySelectorAll('.btn, .btn-action, .topbar-btn').forEach(btn => {
      if (!btn.dataset.ripple) {
        btn.dataset.ripple = '1';
        btn.addEventListener('click', addRipple);
      }
    });
  }
  // Initial attach + re-attach after dynamic content
  attachRipple();
  const observer = new MutationObserver(attachRipple);
  observer.observe(document.body, { childList: true, subtree: true });
})();

// ─── Enhanced toast with progress bar + proper hide ───
// Override the simple toast() above with a richer version
(function upgradeToast() {
  const _originalToast = window.toast || function () {};
  window.toast = function toastV2(msg, type = 'info', duration = 3500) {
    let container = document.getElementById('toastContainer');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toastContainer';
      container.className = 'toast-container';
      container.setAttribute('aria-live', 'polite');
      document.body.appendChild(container);
    }
    const iconMap = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    t.style.setProperty('--toast-duration', duration + 'ms');
    t.innerHTML = `<span class="toast-icon">${iconMap[type] || 'ℹ'}</span><span>${msg}</span>`;
    container.appendChild(t);

    function hide() {
      t.classList.add('hiding');
      t.addEventListener('animationend', () => t.remove(), { once: true });
    }
    t.addEventListener('click', hide);
    setTimeout(hide, duration);
  };
})();

// ─── Form gold focus label ────────────────────────────
(function initFormFocus() {
  document.querySelectorAll('.admin-input, .login-input, .filter-input').forEach(input => {
    const group = input.closest('.admin-form-group');
    if (!group) return;
    input.addEventListener('focus', () => group.classList.add('focused'));
    input.addEventListener('blur', () => group.classList.remove('focused'));
  });
  // Re-run after DOM mutations
  const obs = new MutationObserver(() => {
    document.querySelectorAll('.admin-input:not([data-ff])').forEach(input => {
      input.dataset.ff = '1';
      const group = input.closest('.admin-form-group');
      if (!group) return;
      input.addEventListener('focus', () => group.classList.add('focused'));
      input.addEventListener('blur', () => group.classList.remove('focused'));
    });
  });
  obs.observe(document.body, { childList: true, subtree: true });
})();

window._admin = {
  apiFetch,
  apiFetchForm,
  api,
  toast,
  adminConfirm,
  logout,
  formatDate,
  formatDateTime,
  statusBadge,
  escapeHtml,
  STATUS_OPTIONS,
  STATUS_LABELS,
  CATEGORIES,
  EVENT_LABELS,
  downloadCSV,
  formatCurrency,
  copyToClipboard,
  debounce,
};
