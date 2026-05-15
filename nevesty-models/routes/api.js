const express = require('express');
const router = express.Router();
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const multer = require('multer');
const path = require('path');
const fs = require('fs');
const { query, run, get, generateOrderNumber } = require('../database');
const auth = require('../middleware/auth');

let botInstance = null;
function setBot(bot) { botInstance = bot; }

// ─── Validation helpers ───────────────────────────────────────────────────────
const ALLOWED_EVENT_TYPES = ['fashion_show', 'photo_shoot', 'event', 'commercial', 'runway', 'other'];
const ALLOWED_IMG_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp'];
const ALLOWED_CATEGORIES = ['fashion', 'commercial', 'events'];
const ALLOWED_STATUSES = ['new', 'reviewing', 'confirmed', 'in_progress', 'completed', 'cancelled'];

function sanitize(s, max = 500) {
  if (typeof s !== 'string') return null;
  return s.trim().slice(0, max) || null;
}
function validatePhone(p) { return typeof p === 'string' && /^[\d\s\+\(\)\-]{7,20}$/.test(p.trim()); }
function validateEmail(e) { return !e || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((e || '').trim()); }
function validateDate(d) { return !d || (/^\d{4}-\d{2}-\d{2}$/.test(d) && !isNaN(Date.parse(d))); }

// ─── Input sanitization middleware ───────────────────────────────────────────
// Strip null bytes and control chars from all string body inputs
router.use((req, res, next) => {
  if (req.body && typeof req.body === 'object') {
    for (const [key, val] of Object.entries(req.body)) {
      if (typeof val === 'string') {
        req.body[key] = val.replace(/\x00/g, '').trim();
      }
    }
  }
  next();
});

// ─── File utilities ───────────────────────────────────────────────────────────
function deleteFile(urlPath) {
  if (!urlPath || typeof urlPath !== 'string') return;
  try {
    const uploadsDir = path.resolve(__dirname, '..', 'uploads');
    const abs = path.resolve(__dirname, '..', urlPath.replace(/^\//, ''));
    if (!abs.startsWith(uploadsDir + path.sep)) {
      console.warn('deleteFile blocked path traversal:', urlPath);
      return;
    }
    if (fs.existsSync(abs)) fs.unlinkSync(abs);
  } catch (e) { console.warn('deleteFile error:', e.message); }
}

// ─── CSV helpers ──────────────────────────────────────────────────────────────
function csvCell(v) {
  let s = (v == null ? '' : String(v));
  if (/^[=+\-@]/.test(s)) s = "'" + s;
  return '"' + s.replace(/"/g, '""') + '"';
}

const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    const dir = path.join(__dirname, '../uploads');
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    cb(null, `model_${Date.now()}_${Math.random().toString(36).slice(2)}${ext}`);
  }
});
const upload = multer({
  storage,
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase();
    if (file.mimetype.startsWith('image/') && ALLOWED_IMG_EXTS.includes(ext)) cb(null, true);
    else cb(new Error('Допускаются только изображения JPG, PNG, GIF, WebP'));
  }
});

// ─── Public config ────────────────────────────────────────────────────────────
router.get('/config', (req, res) => {
  res.json({
    bot_username: process.env.BOT_USERNAME || '',
    agency_phone: process.env.AGENCY_PHONE || '',
    agency_email: process.env.AGENCY_EMAIL || '',
  });
});

// ─── Auth ─────────────────────────────────────────────────────────────────────
router.post('/admin/login', async (req, res, next) => {
  try {
    const { username, password } = req.body;
    if (!username || !password) return res.status(400).json({ error: 'Укажите логин и пароль' });
    const admin = await get('SELECT * FROM admins WHERE username = ?', [username]);
    if (!admin) return res.status(401).json({ error: 'Неверный логин или пароль' });
    const ok = await bcrypt.compare(password, admin.password_hash);
    if (!ok) return res.status(401).json({ error: 'Неверный логин или пароль' });
    const token = jwt.sign(
      { id: admin.id, username: admin.username, role: admin.role },
      process.env.JWT_SECRET || 'secret',
      { expiresIn: '24h' }
    );
    res.json({ token, admin: { id: admin.id, username: admin.username, role: admin.role } });
  } catch (e) { next(e); }
});

router.get('/admin/me', auth, async (req, res, next) => {
  try {
    const admin = await get('SELECT id, username, email, role, telegram_id FROM admins WHERE id = ?', [req.admin.id]);
    res.json(admin);
  } catch (e) { next(e); }
});

