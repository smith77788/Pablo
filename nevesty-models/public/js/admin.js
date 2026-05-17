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

// ─── Notifications: inject bell into all topbars ──────
(function injectNotifBell() {
  // Don't inject on login page, or if bell already present (native or injected)
  if (window.location.pathname.includes('login')) return;
  if (document.getElementById('_adminNotifBellWrap')) return;
  if (document.getElementById('notifBellWrap')) return; // index.html has native bell

  // Inject CSS for notification bell + dropdown
  const style = document.createElement('style');
  style.textContent = `
    ._notif-bell-wrap { position: relative; display: inline-flex; flex-shrink: 0; }
    ._notif-bell-btn {
      background: none; border: 1px solid var(--border); color: var(--text-muted);
      width: 34px; height: 34px; border-radius: 3px; font-size: 0.95rem;
      display: flex; align-items: center; justify-content: center;
      cursor: pointer; transition: all 0.2s; position: relative;
    }
    ._notif-bell-btn:hover { border-color: var(--gold); color: var(--gold); }
    ._notif-bell-badge {
      position: absolute; top: -6px; right: -6px;
      background: var(--error, #e53e3e); color: #fff;
      font-size: 0.58rem; font-weight: 700;
      min-width: 18px; height: 18px; border-radius: 9px; padding: 0 3px;
      display: none; align-items: center; justify-content: center;
      border: 2px solid var(--bg, #111);
    }
    ._notif-dropdown {
      position: absolute; top: calc(100% + 8px); right: 0;
      width: 320px; background: var(--bg2, #1a1a1a);
      border: 1px solid var(--border2, #333); border-radius: 4px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.5); z-index: 500;
      display: none;
    }
    ._notif-dropdown.open { display: block; }
    ._notif-dd-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 14px; border-bottom: 1px solid var(--border, #2a2a2a);
      font-size: 0.78rem; font-weight: 600; color: var(--text, #eee);
    }
    ._notif-dd-mark-all {
      font-size: 0.68rem; color: var(--text-dim, #666); font-weight: 400;
      cursor: pointer; text-decoration: underline; background: none; border: none;
    }
    ._notif-dd-mark-all:hover { color: var(--gold, #c9a96e); }
    ._notif-dd-item {
      display: flex; align-items: flex-start; gap: 10px;
      padding: 9px 14px; border-bottom: 1px solid var(--border, #2a2a2a);
      text-decoration: none; color: inherit; cursor: pointer; transition: background 0.15s;
    }
    ._notif-dd-item:last-of-type { border-bottom: none; }
    ._notif-dd-item:hover { background: rgba(255,255,255,0.03); }
    ._notif-dd-item.unread { background: rgba(201,169,110,0.05); border-left: 2px solid var(--gold, #c9a96e); }
    ._notif-dd-icon { font-size: 1rem; flex-shrink: 0; margin-top: 1px; }
    ._notif-dd-body { flex: 1; min-width: 0; }
    ._notif-dd-title { font-size: 0.76rem; font-weight: 600; color: var(--text, #eee); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    ._notif-dd-text { font-size: 0.7rem; color: var(--text-muted, #888); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    ._notif-dd-time { font-size: 0.63rem; color: var(--text-dim, #555); white-space: nowrap; flex-shrink: 0; }
    ._notif-dd-footer {
      display: block; text-align: center; padding: 9px 14px;
      font-size: 0.72rem; color: var(--gold, #c9a96e); text-decoration: none;
      border-top: 1px solid var(--border, #2a2a2a); transition: background 0.15s;
    }
    ._notif-dd-footer:hover { background: rgba(201,169,110,0.08); }
    ._notif-dd-empty { padding: 20px 14px; text-align: center; font-size: 0.78rem; color: var(--text-dim, #555); }
    ._notif-dd-loading { padding: 16px 14px; text-align: center; font-size: 0.78rem; color: var(--text-dim, #555); }
  `;
  document.head.appendChild(style);

  // Build bell HTML
  const wrap = document.createElement('div');
  wrap.className = '_notif-bell-wrap';
  wrap.id = '_adminNotifBellWrap';
  wrap.innerHTML = `
    <button class="_notif-bell-btn" id="_adminNotifBellBtn" title="Уведомления" aria-label="Уведомления">
      🔔
      <span class="_notif-bell-badge" id="_adminNotifBadge"></span>
    </button>
    <div class="_notif-dropdown" id="_adminNotifDropdown">
      <div class="_notif-dd-header">
        <span>Уведомления</span>
        <button class="_notif-dd-mark-all" onclick="_notifMarkAll()">Отметить все прочитанными</button>
      </div>
      <div id="_adminNotifList"><div class="_notif-dd-loading">Загрузка...</div></div>
      <a href="/admin/notifications.html" class="_notif-dd-footer">Все уведомления →</a>
    </div>
  `;

  // Inject into topbar: try .topbar-actions, .topbar-right, or .topbar
  function tryInject() {
    const target =
      document.querySelector('.topbar-actions') ||
      document.querySelector('.topbar-right') ||
      document.querySelector('.topbar');
    if (!target) return false;
    // Prepend the bell as the first child of the topbar actions/right div,
    // or append before last child of topbar
    if (target.classList.contains('topbar')) {
      target.appendChild(wrap);
    } else {
      target.insertBefore(wrap, target.firstChild);
    }
    return true;
  }

  if (!tryInject()) {
    // Try after DOMContentLoaded
    document.addEventListener('DOMContentLoaded', tryInject);
  }

  // Toggle dropdown on bell click
  document.addEventListener('click', e => {
    const bell = document.getElementById('_adminNotifBellBtn');
    const dropdown = document.getElementById('_adminNotifDropdown');
    if (!bell || !dropdown) return;
    const bellWrap = document.getElementById('_adminNotifBellWrap');
    if (bell.contains(e.target)) {
      const isOpen = dropdown.classList.toggle('open');
      if (isOpen) _notifLoadDropdown();
    } else if (bellWrap && !bellWrap.contains(e.target)) {
      dropdown.classList.remove('open');
    }
  });
})();

