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
    res.json({
      total_orders: total.n,
      new_orders: newO.n,
      active_orders: active.n,
      total_models: totalM.n,
      available_models: availM.n,
      unread_messages: unread?.n || 0,
      by_status: byStatus,
      recent
    });
  } catch (e) { next(e); }
});

// ─── Models (public) ──────────────────────────────────────────────────────────
router.get('/models', async (req, res, next) => {
  try {
    const { category, hair_color, min_height, max_height, available, search } = req.query;
    let sql = 'SELECT * FROM models WHERE 1=1';
    const params = [];
    if (category && ALLOWED_CATEGORIES.includes(category)) { sql += ' AND category = ?'; params.push(category); }
    if (hair_color) { sql += ' AND hair_color = ?'; params.push(hair_color); }
    if (min_height && !isNaN(+min_height)) { sql += ' AND height >= ?'; params.push(+min_height); }
    if (max_height && !isNaN(+max_height)) { sql += ' AND height <= ?'; params.push(+max_height); }
    if (available === '1') { sql += ' AND available = 1'; }
    if (available === '0') { sql += ' AND available = 0'; }
    if (search) { sql += ' AND (name LIKE ? OR bio LIKE ?)'; params.push(`%${search}%`, `%${search}%`); }
    sql += ' ORDER BY available DESC, id DESC';
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
      `SELECT o.order_number, o.status, o.event_type, o.event_date, o.client_name, m.name as model_name
       FROM orders o LEFT JOIN models m ON o.model_id = m.id
       WHERE o.order_number = ?`,
      [req.params.order_number.toUpperCase()]
    );
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    res.json(order);
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
    const { status, search } = req.query;
    let where = '1=1'; const params = [];
    if (status && ALLOWED_STATUSES.includes(status)) { where += ' AND o.status = ?'; params.push(status); }
    if (search) { where += ' AND (o.client_name LIKE ? OR o.order_number LIKE ?)'; params.push(`%${search}%`, `%${search}%`); }
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

// PUT /api/settings — сохраняет настройки бота (принимает объект key:value)
router.put('/settings', auth, async (req, res, next) => {
  const ALLOWED_KEYS = ['greeting','about','contacts_phone','contacts_email','contacts_insta','contacts_addr','pricing','notif_new_order','notif_status','notif_message'];
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

module.exports = router;
module.exports.setBot = setBot;