router.put('/admin/me', auth, async (req, res, next) => {
  try {
    const { email, telegram_id, current_password, new_password } = req.body;
    if (email && !validateEmail(email)) return res.status(400).json({ error: 'Некорректный email' });
    const admin = await get('SELECT * FROM admins WHERE id = ?', [req.admin.id]);
    if (new_password) {
      if (new_password.length < 6) return res.status(400).json({ error: 'Пароль минимум 6 символов' });
      const ok = await bcrypt.compare(current_password, admin.password_hash);
      if (!ok) return res.status(400).json({ error: 'Неверный текущий пароль' });
      const hash = await bcrypt.hash(new_password, 10);
      await run('UPDATE admins SET email=?, telegram_id=?, password_hash=? WHERE id=?', [email || null, telegram_id || null, hash, req.admin.id]);
    } else {
      await run('UPDATE admins SET email=?, telegram_id=? WHERE id=?', [email || null, telegram_id || null, req.admin.id]);
    }
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Agent logs feed — for dashboard ─────────────────────────────────────────
router.get('/agent-logs', async (req, res) => {
  try {
    const limit = Math.min(100, parseInt(req.query.limit) || 50);
    const logs = await query(
      'SELECT * FROM agent_logs ORDER BY created_at DESC LIMIT ?',
      [limit]
    );
    res.json(logs);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─── Agent logs (auth-protected) ─────────────────────────────────────────────
router.get('/admin/agent-logs', auth, async (req, res, next) => {
  try {
    const limit = Math.min(parseInt(req.query.limit) || 50, 200);
    const logs = await query('SELECT * FROM agent_logs ORDER BY created_at DESC LIMIT ?', [limit]);
    res.json(logs);
  } catch(e) { next(e); }
});

// ─── Stats ────────────────────────────────────────────────────────────────────
router.get('/admin/stats', auth, async (req, res, next) => {
  try {
    const [total, newO, active, totalM, availM] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE status = 'new'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('reviewing','confirmed','in_progress')"),
      get('SELECT COUNT(*) as n FROM models'),
      get("SELECT COUNT(*) as n FROM models WHERE available = 1"),
    ]);
    // Orders by status (for chart)
    const byStatus = await query(
      `SELECT status, COUNT(*) as count FROM orders GROUP BY status`
    );
    // Messages with unread indicator (admin hasn't replied)
    const unread = await get(
      `SELECT COUNT(DISTINCT o.id) as n FROM orders o
       WHERE o.status NOT IN ('completed','cancelled')
       AND EXISTS (
         SELECT 1 FROM messages m WHERE m.order_id = o.id AND m.sender_type = 'client'
         AND NOT EXISTS (
           SELECT 1 FROM messages m2 WHERE m2.order_id = o.id AND m2.sender_type = 'admin' AND m2.created_at > m.created_at
         )
       )`
    );
    const recent = await query(
      `SELECT o.*, m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       ORDER BY o.created_at DESC LIMIT 8`
    );
    // Last 7 days order counts
    const daily7d = await query(
      `SELECT date(created_at) as day, COUNT(*) as count
       FROM orders
       WHERE created_at >= date('now', '-6 days')
       GROUP BY date(created_at)
       ORDER BY day ASC`
    );
    const daily7dMap = {};
    daily7d.forEach(r => { daily7dMap[r.day] = r.count; });
    const daily7dFull = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      const key = d.toISOString().slice(0, 10);
      const label = d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
      daily7dFull.push({ day: key, label, count: daily7dMap[key] || 0 });
    }
    res.json({
      total_orders: total.n,
      new_orders: newO.n,
      active_orders: active.n,
      total_models: totalM.n,
      available_models: availM.n,
      unread_messages: unread?.n || 0,
      by_status: byStatus,
      recent,
      daily_7d: daily7dFull
    });
  } catch (e) { next(e); }
});

// ─── Admin stats (extended: today, completed, new_clients, pending_reviews) ───
router.get('/admin/stats/extended2', auth, async (req, res, next) => {
  try {
    const [todayOrders, activeOrders, completedOrders, newClients, pendingReviews] = await Promise.all([
      get("SELECT COUNT(*) as n FROM orders WHERE date(created_at) = date('now')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('reviewing','confirmed','in_progress')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status = 'completed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= date('now', '-30 days')"),
      get("SELECT COUNT(*) as n FROM reviews WHERE approved = 0"),
    ]);
    res.json({
      today_orders: todayOrders.n,
      active_orders: activeOrders.n,
      completed_orders: completedOrders.n,
      new_clients_30d: newClients.n,
      pending_reviews: pendingReviews.n,
    });
  } catch (e) { next(e); }
});

// ─── Orders chart — daily counts for N days ───────────────────────────────────
router.get('/admin/orders-chart', auth, async (req, res, next) => {
  try {
    const days = Math.min(90, Math.max(7, parseInt(req.query.days) || 30));
    const rows = await query(
      `SELECT date(created_at) as day, COUNT(*) as count
       FROM orders
       WHERE created_at >= date('now', '-${days - 1} days')
       GROUP BY date(created_at)
       ORDER BY day ASC`
    );
    const countMap = {};
    rows.forEach(r => { countMap[r.day] = r.count; });
    const result = [];
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      const key = d.toISOString().slice(0, 10);
      const label = d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
      result.push({ day: key, label, count: countMap[key] || 0 });
    }
    res.json({ days: result, total: rows.reduce((s, r) => s + r.count, 0) });
  } catch (e) { next(e); }
});

// ─── Notifications center ──────────────────────────────────────────────────────
// GET  /api/admin/notifications          → list (new orders, pending reviews, unread messages)
// POST /api/admin/notifications/read     → mark items as read (stores in-memory per session)
const _notifReadSet = new Set(); // lightweight: reset on restart

router.get('/admin/notifications', auth, async (req, res, next) => {
  try {
    const [newOrders, pendingReviews, unreadOrders] = await Promise.all([
      query(
        `SELECT id, order_number, client_name, created_at FROM orders WHERE status='new' ORDER BY created_at DESC LIMIT 10`
      ),
      query(
        `SELECT id, client_name, rating, text, created_at FROM reviews WHERE approved=0 ORDER BY created_at DESC LIMIT 5`
      ),
      query(
        `SELECT DISTINCT o.id, o.order_number, o.client_name, o.created_at
         FROM orders o
         WHERE o.status NOT IN ('completed','cancelled')
         AND EXISTS (
           SELECT 1 FROM messages m WHERE m.order_id = o.id AND m.sender_type = 'client'
           AND NOT EXISTS (
             SELECT 1 FROM messages m2 WHERE m2.order_id = o.id AND m2.sender_type = 'admin' AND m2.created_at > m.created_at
           )
         )
         ORDER BY o.created_at DESC LIMIT 5`
      ),
    ]);
    const notifications = [];
    newOrders.forEach(o => notifications.push({
      id: `order_new_${o.id}`,
      type: 'new_order',
      title: `Новая заявка: ${o.order_number}`,
      text: o.client_name,
      link: `/admin/orders.html?id=${o.id}`,
      created_at: o.created_at,
      read: _notifReadSet.has(`order_new_${o.id}`),
    }));
    pendingReviews.forEach(r => notifications.push({
      id: `review_${r.id}`,
      type: 'pending_review',
      title: `Отзыв на модерации`,
      text: `${r.client_name} — ${'★'.repeat(r.rating)}`,
      link: `/admin/settings.html#reviews`,
      created_at: r.created_at,
      read: _notifReadSet.has(`review_${r.id}`),
    }));
    unreadOrders.forEach(o => notifications.push({
      id: `msg_${o.id}`,
      type: 'unread_message',
      title: `Непрочитанное сообщение`,
      text: `Заявка ${o.order_number} — ${o.client_name}`,
      link: `/admin/orders.html?id=${o.id}`,
      created_at: o.created_at,
      read: _notifReadSet.has(`msg_${o.id}`),
    }));
    notifications.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    const unreadCount = notifications.filter(n => !n.read).length;
    res.json({ notifications: notifications.slice(0, 10), unread_count: unreadCount });
  } catch (e) { next(e); }
});

router.post('/admin/notifications/read', auth, async (req, res, next) => {
  try {
    const { ids } = req.body;
    if (Array.isArray(ids)) ids.forEach(id => _notifReadSet.add(id));
    else _notifReadSet.add('__all__');
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Models (public) ──────────────────────────────────────────────────────────
router.get('/models', async (req, res, next) => {
  try {
    const { category, hair_color, min_height, max_height, min_age, max_age, city, available, search } = req.query;
    let sql = 'SELECT id, name, age, height, city, category, available, photo_main, bio, instagram, hair_color, eye_color, weight, bust, waist, hips, shoe_size, photos FROM models WHERE 1=1';
    const params = [];
    if (category && ALLOWED_CATEGORIES.includes(category)) { sql += ' AND category = ?'; params.push(category); }
    if (hair_color) { sql += ' AND hair_color = ?'; params.push(hair_color); }
    if (min_height && !isNaN(+min_height)) { sql += ' AND height >= ?'; params.push(+min_height); }
    if (max_height && !isNaN(+max_height)) { sql += ' AND height <= ?'; params.push(+max_height); }
    if (min_age && !isNaN(+min_age)) { sql += ' AND age >= ?'; params.push(+min_age); }
    if (max_age && !isNaN(+max_age)) { sql += ' AND age <= ?'; params.push(+max_age); }
    if (city) { sql += ' AND city = ?'; params.push(city); }
    if (available === '1') { sql += ' AND available = 1'; }
    if (available === '0') { sql += ' AND available = 0'; }
    if (search) { sql += ' AND (name LIKE ? OR bio LIKE ?)'; params.push(`%${search}%`, `%${search}%`); }
    sql += ' ORDER BY available DESC, id DESC LIMIT 200';
    const models = await query(sql, params);
    res.json(models.map(m => ({ ...m, photos: JSON.parse(m.photos || '[]') })));
  } catch (e) { next(e); }
});

router.get('/models/:id', async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT * FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    res.json({ ...m, photos: JSON.parse(m.photos || '[]') });
  } catch (e) { next(e); }
});

// ─── Models PATCH (quick availability toggle, public auth via JWT) ────────────
router.patch('/api/models/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const { available } = req.body;
    if (available === undefined) return res.status(400).json({ error: 'Поле available обязательно' });
    const val = available ? 1 : 0;
    const m = await get('SELECT id FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    await run('UPDATE models SET available = ? WHERE id = ?', [val, id]);
    res.json({ ok: true, available: val });
  } catch (e) { next(e); }
});

router.patch('/models/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const { available } = req.body;
    if (available === undefined) return res.status(400).json({ error: 'Поле available обязательно' });
    const val = available ? 1 : 0;
    const m = await get('SELECT id FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    await run('UPDATE models SET available = ? WHERE id = ?', [val, id]);
    res.json({ ok: true, available: val });
  } catch (e) { next(e); }
});

// ─── Models (admin CRUD) ──────────────────────────────────────────────────────
router.post('/admin/models', auth, upload.fields([{ name: 'photo_main', maxCount: 1 }, { name: 'photos', maxCount: 10 }]), async (req, res, next) => {
  try {
    const { name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, instagram, category, available } = req.body;
    if (!name) return res.status(400).json({ error: 'Укажите имя модели' });
    if (category && !ALLOWED_CATEGORIES.includes(category)) return res.status(400).json({ error: 'Недопустимая категория' });
    const photo_main = req.files?.photo_main?.[0] ? `/uploads/${req.files.photo_main[0].filename}` : null;
    const photos = (req.files?.photos || []).map(f => `/uploads/${f.filename}`);
    const result = await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,photo_main,photos,instagram,category,available)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      [sanitize(name, 100), +age || null, +height || null, +weight || null, +bust || null, +waist || null, +hips || null, sanitize(shoe_size, 10), sanitize(hair_color, 50), sanitize(eye_color, 50), sanitize(bio, 2000), photo_main, JSON.stringify(photos), sanitize(instagram, 100), category || 'fashion', available === '1' ? 1 : 0]
    );
    res.json({ id: result.id });
  } catch (e) { next(e); }
});

