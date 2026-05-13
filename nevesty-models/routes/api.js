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

// ─── File upload ────────────────────────────────────────────────────────────
const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    const dir = path.join(__dirname, '../uploads');
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: (req, file, cb) => {
    const ext = path.extname(file.originalname);
    cb(null, `model_${Date.now()}_${Math.random().toString(36).slice(2)}${ext}`);
  }
});
const upload = multer({
  storage,
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    if (file.mimetype.startsWith('image/')) cb(null, true);
    else cb(new Error('Only images allowed'));
  }
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
    if (!client_name || !client_phone || !event_type) {
      return res.status(400).json({ error: 'Заполните обязательные поля' });
    }
    let order_number;
    let attempts = 0;
    do {
      order_number = generateOrderNumber();
      const exists = await get('SELECT id FROM orders WHERE order_number = ?', [order_number]);
      if (!exists) break;
      attempts++;
    } while (attempts < 10);

    const result = await run(
      `INSERT INTO orders (order_number,client_name,client_phone,client_email,client_telegram,client_chat_id,model_id,event_type,event_date,event_duration,location,budget,comments)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      [order_number, client_name, client_phone, client_email || null, client_telegram || null, client_chat_id || null, model_id || null, event_type, event_date || null, event_duration || 4, location || null, budget || null, comments || null]
    );

    // Notify bot
    if (botInstance) {
      botInstance.notifyNewOrder({ id: result.id, order_number, client_name, client_phone, client_email, client_telegram, event_type, event_date, location, budget, comments, model_id });
    }

    res.json({ order_number, id: result.id });
  } catch (e) {
    res.status(500).json({ error: e.message });
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
  const { content } = req.body;
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
  const hash = await bcrypt.hash(password, 10);
  const result = await run('INSERT INTO admins (username, email, password_hash, role, telegram_id) VALUES (?,?,?,?,?)', [username, email, hash, role || 'manager', telegram_id || null]);
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
