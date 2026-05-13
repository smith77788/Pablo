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

// ─── Validation helpers ──────────────────────────────────────────────────────
const ALLOWED_EVENT_TYPES = ['fashion_show', 'photo_shoot', 'event', 'commercial', 'runway', 'other'];
const ALLOWED_IMG_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp'];
const ALLOWED_CATEGORIES = ['fashion', 'commercial', 'events'];

function sanitizeStr(s, max = 500) {
  if (typeof s !== 'string') return null;
  return s.trim().slice(0, max) || null;
}

function validatePhone(phone) {
  return typeof phone === 'string' && /^[\d\s\+\(\)\-]{7,20}$/.test(phone.trim());
}

function validateEmail(email) {
  if (!email) return true; // optional
  return typeof email === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}

function validateDate(d) {
  if (!d) return true; // optional
  return /^\d{4}-\d{2}-\d{2}$/.test(d) && !isNaN(Date.parse(d));
}

// ─── File upload ────────────────────────────────────────────────────────────
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
    if (file.mimetype.startsWith('image/') && ALLOWED_IMG_EXTS.includes(ext)) {
      cb(null, true);
    } else {
      cb(new Error('Допускаются только изображения JPG, PNG, GIF, WebP'));
    }
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

// ─── Auth ─────────────────────────────────────────────────────────────────
router.post('/admin/login', async (req, res) => {
  try {
    const { username, password } = req.body;
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
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.get('/admin/me', auth, async (req, res) => {
  const admin = await get('SELECT id, username, email, role, telegram_id FROM admins WHERE id = ?', [req.admin.id]);
  res.json(admin);
});

router.put('/admin/me', auth, async (req, res) => {
  const { email, telegram_id, current_password, new_password } = req.body;
  const admin = await get('SELECT * FROM admins WHERE id = ?', [req.admin.id]);
  if (new_password) {
    const ok = await bcrypt.compare(current_password, admin.password_hash);
    if (!ok) return res.status(400).json({ error: 'Неверный текущий пароль' });
    const hash = await bcrypt.hash(new_password, 10);
    await run('UPDATE admins SET email=?, telegram_id=?, password_hash=? WHERE id=?', [email, telegram_id, hash, req.admin.id]);
  } else {
    await run('UPDATE admins SET email=?, telegram_id=? WHERE id=?', [email, telegram_id, req.admin.id]);
  }
  res.json({ ok: true });
});

// ─── Stats ────────────────────────────────────────────────────────────────
router.get('/admin/stats', auth, async (req, res) => {
  const [total_orders, new_orders, active_orders, total_models, available_models] = await Promise.all([
    get('SELECT COUNT(*) as n FROM orders'),
    get("SELECT COUNT(*) as n FROM orders WHERE status = 'new'"),
    get("SELECT COUNT(*) as n FROM orders WHERE status IN ('reviewing','confirmed','in_progress')"),
    get('SELECT COUNT(*) as n FROM models'),
    get("SELECT COUNT(*) as n FROM models WHERE available = 1"),
  ]);
  const recent = await query(
    `SELECT o.*, m.name as model_name FROM orders o
     LEFT JOIN models m ON o.model_id = m.id
     ORDER BY o.created_at DESC LIMIT 5`
  );
  res.json({ total_orders: total_orders.n, new_orders: new_orders.n, active_orders: active_orders.n, total_models: total_models.n, available_models: available_models.n, recent });
});

// ─── Models (public) ──────────────────────────────────────────────────────
router.get('/models', async (req, res) => {
  const { category, hair_color, min_height, max_height, available, search } = req.query;
  let sql = 'SELECT * FROM models WHERE 1=1';
  const params = [];
  if (category) { sql += ' AND category = ?'; params.push(category); }
  if (hair_color) { sql += ' AND hair_color = ?'; params.push(hair_color); }
  if (min_height) { sql += ' AND height >= ?'; params.push(+min_height); }
  if (max_height) { sql += ' AND height <= ?'; params.push(+max_height); }
  if (available === '1') { sql += ' AND available = 1'; }
  if (search) { sql += ' AND (name LIKE ? OR bio LIKE ?)'; params.push(`%${search}%`, `%${search}%`); }
  sql += ' ORDER BY id DESC';
  const models = await query(sql, params);
  res.json(models.map(m => ({ ...m, photos: JSON.parse(m.photos || '[]') })));
});

router.get('/models/:id', async (req, res) => {
  const m = await get('SELECT * FROM models WHERE id = ?', [req.params.id]);
  if (!m) return res.status(404).json({ error: 'Not found' });
  res.json({ ...m, photos: JSON.parse(m.photos || '[]') });
});

// ─── Models (admin) ───────────────────────────────────────────────────────
router.post('/admin/models', auth, upload.fields([{ name: 'photo_main', maxCount: 1 }, { name: 'photos', maxCount: 10 }]), async (req, res) => {
  const { name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, instagram, category, available } = req.body;
  const photo_main = req.files?.photo_main?.[0] ? `/uploads/${req.files.photo_main[0].filename}` : null;
  const photos = (req.files?.photos || []).map(f => `/uploads/${f.filename}`);
  const result = await run(
    `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,photo_main,photos,instagram,category,available)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    [name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, photo_main, JSON.stringify(photos), instagram, category, available === '1' ? 1 : 0]
  );
  res.json({ id: result.id });
});

router.put('/admin/models/:id', auth, upload.fields([{ name: 'photo_main', maxCount: 1 }, { name: 'photos', maxCount: 10 }]), async (req, res) => {
  const existing = await get('SELECT * FROM models WHERE id = ?', [req.params.id]);
  if (!existing) return res.status(404).json({ error: 'Not found' });
  const { name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, instagram, category, available } = req.body;
  const photo_main = req.files?.photo_main?.[0] ? `/uploads/${req.files.photo_main[0].filename}` : existing.photo_main;
  let photos = JSON.parse(existing.photos || '[]');
  if (req.files?.photos?.length) {
    photos = [...photos, ...(req.files.photos || []).map(f => `/uploads/${f.filename}`)];
  }
  await run(
    `UPDATE models SET name=?,age=?,height=?,weight=?,bust=?,waist=?,hips=?,shoe_size=?,hair_color=?,eye_color=?,bio=?,photo_main=?,photos=?,instagram=?,category=?,available=? WHERE id=?`,
    [name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, photo_main, JSON.stringify(photos), instagram, category, available === '1' ? 1 : 0, req.params.id]
  );
  res.json({ ok: true });
});

router.delete('/admin/models/:id', auth, async (req, res) => {
  await run('DELETE FROM models WHERE id = ?', [req.params.id]);
  res.json({ ok: true });
});

router.delete('/admin/models/:id/photo', auth, async (req, res) => {
  const { photo } = req.body;
  const m = await get('SELECT photos FROM models WHERE id = ?', [req.params.id]);
  const photos = JSON.parse(m.photos || '[]').filter(p => p !== photo);
  await run('UPDATE models SET photos = ? WHERE id = ?', [JSON.stringify(photos), req.params.id]);
  res.json({ ok: true });
});

// ─── Orders (public) ──────────────────────────────────────────────────────
router.post('/orders', async (req, res) => {
  try {
    const { client_name, client_phone, client_email, client_telegram, client_chat_id, model_id, event_type, event_date, event_duration, location, budget, comments } = req.body;

    // Required fields
    if (!sanitizeStr(client_name, 100)) return res.status(400).json({ error: 'Укажите ваше имя' });
    if (!client_phone || !validatePhone(client_phone)) return res.status(400).json({ error: 'Укажите корректный номер телефона' });
    if (!ALLOWED_EVENT_TYPES.includes(event_type)) return res.status(400).json({ error: 'Неверный тип мероприятия' });

    // Optional field validation
    if (!validateEmail(client_email)) return res.status(400).json({ error: 'Укажите корректный email' });
    if (!validateDate(event_date)) return res.status(400).json({ error: 'Некорректная дата мероприятия' });

    const duration = Math.min(Math.max(parseInt(event_duration, 10) || 4, 1), 48);

    const order_number = generateOrderNumber();

    const sanitized = {
      client_name: sanitizeStr(client_name, 100),
      client_phone: client_phone.trim().slice(0, 20),
      client_email: sanitizeStr(client_email, 100),
      client_telegram: sanitizeStr(client_telegram, 64),
      client_chat_id: sanitizeStr(client_chat_id, 32),
      model_id: model_id ? parseInt(model_id, 10) || null : null,
      event_type,
      event_date: event_date || null,
      event_duration: duration,
      location: sanitizeStr(location, 200),
      budget: sanitizeStr(budget, 100),
      comments: sanitizeStr(comments, 2000),
    };

    const result = await run(
      `INSERT INTO orders (order_number,client_name,client_phone,client_email,client_telegram,client_chat_id,model_id,event_type,event_date,event_duration,location,budget,comments)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      [order_number, sanitized.client_name, sanitized.client_phone, sanitized.client_email, sanitized.client_telegram, sanitized.client_chat_id, sanitized.model_id, sanitized.event_type, sanitized.event_date, sanitized.event_duration, sanitized.location, sanitized.budget, sanitized.comments]
    );

    if (botInstance) {
      botInstance.notifyNewOrder({ id: result.id, order_number, ...sanitized });
    }

    res.json({ order_number, id: result.id });
  } catch (e) {
    console.error('POST /orders error:', e);
    res.status(500).json({ error: 'Ошибка при создании заявки' });
  }
});

router.get('/orders/status/:order_number', async (req, res) => {
  const order = await get(
    `SELECT o.order_number, o.status, o.event_type, o.event_date, o.client_name, m.name as model_name
     FROM orders o LEFT JOIN models m ON o.model_id = m.id
     WHERE o.order_number = ?`,
    [req.params.order_number]
  );
  if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
  res.json(order);
});

// ─── Orders (admin) ───────────────────────────────────────────────────────
router.get('/admin/orders', auth, async (req, res) => {
  const { status, search } = req.query;
  let sql = `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id WHERE 1=1`;
  const params = [];
  if (status) { sql += ' AND o.status = ?'; params.push(status); }
  if (search) { sql += ' AND (o.client_name LIKE ? OR o.order_number LIKE ? OR o.client_phone LIKE ?)'; params.push(`%${search}%`, `%${search}%`, `%${search}%`); }
  sql += ' ORDER BY o.created_at DESC';
  const orders = await query(sql, params);
  res.json(orders);
});

router.get('/admin/orders/:id', auth, async (req, res) => {
  const order = await get(
    `SELECT o.*, m.name as model_name, m.photo_main as model_photo FROM orders o
     LEFT JOIN models m ON o.model_id = m.id WHERE o.id = ?`,
    [req.params.id]
  );
  if (!order) return res.status(404).json({ error: 'Not found' });
  const messages = await query('SELECT * FROM messages WHERE order_id = ? ORDER BY created_at ASC', [req.params.id]);
  res.json({ ...order, messages });
});

router.put('/admin/orders/:id', auth, async (req, res) => {
  const { status, admin_notes, manager_id } = req.body;
  const order = await get('SELECT * FROM orders WHERE id = ?', [req.params.id]);
  if (!order) return res.status(404).json({ error: 'Not found' });
  await run(
    `UPDATE orders SET status=?, admin_notes=?, manager_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?`,
    [status || order.status, admin_notes !== undefined ? admin_notes : order.admin_notes, manager_id || order.manager_id, req.params.id]
  );
  // Notify client via bot
  if (botInstance && order.client_chat_id && status && status !== order.status) {
    botInstance.notifyStatusChange(order.client_chat_id, order.order_number, status);
  }
  res.json({ ok: true });
});

// ─── Messages ─────────────────────────────────────────────────────────────
router.post('/admin/orders/:id/message', auth, async (req, res) => {
  const content = sanitizeStr(req.body.content, 2000);
  if (!content) return res.status(400).json({ error: 'Сообщение не может быть пустым' });
  const order = await get('SELECT * FROM orders WHERE id = ?', [req.params.id]);
  if (!order) return res.status(404).json({ error: 'Not found' });
  const admin = await get('SELECT username FROM admins WHERE id = ?', [req.admin.id]);
  await run('INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)', [req.params.id, 'admin', admin.username, content]);
  if (botInstance && order.client_chat_id) {
    botInstance.sendMessageToClient(order.client_chat_id, order.order_number, content, admin.username);
  }
  res.json({ ok: true });
});

// ─── Managers (admin only) ────────────────────────────────────────────────
router.get('/admin/managers', auth, async (req, res) => {
  const managers = await query('SELECT id, username, email, role, telegram_id, created_at FROM admins ORDER BY created_at DESC');
  res.json(managers);
});

router.post('/admin/managers', auth, async (req, res) => {
  if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
  const { username, email, password, role, telegram_id } = req.body;
  if (!username || !/^[a-zA-Z0-9_]{3,32}$/.test(username)) return res.status(400).json({ error: 'Логин: 3–32 символа, только буквы/цифры/_' });
  if (!password || password.length < 6) return res.status(400).json({ error: 'Пароль минимум 6 символов' });
  if (email && !validateEmail(email)) return res.status(400).json({ error: 'Некорректный email' });
  const allowedRoles = ['manager', 'superadmin'];
  const existing = await get('SELECT id FROM admins WHERE username = ?', [username]);
  if (existing) return res.status(409).json({ error: 'Пользователь с таким логином уже существует' });
  const hash = await bcrypt.hash(password, 10);
  const result = await run(
    'INSERT INTO admins (username, email, password_hash, role, telegram_id) VALUES (?,?,?,?,?)',
    [username, email || null, hash, allowedRoles.includes(role) ? role : 'manager', telegram_id || null]
  );
  res.json({ id: result.id });
});

router.delete('/admin/managers/:id', auth, async (req, res) => {
  if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
  if (+req.params.id === req.admin.id) return res.status(400).json({ error: 'Cannot delete yourself' });
  await run('DELETE FROM admins WHERE id = ?', [req.params.id]);
  res.json({ ok: true });
});

module.exports = router;
module.exports.setBot = setBot;