router.put('/admin/models/:id', auth, upload.fields([{ name: 'photo_main', maxCount: 1 }, { name: 'photos', maxCount: 10 }]), async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const existing = await get('SELECT * FROM models WHERE id = ?', [id]);
    if (!existing) return res.status(404).json({ error: 'Модель не найдена' });
    const { name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, instagram, category, available } = req.body;
    // Replace main photo if new one uploaded
    let photo_main = existing.photo_main;
    if (req.files?.photo_main?.[0]) {
      deleteFile(existing.photo_main);
      photo_main = `/uploads/${req.files.photo_main[0].filename}`;
    }
    let photos = JSON.parse(existing.photos || '[]');
    if (req.files?.photos?.length) {
      photos = [...photos, ...req.files.photos.map(f => `/uploads/${f.filename}`)];
    }
    await run(
      `UPDATE models SET name=?,age=?,height=?,weight=?,bust=?,waist=?,hips=?,shoe_size=?,hair_color=?,eye_color=?,bio=?,photo_main=?,photos=?,instagram=?,category=?,available=? WHERE id=?`,
      [sanitize(name, 100), +age || null, +height || null, +weight || null, +bust || null, +waist || null, +hips || null, sanitize(shoe_size, 10), sanitize(hair_color, 50), sanitize(eye_color, 50), sanitize(bio, 2000), photo_main, JSON.stringify(photos), sanitize(instagram, 100), category || existing.category, available === '1' ? 1 : 0, id]
    );
    res.json({ ok: true });
  } catch (e) { next(e); }
});