// ─── Notification dropdown helpers ────────────────────
function _notifTimeAgo(dateStr) {
  if (!dateStr) return '—';
  try {
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);
    if (mins < 1) return 'только что';
    if (mins < 60) return `${mins} мин. назад`;
    if (hours < 24) return `${hours} ч. назад`;
    if (days === 1) return 'вчера';
    if (days < 7) return `${days} дн. назад`;
    return formatDate(dateStr);
  } catch {
    return '—';
  }
}

const _NOTIF_ICONS = { new_order: '📋', pending_review: '⭐', unread_message: '💬', system: '📢' };
const _NOTIF_LINKS = {
  new_order: '/admin/orders.html',
  pending_review: '/admin/reviews.html',
  unread_message: '/admin/messages.html',
};

async function _notifLoadDropdown() {
  const list = document.getElementById('_adminNotifList');
  if (!list) return;
  list.innerHTML = '<div class="_notif-dd-loading">Загрузка...</div>';
  try {
    const data = await apiFetch('/admin/notifications?limit=5');
    const items = data.notifications || [];
    if (!items.length) {
      list.innerHTML = '<div class="_notif-dd-empty">Уведомлений нет</div>';
      return;
    }
    list.innerHTML = items
      .map(n => {
        const icon = _NOTIF_ICONS[n.type] || '🔔';
        const link = n.link || _NOTIF_LINKS[n.type] || '/admin/notifications.html';
        return `<a class="_notif-dd-item ${n.read ? '' : 'unread'}"
          href="${escapeHtml(link)}"
          onclick="_notifMarkRead('${escapeHtml(String(n.id))}', this)">
        <span class="_notif-dd-icon">${icon}</span>
        <div class="_notif-dd-body">
          <div class="_notif-dd-title">${escapeHtml(n.title || '')}</div>
          <div class="_notif-dd-text">${escapeHtml(n.text || '')}</div>
        </div>
        <div class="_notif-dd-time">${_notifTimeAgo(n.created_at)}</div>
      </a>`;
      })
      .join('');
  } catch (e) {
    list.innerHTML = `<div class="_notif-dd-empty">Ошибка загрузки</div>`;
  }
}

async function _notifMarkRead(id, linkEl) {
  if (!id) return;
  try {
    await apiFetch(`/admin/notifications/${encodeURIComponent(id)}/read`, { method: 'PATCH' });
    if (linkEl) {
      linkEl.classList.remove('unread');
    }
    // Refresh badge count
    pollNotifications();
  } catch {
    /* ignore, navigation proceeds */
  }
}

async function _notifMarkAll() {
  try {
    await apiFetch('/admin/notifications/read-all', { method: 'PATCH' });
    // Re-render dropdown and update badge
    await _notifLoadDropdown();
    pollNotifications();
    toast('Все уведомления отмечены прочитанными', 'success');
  } catch (e) {
    toast('Ошибка: ' + e.message, 'error');
  }
}

// ─── Notifications badge polling ──────────────────────
async function pollNotifications() {
  try {
    const data = await apiFetch('/admin/notifications?limit=1&unread=1');
    const count = data.unread_count ?? (data.notifications || []).filter(n => !n.read).length;
    // Update sidebar nav badge
    document.querySelectorAll('#notifNavBadge').forEach(el => {
      el.textContent = count > 9 ? '9+' : count;
      el.style.display = count > 0 ? '' : 'none';
    });
    // Update injected bell badge
    const bellBadge = document.getElementById('_adminNotifBadge');
    if (bellBadge) {
      bellBadge.textContent = count > 9 ? '9+' : count;
      bellBadge.style.display = count > 0 ? 'flex' : 'none';
    }
    // Also update index.html native badge if present
    const nativeBadge = document.getElementById('notifBadge');
    if (nativeBadge) {
      nativeBadge.textContent = count > 9 ? '9+' : count;
      nativeBadge.style.display = count > 0 ? 'flex' : 'none';
    }
    // Update page title with unread badge (like Gmail)
    const totalBadge = (parseInt(document.getElementById('newOrdersBadge')?.textContent) || 0) + count;
    if (totalBadge > 0) {
      const base = document.title.replace(/^\(\d+\)\s*/, '');
      document.title = `(${totalBadge}) ${base}`;
    } else {
      document.title = document.title.replace(/^\(\d+\)\s*/, '');
    }
  } catch {}
}
pollNotifications();
setInterval(pollNotifications, 30000);

// ─── Keyboard shortcuts ───────────────────────────────
document.addEventListener('keydown', e => {
  // Escape: close any open modal
  if (e.key === 'Escape') {
    document
      .querySelectorAll('.modal.active, .modal[style*="display: flex"], .modal[style*="display:flex"]')
      .forEach(m => {
        const closeBtn = m.querySelector('[data-action="close-modal"], .modal-close, .btn-modal-close');
        if (closeBtn) closeBtn.click();
        else m.style.display = 'none';
      });
  }
  // Ctrl/Cmd + K: focus search input if present
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    const searchInput = document.querySelector(
      '.filter-input[type="search"], input[placeholder*="поиск"], input[placeholder*="Поиск"]'
    );
    if (searchInput) {
      e.preventDefault();
      searchInput.focus();
      searchInput.select();
    }
  }
});

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
