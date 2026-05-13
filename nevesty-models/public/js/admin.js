/* ─── Admin panel shared utilities ─────────────────── */
const API = '/api';

const token = localStorage.getItem('admin_token');
if (!token && !window.location.pathname.includes('login')) {
  window.location.href = '/admin/login.html';
}

const adminUser = JSON.parse(localStorage.getItem('admin_user') || '{}');

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
      ...opts.headers
    },
    ...opts
  });
  if (res.status === 401) { localStorage.clear(); window.location.href = '/admin/login.html'; }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Ошибка запроса');
  return data;
}

async function apiFetchForm(path, formData, method = 'POST') {
  const res = await fetch(API + path, {
    method,
    headers: { 'Authorization': `Bearer ${token}` },
    body: formData
  });
  if (res.status === 401) { localStorage.clear(); window.location.href = '/admin/login.html'; }
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Ошибка запроса');
  return data;
}

function toast(msg, type = 'info') {
  let container = document.getElementById('toastContainer');
  if (!container) { container = document.createElement('div'); container.id = 'toastContainer'; container.className = 'toast-container'; document.body.appendChild(container); }
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${type === 'success' ? '✓' : type === 'error' ? '✕' : 'ℹ'}</span> <span>${msg}</span>`;
  container.appendChild(t);
  setTimeout(() => t.style.opacity = '0', 3500);
  setTimeout(() => t.remove(), 4000);
}

function confirm(title, msg, onOk) {
  let overlay = document.getElementById('confirmOverlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'confirmOverlay';
    overlay.className = 'confirm-overlay';
    overlay.innerHTML = `<div class="confirm-box"><h4 id="confirmTitle"></h4><p id="confirmMsg"></p><div class="confirm-btns"><button class="btn btn-md btn-ghost" id="confirmCancel">Отмена</button><button class="btn btn-md btn-danger" id="confirmOk">Подтвердить</button></div></div>`;
    document.body.appendChild(overlay);
    document.getElementById('confirmCancel').addEventListener('click', () => overlay.classList.remove('open'));
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.classList.remove('open'); });
  }
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmMsg').textContent = msg;
  overlay.classList.add('open');
  const okBtn = document.getElementById('confirmOk');
  const newBtn = okBtn.cloneNode(true);
  okBtn.parentNode.replaceChild(newBtn, okBtn);
  newBtn.addEventListener('click', () => { overlay.classList.remove('open'); onOk(); });
}

function logout() {
  localStorage.clear();
  window.location.href = '/admin/login.html';
}

const STATUS_LABELS = {
  new: 'Новая', reviewing: 'На рассмотрении', confirmed: 'Подтверждена',
  in_progress: 'В процессе', completed: 'Завершена', cancelled: 'Отменена'
};
const STATUS_OPTIONS = Object.entries(STATUS_LABELS).map(([v, l]) => `<option value="${v}">${l}</option>`).join('');

function statusBadge(status) {
  return `<span class="badge badge-${status}">${STATUS_LABELS[status] || status}</span>`;
}

function formatDate(d) {
  if (!d) return '—';
  try { return new Date(d).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' }); } catch { return d; }
}
function formatDateTime(d) {
  if (!d) return '—';
  try { return new Date(d).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }); } catch { return d; }
}

const CATEGORIES = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };
const EVENT_LABELS = {
  fashion_show: 'Показ мод', photo_shoot: 'Фотосессия', event: 'Мероприятие',
  commercial: 'Коммерческая съёмка', runway: 'Подиум', other: 'Другое'
};

// ─── Populate user info ────────────────────────────────
document.querySelectorAll('.sidebar-user-name').forEach(el => el.textContent = adminUser.username || '—');
document.querySelectorAll('.sidebar-user-role').forEach(el => el.textContent = adminUser.role === 'superadmin' ? 'Суперадмин' : 'Менеджер');
document.querySelectorAll('.sidebar-user-letter').forEach(el => el.textContent = (adminUser.username || 'A')[0].toUpperCase());

// ─── Active nav link ───────────────────────────────────
const currentPath = window.location.pathname;
document.querySelectorAll('.sidebar-nav a').forEach(a => {
  if (a.getAttribute('href') === currentPath || a.getAttribute('href') === currentPath.replace('/admin/', '/admin/index.html')) {
    a.classList.add('active');
  }
});

// ─── Logout buttons ────────────────────────────────────
document.querySelectorAll('[data-action="logout"]').forEach(el => {
  el.addEventListener('click', (e) => { e.preventDefault(); logout(); });
});

// ─── Poll new orders badge ─────────────────────────────
async function pollNewOrders() {
  try {
    const stats = await apiFetch('/admin/stats');
    document.querySelectorAll('#newOrdersBadge').forEach(el => {
      el.textContent = stats.new_orders;
      el.style.display = stats.new_orders > 0 ? 'inline' : 'none';
    });
  } catch {}
}
pollNewOrders();
setInterval(pollNewOrders, 30000);

window._admin = { apiFetch, apiFetchForm, toast, confirm, logout, formatDate, formatDateTime, statusBadge, STATUS_OPTIONS, CATEGORIES, EVENT_LABELS };