router.delete('/admin/models/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT * FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    // Delete all photos from disk
    deleteFile(m.photo_main);
    const photos = JSON.parse(m.photos || '[]');
    photos.forEach(p => deleteFile(p));
    await run('DELETE FROM models WHERE id = ?', [id]);
    res.json({ ok: true });
  } catch (e) { next(e); }
});

router.delete('/admin/models/:id/photo', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const { photo } = req.body;
    const m = await get('SELECT photos FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    const photos = JSON.parse(m.photos || '[]').filter(p => p !== photo);
    await run('UPDATE models SET photos = ? WHERE id = ?', [JSON.stringify(photos), id]);
    deleteFile(photo);
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Orders (public) ──────────────────────────────────────────────────────────
router.post('/orders', async (req, res, next) => {
  try {
    const { client_name, client_phone, client_email, client_telegram, client_chat_id,
            model_id, event_type, event_date, event_duration, location, budget, comments } = req.body;

    if (!sanitize(client_name, 100)) return res.status(400).json({ error: 'Укажите ваше имя' });
    if (!client_phone || !validatePhone(client_phone)) return res.status(400).json({ error: 'Укажите корректный номер телефона' });
    if (!ALLOWED_EVENT_TYPES.includes(event_type)) return res.status(400).json({ error: 'Неверный тип мероприятия' });
    if (!validateEmail(client_email)) return res.status(400).json({ error: 'Некорректный email' });
    if (!validateDate(event_date)) return res.status(400).json({ error: 'Некорректная дата' });

    const duration = Math.min(Math.max(parseInt(event_duration, 10) || 4, 1), 48);
    const order_number = generateOrderNumber();

    const s = {
      client_name: sanitize(client_name, 100),
      client_phone: client_phone.trim().slice(0, 20),
      client_email: sanitize(client_email, 100),
      client_telegram: sanitize(client_telegram, 64),
      client_chat_id: sanitize(client_chat_id, 32),
      model_id: model_id ? (parseInt(model_id, 10) || null) : null,
      event_type,
      event_date: event_date || null,
      event_duration: duration,
      location: sanitize(location, 200),
      budget: sanitize(budget, 100),
      comments: sanitize(comments, 2000),
    };

    const result = await run(
      `INSERT INTO orders (order_number,client_name,client_phone,client_email,client_telegram,client_chat_id,model_id,event_type,event_date,event_duration,location,budget,comments)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      [order_number, s.client_name, s.client_phone, s.client_email, s.client_telegram, s.client_chat_id, s.model_id, s.event_type, s.event_date, s.event_duration, s.location, s.budget, s.comments]
    );

    if (botInstance) {
      botInstance.notifyNewOrder({ id: result.id, order_number, ...s }).catch(e => console.error('Bot notify error:', e.message));
    }

    res.json({ order_number, id: result.id });
  } catch (e) { next(e); }
});

router.get('/orders/status/:order_number', async (req, res, next) => {
  try {
    const order = await get(
      `SELECT o.order_number, o.status, o.event_type, o.event_date, o.client_name, o.created_at, m.name as model_name
       FROM orders o LEFT JOIN models m ON o.model_id = m.id
       WHERE o.order_number = ?`,
      [req.params.order_number.toUpperCase()]
    );
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    res.json(order);
  } catch (e) { next(e); }
});

// GET /api/orders/status?number=ORD-XXXX — public status check by query param
router.get('/orders/status', async (req, res, next) => {
  try {
    const number = (req.query.number || '').trim().toUpperCase();
    if (!number) return res.status(400).json({ error: 'Укажите номер заявки' });
    const order = await get(
      `SELECT o.order_number, o.status, o.event_type, o.event_date, o.client_name, o.created_at, m.name as model_name
       FROM orders o LEFT JOIN models m ON o.model_id = m.id
       WHERE o.order_number = ?`,
      [number]
    );
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    res.json(order);
  } catch (e) { next(e); }
});

// ─── Favorites (wishlist) — stored by localStorage key on site, chat_id in bot ─
// GET  /api/favorites?ids=1,2,3        → public, returns model stubs for given IDs
// POST /api/favorites/check            → check if model is in DB (validation)
router.get('/favorites', async (req, res, next) => {
  try {
    const rawIds = (req.query.ids || '').split(',').map(x => parseInt(x)).filter(Boolean);
    if (!rawIds.length) return res.json([]);
    const placeholders = rawIds.map(() => '?').join(',');
    const models = await query(
      `SELECT id, name, height, category, available, photo_main FROM models WHERE id IN (${placeholders})`,
      rawIds
    );
    res.json(models);
  } catch (e) { next(e); }
});

// ─── Quick booking (name + phone only) ────────────────────────────────────────
router.post('/quick-booking', async (req, res, next) => {
  try {
    const { client_name, client_phone } = req.body;
    if (!sanitize(client_name, 100)) return res.status(400).json({ error: 'Укажите имя' });
    if (!client_phone || !validatePhone(client_phone)) return res.status(400).json({ error: 'Укажите корректный номер телефона' });
    const result = await run(
      `INSERT INTO quick_bookings (client_name, client_phone) VALUES (?,?)`,
      [sanitize(client_name, 100), client_phone.trim().slice(0, 20)]
    );
    // Also create a real order so admin sees it
    const order_number = generateOrderNumber();
    const ordResult = await run(
      `INSERT INTO orders (order_number,client_name,client_phone,event_type,comments)
       VALUES (?,?,?,'other',?)`,
      [order_number, sanitize(client_name, 100), client_phone.trim().slice(0, 20), 'Быстрая заявка — менеджер уточнит детали']
    );
    const order = await get('SELECT * FROM orders WHERE id=?', [ordResult.id]);
    if (botInstance && order) {
      botInstance.notifyNewOrder({ ...order, order_number }).catch(e => console.error('Bot notify quick booking:', e.message));
    }
    res.json({ ok: true, order_number });
  } catch (e) { next(e); }
});

// ─── Admin: quick bookings list ───────────────────────────────────────────────
router.get('/admin/quick-bookings', auth, async (req, res, next) => {
  try {
    const rows = await query('SELECT * FROM quick_bookings ORDER BY created_at DESC LIMIT 100');
    res.json(rows);
  } catch (e) { next(e); }
});

// ─── Orders (admin) ───────────────────────────────────────────────────────────
router.get('/admin/orders', auth, async (req, res, next) => {
  try {
    const { status, search } = req.query;
    const page = Math.max(1, parseInt(req.query.page) || 1);
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit) || 25));
    const offset = (page - 1) * limit;
    let where = '1=1';
    const params = [];
    if (status && ALLOWED_STATUSES.includes(status)) { where += ' AND o.status = ?'; params.push(status); }
    if (search) {
      where += ' AND (o.client_name LIKE ? OR o.order_number LIKE ? OR o.client_phone LIKE ?)';
      params.push(`%${search}%`, `%${search}%`, `%${search}%`);
    }
    const [totalRow, orders] = await Promise.all([
      get(`SELECT COUNT(*) as n FROM orders o WHERE ${where}`, params),
      query(`SELECT o.*, m.name as model_name, a.username as manager_name
             FROM orders o
             LEFT JOIN models m ON o.model_id = m.id
             LEFT JOIN admins a ON o.manager_id = a.id
             WHERE ${where}
             ORDER BY o.created_at DESC
             LIMIT ? OFFSET ?`, [...params, limit, offset])
    ]);
    res.json({
      orders,
      total: totalRow.n,
      page,
      pages: Math.ceil(totalRow.n / limit),
      limit
    });
  } catch (e) { next(e); }
});

router.get('/admin/orders/export', auth, async (req, res, next) => {
  try {
    const { status, search, period } = req.query;
    let where = '1=1'; const params = [];
    if (status && ALLOWED_STATUSES.includes(status)) { where += ' AND o.status = ?'; params.push(status); }
    if (search) { where += ' AND (o.client_name LIKE ? OR o.order_number LIKE ?)'; params.push(`%${search}%`, `%${search}%`); }
    if (period === 'today')  { where += " AND date(o.created_at) = date('now')"; }
    if (period === 'week')   { where += " AND o.created_at >= date('now', '-7 days')"; }
    if (period === 'month')  { where += " AND o.created_at >= date('now', '-30 days')"; }
    const orders = await query(
      `SELECT o.order_number, o.client_name, o.client_phone, o.client_email, o.client_telegram,
              o.event_type, o.event_date, o.event_duration, o.location, o.budget, o.comments,
              o.status, o.admin_notes, m.name as model_name, a.username as manager_name,
              o.created_at, o.updated_at
       FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       LEFT JOIN admins a ON o.manager_id = a.id
       WHERE ${where} ORDER BY o.created_at DESC`, params
    );
    const STATUS_RU = { new:'Новая', reviewing:'На рассмотрении', confirmed:'Подтверждена', in_progress:'В процессе', completed:'Завершена', cancelled:'Отменена' };
    const EVENT_RU = { fashion_show:'Показ мод', photo_shoot:'Фотосессия', event:'Мероприятие', commercial:'Коммерческая', runway:'Подиум', other:'Другое' };
    const headers = ['Номер','Клиент','Телефон','Email','Telegram','Мероприятие','Дата','Часов','Место','Бюджет','Комментарий','Статус','Заметки','Модель','Менеджер','Создана','Обновлена'];
    const csvRow = (cols) => cols.map(csvCell).join(',');
    const rows = [csvRow(headers), ...orders.map(o => csvRow([
      o.order_number, o.client_name, o.client_phone, o.client_email || '', o.client_telegram || '',
      EVENT_RU[o.event_type] || o.event_type, o.event_date || '', o.event_duration,
      o.location || '', o.budget || '', o.comments || '',
      STATUS_RU[o.status] || o.status, o.admin_notes || '', o.model_name || '', o.manager_name || '',
      o.created_at, o.updated_at
    ]))];
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="orders_${Date.now()}.csv"`);
    res.send('﻿' + rows.join('\n')); // BOM for Excel
  } catch (e) { next(e); }
});

router.get('/admin/orders/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const order = await get(
      `SELECT o.*, m.name as model_name, m.photo_main as model_photo, a.username as manager_name
       FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       LEFT JOIN admins a ON o.manager_id = a.id
       WHERE o.id = ?`,
      [id]
    );
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    const messages = await query('SELECT * FROM messages WHERE order_id = ? ORDER BY created_at ASC', [id]);
    const hasUnread = messages.some(m => m.sender_type === 'client') &&
      !messages.slice().reverse().find(m => m.sender_type === 'admin');
    res.json({ ...order, messages, has_unread: hasUnread });
  } catch (e) { next(e); }
});

router.put('/admin/orders/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const { status, admin_notes, manager_id } = req.body;
    if (status && !ALLOWED_STATUSES.includes(status)) return res.status(400).json({ error: 'Недопустимый статус' });
    const order = await get('SELECT * FROM orders WHERE id = ?', [id]);
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    await run(
      `UPDATE orders SET status=COALESCE(?,status), admin_notes=?, manager_id=COALESCE(?,manager_id), updated_at=CURRENT_TIMESTAMP WHERE id=?`,
      [status || null, admin_notes !== undefined ? sanitize(admin_notes, 2000) : order.admin_notes, manager_id || null, id]
    );
    if (botInstance && order.client_chat_id && status && status !== order.status) {
      botInstance.notifyStatusChange(order.client_chat_id, order.order_number, status);
    }
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Bulk actions ─────────────────────────────────────────────────────────────
router.post('/admin/orders/bulk', auth, async (req, res, next) => {
  try {
    const { ids, action } = req.body;
    if (!Array.isArray(ids) || !ids.length) return res.status(400).json({ error: 'Не указаны заявки' });
    const validIds = ids.map(Number).filter(n => n > 0);
    if (!validIds.length) return res.status(400).json({ error: 'Некорректные ID заявок' });
    if (!ALLOWED_STATUSES.includes(action) && action !== 'delete') return res.status(400).json({ error: 'Недопустимое действие' });
    if (action === 'delete') {
      await run(`DELETE FROM orders WHERE id IN (${validIds.map(() => '?').join(',')})`, validIds);
    } else {
      const orders = await query(`SELECT id, client_chat_id, order_number, status FROM orders WHERE id IN (${validIds.map(() => '?').join(',')})`, validIds);
      await run(`UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id IN (${validIds.map(() => '?').join(',')})`, [action, ...validIds]);
      // Notify clients whose status changed (parallel)
      if (botInstance) {
        const toNotify = orders.filter(o => o.status !== action && o.client_chat_id);
        await Promise.allSettled(
          toNotify.map(o => botInstance.notifyStatusChange(o.client_chat_id, o.order_number, action))
        );
      }
    }
    res.json({ ok: true, affected: validIds.length });
  } catch (e) { next(e); }
});

// ─── Messages ─────────────────────────────────────────────────────────────────
router.post('/admin/orders/:id/message', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const content = sanitize(req.body.content, 2000);
    if (!content) return res.status(400).json({ error: 'Сообщение не может быть пустым' });
    const order = await get('SELECT * FROM orders WHERE id = ?', [id]);
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    const admin = await get('SELECT username FROM admins WHERE id = ?', [req.admin.id]);
    await run('INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)',
      [id, 'admin', admin.username, content]);
    if (botInstance) {
      if (order.client_chat_id) {
        botInstance.sendMessageToClient(order.client_chat_id, order.order_number, content, admin.username);
      }
    }
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Admin broadcast ──────────────────────────────────────────────────────────
// POST /api/admin/notify — send a custom Telegram message to all admins
router.post('/admin/notify', auth, async (req, res, next) => {
  try {
    const text = sanitize(req.body.text, 1000);
    if (!text) return res.status(400).json({ error: 'Текст не может быть пустым' });
    if (botInstance?.notifyAdmin) {
      await botInstance.notifyAdmin(`📢 *${req.admin.username}:*\n${text}`);
    }
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Managers ─────────────────────────────────────────────────────────────────
router.get('/admin/managers', auth, async (req, res, next) => {
  try {
    if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
    const managers = await query('SELECT id, username, email, role, telegram_id, created_at FROM admins ORDER BY created_at DESC');
    res.json(managers);
  } catch (e) { next(e); }
});

router.post('/admin/managers', auth, async (req, res, next) => {
  try {
    if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
    const { username, email, password, role, telegram_id } = req.body;
    if (!username || !/^[a-zA-Z0-9_]{3,32}$/.test(username)) return res.status(400).json({ error: 'Логин: 3–32 символа, только буквы/цифры/_' });
    if (!password || password.length < 6) return res.status(400).json({ error: 'Пароль минимум 6 символов' });
    if (email && !validateEmail(email)) return res.status(400).json({ error: 'Некорректный email' });
    const existing = await get('SELECT id FROM admins WHERE username = ?', [username]);
    if (existing) return res.status(409).json({ error: 'Логин уже занят' });
    const hash = await bcrypt.hash(password, 10);
    const result = await run(
      'INSERT INTO admins (username, email, password_hash, role, telegram_id) VALUES (?,?,?,?,?)',
      [username, email || null, hash, ['manager', 'superadmin'].includes(role) ? role : 'manager', telegram_id || null]
    );
    res.json({ id: result.id });
  } catch (e) { next(e); }
});

router.delete('/admin/managers/:id', auth, async (req, res, next) => {
  try {
    if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    if (id === req.admin.id) return res.status(400).json({ error: 'Нельзя удалить себя' });
    await run('DELETE FROM admins WHERE id = ?', [id]);
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// GET /api/settings — возвращает все настройки бота
router.get('/settings', auth, async (req, res, next) => {
  try {
    const rows = await query('SELECT key, value FROM bot_settings ORDER BY key');
    const settings = {};
    rows.forEach(r => { settings[r.key] = r.value; });
    res.json(settings);
  } catch(e) { next(e); }
});

// DELETE /api/admin/sessions — clear active sessions (JWT is stateless; signals client logout)
router.delete('/admin/sessions', auth, async (req, res, next) => {
  try {
    // Stateless JWT — no server-side session store to clear.
    // Client will clear localStorage on receipt of this response.
    res.json({ ok: true, message: 'Sessions cleared' });
  } catch(e) { next(e); }
});

// PUT /api/settings — сохраняет настройки бота (принимает объект key:value)
router.put('/settings', auth, async (req, res, next) => {
  const ALLOWED_KEYS = [
    'greeting', 'about',
    'contacts_phone', 'contacts_email', 'contacts_insta', 'contacts_addr',
    'pricing',
    'notif_new_order', 'notif_status', 'notif_message',
    'agency_name', 'tagline', 'hero_image',
    'webhook_url', 'tg_notif_enabled',
  ];
  try {
    const body = req.body;
    if (typeof body !== 'object' || !body) return res.status(400).json({ error: 'Invalid body' });
    for (const [key, value] of Object.entries(body)) {
      if (!ALLOWED_KEYS.includes(key)) continue;
      const v = String(value ?? '').trim().slice(0, 2000);
      await run('INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', [key, v]);
    }
    res.json({ ok: true });
  } catch(e) { next(e); }
});

// ─── Reviews (public) ─────────────────────────────────────────────────────────
router.get('/reviews', async (req, res, next) => {
  try {
    const limit = Math.min(50, Math.max(1, parseInt(req.query.limit) || 20));
    const model_id = req.query.model_id ? parseInt(req.query.model_id) : null;
    let sql = 'SELECT r.id, r.client_name, r.rating, r.text, r.model_id, r.created_at, m.name as model_name FROM reviews r LEFT JOIN models m ON r.model_id = m.id WHERE r.approved = 1';
    const params = [];
    if (model_id && Number.isInteger(model_id) && model_id > 0) {
      sql += ' AND r.model_id = ?';
      params.push(model_id);
    }
    sql += ' ORDER BY r.created_at DESC LIMIT ?';
    params.push(limit);
    const reviews = await query(sql, params);
    res.json(reviews);
  } catch (e) { next(e); }
});

// ─── Reviews (admin) ──────────────────────────────────────────────────────────
router.get('/admin/reviews', auth, async (req, res, next) => {
  try {
    const page = Math.max(1, parseInt(req.query.page) || 1);
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit) || 25));
    const offset = (page - 1) * limit;
    const approved = req.query.approved; // '0', '1', or undefined (all)
    let where = '1=1';
    const params = [];
    if (approved === '0' || approved === '1') {
      where += ' AND r.approved = ?';
      params.push(parseInt(approved));
    }
    const [totalRow, reviews] = await Promise.all([
      get(`SELECT COUNT(*) as n FROM reviews r WHERE ${where}`, params),
      query(
        `SELECT r.*, m.name as model_name FROM reviews r
         LEFT JOIN models m ON r.model_id = m.id
         WHERE ${where}
         ORDER BY r.created_at DESC LIMIT ? OFFSET ?`,
        [...params, limit, offset]
      )
    ]);
    res.json({ reviews, total: totalRow.n, page, pages: Math.ceil(totalRow.n / limit), limit });
  } catch (e) { next(e); }
});

router.put('/admin/reviews/:id/approve', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const review = await get('SELECT id, approved FROM reviews WHERE id = ?', [id]);
    if (!review) return res.status(404).json({ error: 'Отзыв не найден' });
    const newApproved = review.approved ? 0 : 1;
    await run('UPDATE reviews SET approved = ? WHERE id = ?', [newApproved, id]);
    res.json({ ok: true, approved: newApproved });
  } catch (e) { next(e); }
});

router.delete('/admin/reviews/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const review = await get('SELECT id FROM reviews WHERE id = ?', [id]);
    if (!review) return res.status(404).json({ error: 'Отзыв не найден' });
    await run('DELETE FROM reviews WHERE id = ?', [id]);
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Order notes (admin) ──────────────────────────────────────────────────────
router.post('/orders/:id/notes', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const admin_note = sanitize(req.body.admin_note, 2000);
    if (!admin_note) return res.status(400).json({ error: 'Заметка не может быть пустой' });
    const order = await get('SELECT id FROM orders WHERE id = ?', [id]);
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    const result = await run(
      'INSERT INTO order_notes (order_id, admin_note) VALUES (?, ?)',
      [id, admin_note]
    );
    res.json({ id: result.id });
  } catch (e) { next(e); }
});

router.get('/orders/:id/notes', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const order = await get('SELECT id FROM orders WHERE id = ?', [id]);
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    const notes = await query(
      'SELECT * FROM order_notes WHERE order_id = ? ORDER BY created_at ASC',
      [id]
    );
    res.json(notes);
  } catch (e) { next(e); }
});

// ─── Extended stats ───────────────────────────────────────────────────────────
router.get('/stats/extended', auth, async (req, res, next) => {
  try {
    const [
      byDayOfWeek,
      byMonth,
      topModels,
      avgDuration,
      reviewStats
    ] = await Promise.all([
      // Orders by day of week (0=Sun ... 6=Sat)
      query(
        `SELECT CAST(strftime('%w', created_at) AS INTEGER) as day_of_week,
                COUNT(*) as count
         FROM orders
         GROUP BY day_of_week
         ORDER BY day_of_week`
      ),
      // Orders by month (last 12 months)
      query(
        `SELECT strftime('%Y-%m', created_at) as month,
                COUNT(*) as count
         FROM orders
         WHERE created_at >= date('now', '-12 months')
         GROUP BY month
         ORDER BY month`
      ),
      // Top 5 most booked models
      query(
        `SELECT m.id, m.name, m.photo_main, m.category,
                COUNT(o.id) as bookings,
                SUM(CASE WHEN o.status = 'completed' THEN 1 ELSE 0 END) as completed
         FROM models m
         LEFT JOIN orders o ON o.model_id = m.id
         GROUP BY m.id
         ORDER BY bookings DESC
         LIMIT 5`
      ),
      // Average booking duration per event type
      query(
        `SELECT event_type, ROUND(AVG(event_duration), 1) as avg_duration, COUNT(*) as count
         FROM orders
         GROUP BY event_type
         ORDER BY count DESC`
      ),
      // Review summary
      get(
        `SELECT COUNT(*) as total,
                SUM(CASE WHEN approved = 1 THEN 1 ELSE 0 END) as approved,
                ROUND(AVG(CASE WHEN approved = 1 THEN rating END), 2) as avg_rating
         FROM reviews`
      )
    ]);

    const DAY_NAMES = ['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
    const byDayNamed = byDayOfWeek.map(r => ({ ...r, day_name: DAY_NAMES[r.day_of_week] || '' }));

    res.json({
      orders_by_day_of_week: byDayNamed,
      orders_by_month: byMonth,
      top_models: topModels,
      avg_duration_by_event: avgDuration,
      review_stats: reviewStats || { total: 0, approved: 0, avg_rating: null }
    });
  } catch (e) { next(e); }
});

// ─── Export endpoints ─────────────────────────────────────────────────────────
router.get('/export/orders', auth, async (req, res, next) => {
  try {
    const orders = await query(`
      SELECT o.id, o.client_name, o.client_phone, o.service_type,
             o.event_date, o.status, o.created_at,
             m.name as model_name
      FROM orders o LEFT JOIN models m ON o.model_id = m.id
      ORDER BY o.created_at DESC
    `);

    const headers = ['ID','Клиент','Телефон','Услуга','Дата мероприятия','Статус','Дата заявки','Модель'];
    const rows = orders.map(o => [
      o.id, o.client_name, o.client_phone, o.service_type,
      o.event_date || '', o.status, o.created_at, o.model_name || ''
    ]);

    const csv = [headers, ...rows]
      .map(r => r.map(v => `"${String(v == null ? '' : v).replace(/"/g,'""')}"`).join(','))
      .join('\n');

    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="orders-${Date.now()}.csv"`);
    res.send('﻿' + csv); // BOM for Excel
  } catch(e) { next(e); }
});

router.get('/export/models', auth, async (req, res, next) => {
  try {
    const models = await query('SELECT id, name, age, height, city, category, available, photo_main, bio, instagram, hair_color, eye_color, weight, bust, waist, hips, shoe_size, photos FROM models ORDER BY name');
    res.setHeader('Content-Disposition', `attachment; filename="models-${Date.now()}.json"`);
    res.json(models);
  } catch(e) { next(e); }
});

// ─── Stats (simple summary) ───────────────────────────────────────────────────
router.get('/stats', auth, async (req, res, next) => {
  try {
    const [total, newCount, models, revenue] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE status='new'"),
      get('SELECT COUNT(*) as n FROM models'),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('confirmed','completed','in_progress')")
    ]);
    res.json({
      total: total.n, new: newCount.n,
      models: models.n, activeOrders: revenue.n,
      estimatedRevenue: revenue.n * 15000
    });
  } catch(e) { next(e); }
});

// ─── Agent discussions (admin) ────────────────────────────────────────────────
router.get('/admin/discussions', auth, async (req, res, next) => {
  try {
    const limit = Math.min(parseInt(req.query.limit) || 50, 200);
    const discussions = await query(
      'SELECT * FROM agent_discussions ORDER BY created_at DESC LIMIT ?',
      [limit]
    );
    res.json(discussions);
  } catch(e) { next(e); }
});

router.get('/admin/findings', auth, async (req, res, next) => {
  try {
    const status = req.query.status || 'open';
    const findings = await query(
      'SELECT * FROM agent_findings WHERE status=? ORDER BY created_at DESC LIMIT 100',
      [status]
    );
    res.json(findings);
  } catch(e) { next(e); }
});

module.exports = router;
module.exports.setBot = setBot;
