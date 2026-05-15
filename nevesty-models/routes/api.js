const express = require('express');
const router = express.Router();
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const multer = require('multer');
const path = require('path');
const fs = require('fs');
let sharp;
try { sharp = require('sharp'); } catch { sharp = null; }
const crypto = require('crypto');
const speakeasy = require('speakeasy');
const QRCode = require('qrcode');
const { query, run, get, generateOrderNumber, getSetting } = require('../database');
const auth = require('../middleware/auth');
const mailer = require('../services/mailer');
const payment = require('../services/payment');
const { cache, TTL_CATALOG } = require('../services/cache');
const { ALLOWED_EVENT_TYPES, ALLOWED_CATEGORIES, VALID_STATUSES } = require('../utils/constants');

// ─── Rate limiters ────────────────────────────────────────────────────────────
let contactRateLimit = (req, res, next) => next(); // fallback: no-op
let strictLimiter    = (req, res, next) => next(); // fallback: no-op
let authLimiter      = (req, res, next) => next(); // fallback: no-op
let aiMatchLimiter   = (req, res, next) => next(); // fallback: no-op
let bookingLimiter   = (req, res, next) => next(); // fallback: no-op — 5/hour for /orders
try {
  const rateLimit = require('express-rate-limit');
  // Contact form: 3 requests per hour per IP
  contactRateLimit = rateLimit({
    windowMs: 60 * 60 * 1000, // 1 hour
    max: 3,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много сообщений. Попробуйте через час.' },
  });
  // Booking/orders limiter: 5 requests per hour per IP
  bookingLimiter = rateLimit({
    windowMs: 60 * 60 * 1000, // 1 hour
    max: 5,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Превышен лимит заявок на бронирование, попробуйте через час' },
  });
  // Strict limit for quick-booking: 10 per hour
  strictLimiter = rateLimit({
    windowMs: 60 * 60 * 1000, // 1 hour
    max: 10,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Превышен лимит заявок, попробуйте через час' },
  });
  // Auth limit: 20 attempts per 15 minutes
  authLimiter = rateLimit({
    windowMs: 15 * 60 * 1000,
    max: 20,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много попыток входа' },
  });
  // AI match: 10 requests per hour per IP (public endpoint using paid API)
  aiMatchLimiter = rateLimit({
    windowMs: 60 * 60 * 1000,
    max: 10,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много запросов. Попробуйте через час.' },
  });
} catch { /* express-rate-limit not available */ }

let botInstance = null;
function setBot(bot) { botInstance = bot; }

// ─── MarkdownV2 escape helper ─────────────────────────────────────────────────
const escMd = s => String(s || '').replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, '\\$&');

// ─── Audit log helper ─────────────────────────────────────────────────────────
async function logAudit(req, action, entity, entityId, details) {
  try {
    const user = req.admin?.username || 'unknown';
    await run('INSERT INTO audit_log (admin_username, action, entity, entity_id, details, ip) VALUES (?,?,?,?,?,?)',
      [user, action, entity, entityId || null, details ? JSON.stringify(details) : null, req.ip || '']);
  } catch {} // silently ignore audit failures
}

// ─── Validation helpers ───────────────────────────────────────────────────────
// ALLOWED_EVENT_TYPES, ALLOWED_CATEGORIES, VALID_STATUSES imported from utils/constants
const ALLOWED_IMG_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp'];
const ALLOWED_STATUSES = VALID_STATUSES; // alias for backwards compat within this file

function sanitize(s, max = 500) {
  if (typeof s !== 'string') return null;
  return s.trim().slice(0, max) || null;
}
function validatePhone(p) { return typeof p === 'string' && /^[\d\s\+\(\)\-]{7,20}$/.test(p.trim()); }
function validateEmail(e) { return !e || /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test((e || '').trim()); }
function validateDate(d) { return !d || (/^\d{4}-\d{2}-\d{2}$/.test(d) && !isNaN(Date.parse(d))); }

// ─── Input sanitization middleware ───────────────────────────────────────────
// Strip null bytes from all string body inputs (deep: handles nested objects & arrays)
function sanitizeInput(req, res, next) {
  function clean(val) {
    if (typeof val === 'string') {
      // Remove null bytes, limit length
      return val.replace(/\0/g, '').trim().slice(0, 10000);
    }
    if (Array.isArray(val)) return val.map(clean);
    if (val && typeof val === 'object') {
      return Object.fromEntries(Object.entries(val).map(([k, v]) => [k, clean(v)]));
    }
    return val;
  }
  if (req.body) req.body = clean(req.body);
  next();
}
router.use(sanitizeInput);

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

// ─── WebP conversion ─────────────────────────────────────────────────────────
// eslint-disable-next-line no-unused-vars
async function convertToWebP(filePath) {
  if (!sharp) return filePath; // sharp not available, skip
  const ext = path.extname(filePath).toLowerCase();
  if (ext === '.webp') return filePath; // already WebP
  const webpPath = filePath.replace(new RegExp(ext.replace('.', '\\.') + '$'), '.webp');
  try {
    const sizeBefore = fs.existsSync(filePath) ? fs.statSync(filePath).size : 0;
    await sharp(filePath)
      .resize(1200, 1600, { fit: 'inside', withoutEnlargement: true })
      .webp({ quality: 85, effort: 4 })
      .toFile(webpPath);
    const sizeAfter = fs.existsSync(webpPath) ? fs.statSync(webpPath).size : 0;
    console.log('[sharp] converted', filePath, '→', webpPath, sizeBefore, '→', sizeAfter, 'bytes');
    fs.unlink(filePath, () => {}); // delete original after successful conversion
    return webpPath;
  } catch (e) {
    console.error('[WebP] conversion error:', e.message);
    return filePath; // return original if conversion fails
  }
}

// ─── WebP conversion with thumbnail generation ────────────────────────────────
// Returns { full: webpPath, thumb: thumbPath } — both are absolute filesystem paths.
// On failure falls back gracefully: full = original, thumb = null.
async function convertToWebPWithThumb(filePath) {
  if (!sharp) return { full: filePath, thumb: null };
  const ext = path.extname(filePath).toLowerCase();
  const baseNoExt = path.basename(filePath, ext);
  const dir = path.dirname(filePath);

  // Full-size WebP path (same directory, .webp extension)
  const webpPath = ext === '.webp' ? filePath : path.join(dir, baseNoExt + '.webp');

  // Thumbnail: <uploads_dir>/thumbs/<basename>.webp
  const thumbsDir = path.join(dir, 'thumbs');
  if (!fs.existsSync(thumbsDir)) fs.mkdirSync(thumbsDir, { recursive: true });
  const thumbPath = path.join(thumbsDir, baseNoExt + '.webp');

  let fullResult = filePath;
  let thumbResult = null;

  try {
    const sizeBefore = fs.existsSync(filePath) ? fs.statSync(filePath).size : 0;

    // Generate full-size WebP
    if (ext !== '.webp') {
      await sharp(filePath)
        .resize(1200, 1600, { fit: 'inside', withoutEnlargement: true })
        .webp({ quality: 85, effort: 4 })
        .toFile(webpPath);
      const sizeAfter = fs.existsSync(webpPath) ? fs.statSync(webpPath).size : 0;
      console.log('[sharp] converted', filePath, '→', webpPath, sizeBefore, '→', sizeAfter, 'bytes');
      fs.unlink(filePath, () => {}); // delete original non-WebP file
      fullResult = webpPath;
    }
  } catch (e) {
    console.error('[WebP] full conversion error:', e.message);
    // fullResult stays as original filePath — graceful fallback
  }

  try {
    // Generate thumbnail (400x600 max, quality 80)
    const thumbSrc = fullResult !== filePath ? fullResult : filePath;
    await sharp(thumbSrc)
      .resize(400, 600, { fit: 'inside', withoutEnlargement: true })
      .webp({ quality: 80, effort: 4 })
      .toFile(thumbPath);
    const thumbSize = fs.existsSync(thumbPath) ? fs.statSync(thumbPath).size : 0;
    console.log('[sharp] thumbnail', thumbPath, thumbSize, 'bytes');
    thumbResult = thumbPath;
  } catch (e) {
    console.error('[WebP] thumbnail error:', e.message);
    // thumbResult stays null — graceful fallback, upload still succeeds
  }

  return { full: fullResult, thumb: thumbResult };
}

// ─── Derive thumb URL from a stored photo URL ─────────────────────────────────
// e.g. /uploads/model_123.webp → /uploads/thumbs/model_123.webp
function deriveThumbUrl(photoUrl) {
  if (!photoUrl || typeof photoUrl !== 'string') return null;
  const basename = path.basename(photoUrl);
  const dir = path.dirname(photoUrl); // e.g. /uploads
  return `${dir}/thumbs/${basename}`;
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

// ─── CSRF token ───────────────────────────────────────────────────────────────
router.get('/csrf-token', (req, res) => {
  const ip = req.ip || '';
  const { generateToken } = require('../middleware/csrf');
  res.json({ token: generateToken(ip) });
});

// ─── Auth ─────────────────────────────────────────────────────────────────────

/** Helper: issue full JWT + refresh token pair for an admin */
async function issueTokenPair(admin) {
  const token = jwt.sign(
    { id: admin.id, username: admin.username, role: admin.role },
    process.env.JWT_SECRET || 'secret',
    { expiresIn: '15m' }
  );
  const refreshTokenRaw = crypto.randomBytes(48).toString('hex');
  const refreshHash = crypto.createHash('sha256').update(refreshTokenRaw).digest('hex');
  await run(
    "INSERT INTO refresh_tokens (token_hash, admin_id, expires_at) VALUES (?, ?, datetime('now', '+7 days'))",
    [refreshHash, admin.id]
  );
  await run("DELETE FROM refresh_tokens WHERE admin_id=? AND (expires_at < CURRENT_TIMESTAMP OR revoked=1)", [admin.id]).catch(() => {});
  return { token, refresh_token: refreshTokenRaw };
}

router.post('/admin/login', authLimiter, async (req, res, next) => {
  try {
    const { username, password } = req.body;
    if (!username || !password) return res.status(400).json({ error: 'Укажите логин и пароль' });
    const admin = await get('SELECT * FROM admins WHERE username = ?', [username]);
    if (!admin) return res.status(401).json({ error: 'Неверный логин или пароль' });
    const ok = await bcrypt.compare(password, admin.password_hash);
    if (!ok) return res.status(401).json({ error: 'Неверный логин или пароль' });

    // If 2FA is enabled, issue a short-lived temp token instead of full JWT
    if (admin.totp_enabled) {
      const tempTokenRaw = crypto.randomBytes(32).toString('hex');
      const tempHash = crypto.createHash('sha256').update(tempTokenRaw).digest('hex');
      // Clean up stale temp tokens for this admin
      await run("DELETE FROM totp_temp_tokens WHERE admin_id=? AND expires_at < CURRENT_TIMESTAMP", [admin.id]).catch(() => {});
      await run(
        "INSERT INTO totp_temp_tokens (token_hash, admin_id, expires_at) VALUES (?, ?, datetime('now', '+5 minutes'))",
        [tempHash, admin.id]
      );
      return res.json({ requires_2fa: true, temp_token: tempTokenRaw });
    }

    // No 2FA — issue full token pair
    const { token, refresh_token } = await issueTokenPair(admin);
    res.json({ token, refresh_token, admin: { id: admin.id, username: admin.username, role: admin.role } });
  } catch (e) { next(e); }
});

// ─── Auth: verify TOTP ────────────────────────────────────────────────────────
router.post('/auth/verify-totp', authLimiter, async (req, res, next) => {
  try {
    const { temp_token, totp_code } = req.body;
    if (!temp_token || !totp_code) return res.status(400).json({ error: 'temp_token and totp_code required' });

    const tempHash = crypto.createHash('sha256').update(String(temp_token)).digest('hex');
    const stored = await get(
      'SELECT * FROM totp_temp_tokens WHERE token_hash=? AND expires_at > CURRENT_TIMESTAMP',
      [tempHash]
    );
    if (!stored) return res.status(401).json({ error: 'Неверный или истёкший токен' });

    // Check attempt limit (3 max) — must be checked before any further processing
    if (stored.attempts >= 3) {
      await run('DELETE FROM totp_temp_tokens WHERE id=?', [stored.id]);
      return res.status(401).json({ error: 'Превышено количество попыток. Войдите заново.' });
    }

    const admin = await get('SELECT * FROM admins WHERE id=?', [stored.admin_id]);
    if (!admin || !admin.totp_secret) {
      await run('DELETE FROM totp_temp_tokens WHERE id=?', [stored.id]);
      return res.status(401).json({ error: 'Ошибка аутентификации' });
    }

    const valid = speakeasy.totp.verify({
      secret: admin.totp_secret,
      encoding: 'base32',
      token: String(totp_code).replace(/\s/g, ''),
      window: 1,
    });

    if (!valid) {
      // Atomically increment then re-read; delete token immediately when limit reached
      await run('UPDATE totp_temp_tokens SET attempts=attempts+1 WHERE id=?', [stored.id]);
      const updated = await get('SELECT attempts FROM totp_temp_tokens WHERE id=?', [stored.id]).catch(() => null);
      if (!updated || updated.attempts >= 3) {
        await run('DELETE FROM totp_temp_tokens WHERE id=?', [stored.id]).catch(() => {});
        return res.status(401).json({ error: 'Превышено количество попыток. Войдите заново.' });
      }
      return res.status(401).json({ error: 'Неверный код. Проверьте время и попробуйте снова.' });
    }

    // Valid — delete temp token and issue full JWT pair
    await run('DELETE FROM totp_temp_tokens WHERE id=?', [stored.id]);
    const { token, refresh_token } = await issueTokenPair(admin);
    res.json({ token, refresh_token, admin: { id: admin.id, username: admin.username, role: admin.role } });
  } catch (e) { next(e); }
});

// ─── Auth: refresh token ─────────────────────────────────────────────────────
router.post('/auth/refresh', authLimiter, async (req, res, next) => {
  try {
    const { refresh_token } = req.body;
    if (!refresh_token) return res.status(400).json({ error: 'refresh_token required' });
    const tokenHash = crypto.createHash('sha256').update(refresh_token).digest('hex');
    const stored = await get(
      'SELECT * FROM refresh_tokens WHERE token_hash=? AND revoked=0 AND expires_at > CURRENT_TIMESTAMP',
      [tokenHash]
    );
    if (!stored) return res.status(401).json({ error: 'Invalid or expired refresh token' });
    const admin = await get('SELECT id, username, role FROM admins WHERE id=?', [stored.admin_id]);
    if (!admin) return res.status(401).json({ error: 'Admin not found' });
    // Rotate: revoke old token, issue new pair
    await run('UPDATE refresh_tokens SET revoked=1 WHERE id=?', [stored.id]);
    const newRefresh = crypto.randomBytes(48).toString('hex');
    const newHash = crypto.createHash('sha256').update(newRefresh).digest('hex');
    await run(
      "INSERT INTO refresh_tokens (token_hash, admin_id, expires_at) VALUES (?, ?, datetime('now', '+7 days'))",
      [newHash, admin.id]
    );
    const token = jwt.sign(
      { id: admin.id, username: admin.username, role: admin.role },
      process.env.JWT_SECRET || 'secret',
      { expiresIn: '15m' }
    );
    res.json({ token, refresh_token: newRefresh });
  } catch (e) { next(e); }
});

// ─── Auth: revoke (logout) ───────────────────────────────────────────────────
router.post('/auth/logout', async (req, res, next) => {
  try {
    const { refresh_token } = req.body;
    if (refresh_token) {
      const tokenHash = crypto.createHash('sha256').update(refresh_token).digest('hex');
      await run('UPDATE refresh_tokens SET revoked=1 WHERE token_hash=?', [tokenHash]);
    }
    res.json({ ok: true });
  } catch (e) { next(e); }
});

router.get('/admin/me', auth, async (req, res, next) => {
  try {
    const admin = await get('SELECT id, username, email, role, telegram_id, totp_enabled FROM admins WHERE id = ?', [req.admin.id]);
    res.json(admin);
  } catch (e) { next(e); }
});

// ─── TOTP 2FA management ──────────────────────────────────────────────────────

// GET /admin/totp/setup — generate a new TOTP secret (do NOT save yet)
router.get('/admin/totp/setup', auth, async (req, res, next) => {
  try {
    const secret = speakeasy.generateSecret({
      name: `Nevesty Models (${req.admin.username})`,
      length: 20,
    });
    const qr_url = await QRCode.toDataURL(secret.otpauth_url);
    res.json({
      secret: secret.base32,
      qr_url,
      manual_key: secret.base32,
      otpauth_url: secret.otpauth_url,
    });
  } catch (e) { next(e); }
});

// POST /admin/totp/enable — validate code against provided secret, then save
router.post('/admin/totp/enable', auth, async (req, res, next) => {
  try {
    const { secret, totp_code } = req.body;
    if (!secret || !totp_code) return res.status(400).json({ error: 'secret and totp_code required' });

    const valid = speakeasy.totp.verify({
      secret: String(secret),
      encoding: 'base32',
      token: String(totp_code).replace(/\s/g, ''),
      window: 1,
    });
    if (!valid) return res.status(400).json({ error: 'Неверный код. Проверьте приложение и попробуйте снова.' });

    await run('UPDATE admins SET totp_secret=?, totp_enabled=1 WHERE id=?', [String(secret), req.admin.id]);
    await logAudit(req, 'totp_enable', 'admin', req.admin.id, { username: req.admin.username });
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// DELETE /admin/totp/disable — validate current TOTP then disable
router.delete('/admin/totp/disable', auth, async (req, res, next) => {
  try {
    const { totp_code } = req.body;
    if (!totp_code) return res.status(400).json({ error: 'totp_code required' });

    const admin = await get('SELECT totp_secret, totp_enabled FROM admins WHERE id=?', [req.admin.id]);
    if (!admin || !admin.totp_enabled || !admin.totp_secret) {
      return res.status(400).json({ error: '2FA не включена' });
    }

    const valid = speakeasy.totp.verify({
      secret: admin.totp_secret,
      encoding: 'base32',
      token: String(totp_code).replace(/\s/g, ''),
      window: 1,
    });
    if (!valid) return res.status(400).json({ error: 'Неверный код подтверждения' });

    await run('UPDATE admins SET totp_secret=NULL, totp_enabled=0 WHERE id=?', [req.admin.id]);
    await logAudit(req, 'totp_disable', 'admin', req.admin.id, { username: req.admin.username });
    res.json({ ok: true });
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

// ─── Agent logs feed — auth-protected (was public, now requires JWT) ──────────
router.get('/agent-logs', auth, async (req, res, next) => {
  try {
    const limit = Math.min(100, parseInt(req.query.limit) || 50);
    const logs = await query(
      'SELECT * FROM agent_logs ORDER BY created_at DESC LIMIT ?',
      [limit]
    );
    res.json(logs);
  } catch (e) {
    next(e);
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
    const [total, newO, active, totalM, availM, convRow, avgBudgetRow, trend7d, trendPrev7d, avgCycleRow] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE status = 'new'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('reviewing','confirmed','in_progress')"),
      get('SELECT COUNT(*) as n FROM models'),
      get("SELECT COUNT(*) as n FROM models WHERE available = 1"),
      // conversion: (confirmed+in_progress+completed) / total
      get(`SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status IN ('confirmed','in_progress','completed') THEN 1 ELSE 0 END) as converted
           FROM orders`),
      // avg budget of confirmed/completed orders (budget stored as text, try to parse)
      get(`SELECT ROUND(AVG(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','.') AS REAL)), 0) as avg_budget
           FROM orders
           WHERE status IN ('confirmed','completed')
           AND budget IS NOT NULL AND budget != ''
           AND CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','.') AS REAL) > 0`),
      // orders last 7 days
      get(`SELECT COUNT(*) as n FROM orders WHERE created_at >= date('now', '-6 days')`),
      // orders previous 7 days (7-14 days ago)
      get(`SELECT COUNT(*) as n FROM orders WHERE created_at >= date('now', '-13 days') AND created_at < date('now', '-6 days')`),
      // avg days from new to completed
      get(`SELECT ROUND(AVG(CAST(julianday(updated_at) - julianday(created_at) AS REAL)), 1) as avg_days
           FROM orders WHERE status = 'completed'`),
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
    // Top 5 models by order count
    const topModels = await query(
      `SELECT m.id, m.name, COUNT(o.id) as order_count
       FROM models m
       LEFT JOIN orders o ON m.id = o.model_id
       GROUP BY m.id
       ORDER BY order_count DESC
       LIMIT 5`
    );
    // orders_by_status object
    const ordersByStatus = {};
    byStatus.forEach(r => { ordersByStatus[r.status] = r.count; });

    // Compute derived metrics
    const totalOrders = convRow.total || 0;
    const converted   = convRow.converted || 0;
    const convRate    = totalOrders > 0 ? Math.round((converted / totalOrders) * 1000) / 10 : 0;

    const cur7  = trend7d.n || 0;
    const prev7 = trendPrev7d.n || 0;
    const trendDir = cur7 > prev7 ? 'up' : cur7 < prev7 ? 'down' : 'flat';
    const trendDelta = cur7 - prev7;

    res.json({
      total_orders: total.n,
      new_orders: newO.n,
      active_orders: active.n,
      total_models: totalM.n,
      available_models: availM.n,
      unread_messages: unread?.n || 0,
      by_status: byStatus,
      recent,
      daily_7d: daily7dFull,
      // Enhanced metrics
      conversion_rate: convRate,
      avg_order_budget: avgBudgetRow?.avg_budget || null,
      top_models: topModels,
      orders_by_status: ordersByStatus,
      orders_trend: {
        direction: trendDir,
        delta: trendDelta,
        current_7d: cur7,
        previous_7d: prev7,
      },
      avg_cycle_days: avgCycleRow?.avg_days || null,
    });
  } catch (e) { next(e); }
});

// ─── Admin stats (extended: today, completed, new_clients, pending_reviews) ───
router.get('/admin/stats/extended2', auth, async (req, res, next) => {
  try {
    const [todayOrders, activeOrders, completedOrders, newClients, pendingReviews, revenueRow, repeatRow, avgDealRow] = await Promise.all([
      get("SELECT COUNT(*) as n FROM orders WHERE date(created_at) = date('now')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('reviewing','confirmed','in_progress')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status = 'completed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= date('now', '-30 days')"),
      get("SELECT COUNT(*) as n FROM reviews WHERE approved = 0"),
      get(`SELECT COALESCE(SUM(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER)), 0) as total
          FROM orders WHERE status IN ('confirmed','in_progress','completed')
          AND budget IS NOT NULL AND budget != ''
          AND created_at >= date('now', '-30 days')`),
      get(`SELECT COUNT(*) as n FROM (
            SELECT client_phone FROM orders WHERE client_phone IS NOT NULL
            GROUP BY client_phone HAVING COUNT(*) >= 2
          )`),
      get(`SELECT ROUND(AVG(CAST(julianday(updated_at) - julianday(created_at) AS REAL)), 1) as days
          FROM orders WHERE status='completed'`),
    ]);
    res.json({
      today_orders: todayOrders.n,
      active_orders: activeOrders.n,
      completed_orders: completedOrders.n,
      new_clients_30d: newClients.n,
      pending_reviews: pendingReviews.n,
      revenue_30d: revenueRow.total,
      repeat_clients: repeatRow.n,
      avg_deal_days: avgDealRow.days || 0,
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
       WHERE created_at >= date('now', ? || ' days')
       GROUP BY date(created_at)
       ORDER BY day ASC`,
      [`-${days - 1}`]
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

// ─── Models (public) — with 2-minute cache keyed on query params ──────────────
router.get('/models', async (req, res, next) => {
  try {
    // Build a stable cache key from sorted query params
    const qHash = crypto.createHash('md5')
      .update(JSON.stringify(Object.fromEntries(
        Object.entries(req.query).sort(([a], [b]) => a.localeCompare(b))
      )))
      .digest('hex');
    const cacheKey = `catalog:${qHash}`;
    const cached = cache.get(cacheKey, TTL_CATALOG);
    if (cached !== undefined) {
      res.setHeader('X-Cache', 'HIT');
      return res.json(cached);
    }

    const { category, hair_color, min_height, max_height, min_age, max_age, city, available, search,
            height_min, height_max, age_min, age_max } = req.query;
    // Support both naming conventions: min_height/max_height and height_min/height_max
    const _minH = min_height || height_min;
    const _maxH = max_height || height_max;
    const _minA = min_age    || age_min;
    const _maxA = max_age    || age_max;
    let sql = "SELECT id, name, age, height, city, category, available, featured, photo_main, bio, instagram, hair_color, eye_color, weight, bust, waist, hips, shoe_size, photos, (SELECT COUNT(*) FROM orders WHERE model_id=models.id AND status IN ('completed','confirmed')) as order_count FROM models WHERE archived=0";
    const params = [];
    if (category && ALLOWED_CATEGORIES.includes(category)) { sql += ' AND category = ?'; params.push(category); }
    if (hair_color) { sql += ' AND hair_color = ?'; params.push(hair_color); }
    if (_minH && !isNaN(+_minH)) { sql += ' AND height >= ?'; params.push(+_minH); }
    if (_maxH && !isNaN(+_maxH)) { sql += ' AND height <= ?'; params.push(+_maxH); }
    if (_minA && !isNaN(+_minA)) { sql += ' AND age >= ?'; params.push(+_minA); }
    if (_maxA && !isNaN(+_maxA)) { sql += ' AND age <= ?'; params.push(+_maxA); }
    if (city) { sql += ' AND city = ?'; params.push(city); }
    if (available === '1') { sql += ' AND available = 1'; }
    if (available === '0') { sql += ' AND available = 0'; }
    if (search) { sql += ' AND (name LIKE ? OR bio LIKE ?)'; params.push(`%${search}%`, `%${search}%`); }
    sql += ' ORDER BY available DESC, id DESC LIMIT 200';
    const models = await query(sql, params);
    const result = models.map(m => {
      const photos = JSON.parse(m.photos || '[]');
      return {
        ...m,
        photos,
        photo_thumb: deriveThumbUrl(m.photo_main),
        photos_thumbs: photos.map(deriveThumbUrl),
      };
    });
    cache.set(cacheKey, result, TTL_CATALOG);
    res.setHeader('X-Cache', 'MISS');
    res.json(result);
  } catch (e) { next(e); }
});

// ─── Models search (public, extended filters) ─────────────────────────────────
router.get('/models/search', async (req, res, next) => {
  try {
    const { min_height, max_height, min_age, max_age, category, city, page = 0, limit = 12 } = req.query;
    const where = ['1=1'];
    const params = [];
    if (min_height && !isNaN(+min_height)) { where.push('height >= ?'); params.push(parseInt(min_height)); }
    if (max_height && !isNaN(+max_height)) { where.push('height <= ?'); params.push(parseInt(max_height)); }
    if (min_age && !isNaN(+min_age)) { where.push('age >= ?'); params.push(parseInt(min_age)); }
    if (max_age && !isNaN(+max_age)) { where.push('age <= ?'); params.push(parseInt(max_age)); }
    if (category && ALLOWED_CATEGORIES.includes(category)) { where.push('category = ?'); params.push(category); }
    if (city) { where.push('city = ?'); params.push(city); }
    const whereSql = where.join(' AND ');
    const limitN = Math.min(50, Math.max(1, parseInt(limit) || 12));
    const offset = Math.max(0, parseInt(page) || 0) * limitN;
    const countParams = [...params];
    params.push(limitN, offset);
    const [models, totalRow] = await Promise.all([
      query(`SELECT id, name, age, height, city, category, available, photo_main, bio, hair_color FROM models WHERE ${whereSql} ORDER BY available DESC, id DESC LIMIT ? OFFSET ?`, params),
      get(`SELECT COUNT(*) as cnt FROM models WHERE ${whereSql}`, countParams)
    ]);
    res.json({ models, total: totalRow?.cnt || 0, page: parseInt(page) || 0 });
  } catch (e) { next(e); }
});

// ─── Related models (public) ──────────────────────────────────────────────────
router.get('/models/related', async (req, res) => {
  try {
    const { category, city, limit = 4, exclude } = req.query;
    const where = ['archived=0', 'available=1'];
    const params = [];
    if (exclude) { where.push('id != ?'); params.push(parseInt(exclude)); }
    if (category) { where.push('category = ?'); params.push(category); }
    else if (city) { where.push('city = ?'); params.push(city); }
    params.push(parseInt(limit) || 4);

    const models = await query(
      `SELECT id, name, photos, photo_main, height, category, city FROM models WHERE ${where.join(' AND ')} ORDER BY featured DESC, order_count DESC LIMIT ?`,
      params
    );
    res.json({ models });
  } catch (e) { res.status(500).json({ error: 'DB error' }); }
});

router.get('/models/:id', async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT * FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    const photos = JSON.parse(m.photos || '[]');
    // Include upcoming busy dates for public display
    const busyDates = await query(
      `SELECT busy_date FROM model_busy_dates WHERE model_id=? AND busy_date >= date('now') ORDER BY busy_date`,
      [id]
    ).catch(() => []);
    res.json({
      ...m,
      photos,
      // Thumbnail paths derived from full paths — available for list views
      photo_thumb: deriveThumbUrl(m.photo_main),
      photos_thumbs: photos.map(deriveThumbUrl),
      busy_dates: busyDates.map(r => r.busy_date),
    });
    // Increment view count asynchronously after response is sent
    run('UPDATE models SET view_count = COALESCE(view_count, 0) + 1 WHERE id=?', [id]).catch(() => {});
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
    cache.delByPrefix('catalog:'); // invalidate catalog cache
    res.json({ ok: true, available: val });
  } catch (e) { next(e); }
});

// ─── Models admin list with filters/pagination ────────────────────────────────
router.get('/admin/models', auth, async (req, res, next) => {
  try {
    const { page = 0, sort = 'name', limit = 15, archived, search } = req.query;
    const offset = parseInt(page) * parseInt(limit);

    const sortMap = {
      name: 'name ASC',
      orders: 'order_count DESC',
      order_count: 'order_count DESC',
      reviews_count: 'reviews_count DESC',
      avg_rating: 'avg_rating DESC',
      views: 'view_count DESC',
      created: 'id DESC'
    };
    const orderBy = sortMap[sort] || 'name ASC';

    const where = [];
    const params = [];
    if (archived !== undefined) { where.push('archived=?'); params.push(parseInt(archived)); }
    if (search) { where.push('name LIKE ?'); params.push(`%${search}%`); }
    const whereStr = where.length ? `WHERE ${where.join(' AND ')}` : '';

    const total = (await get(`SELECT COUNT(*) as cnt FROM models ${whereStr}`, params))?.cnt || 0;
    const models = await query(
      `SELECT *,
         (SELECT COUNT(*) FROM orders WHERE model_id=models.id) as order_count,
         (SELECT COUNT(*) FROM reviews WHERE model_id=models.id AND approved=1) as reviews_count,
         (SELECT ROUND(AVG(rating),1) FROM reviews WHERE model_id=models.id AND approved=1) as avg_rating
       FROM models ${whereStr} ORDER BY ${orderBy} LIMIT ? OFFSET ?`,
      [...params, parseInt(limit), offset]
    );

    res.json({ models, total });
  } catch (e) {
    res.status(500).json({ error: 'DB error' });
  }
});

// ─── Create model (admin, JSON body — no photo upload) ────────────────────────
router.post('/admin/models/json', auth, async (req, res, next) => {
  try {
    const { name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, instagram, phone, category, city, featured, available } = req.body;
    if (!name) return res.status(400).json({ error: 'Name required' });
    const result = await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,instagram,phone,category,city,featured,available,archived) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)`,
      [name, age||null, height||null, weight||null, bust||null, waist||null, hips||null,
       shoe_size||null, hair_color||null, eye_color||null, bio||null, instagram||null,
       phone||null, category||null, city||null, featured?1:0, available?1:0]
    );
    cache.delByPrefix('catalog:'); // invalidate catalog cache
    res.json({ id: result.id, success: true });
  } catch (e) {
    res.status(500).json({ error: 'DB error' });
  }
});

// ─── Full update model via JSON body (admin PUT, no file upload) ──────────────
router.put('/admin/models/:id/json', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const { name, age, height, bio, instagram, phone, category, city, featured, available } = req.body;
    await run(
      `UPDATE models SET name=?,age=?,height=?,bio=?,instagram=?,phone=?,category=?,city=?,featured=?,available=? WHERE id=?`,
      [name, age||null, height||null, bio||null, instagram||null, phone||null,
       category||null, city||null, featured?1:0, available?1:0, id]
    );
    cache.delByPrefix('catalog:'); // invalidate catalog cache
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: 'DB error' });
  }
});

// ─── Partial update model (admin PATCH) ───────────────────────────────────────
router.patch('/admin/models/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const allowed = ['name','age','height','weight','bio','instagram','phone','category','city','featured','available','archived'];
    const updates = [];
    const params = [];
    for (const [k, v] of Object.entries(req.body)) {
      if (allowed.includes(k)) { updates.push(`${k}=?`); params.push(v); }
    }
    if (!updates.length) return res.status(400).json({ error: 'Nothing to update' });
    params.push(id);
    await run(`UPDATE models SET ${updates.join(',')} WHERE id=?`, params);
    cache.delByPrefix('catalog:'); // invalidate catalog cache
    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: 'DB error' });
  }
});

// PATCH /admin/models/:id/archive — soft-delete: set archived=1, available=0
router.patch('/admin/models/:id/archive', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT id, name FROM models WHERE id=?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    await run('UPDATE models SET archived=1, available=0 WHERE id=?', [id]);
    cache.delByPrefix('catalog:');
    await logAudit(req, 'archive', 'model', id, { name: m.name });
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// PATCH /admin/models/:id/restore — restore from archive: set archived=0
router.patch('/admin/models/:id/restore', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT id, name FROM models WHERE id=?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    await run('UPDATE models SET archived=0 WHERE id=?', [id]);
    cache.delByPrefix('catalog:');
    await logAudit(req, 'restore', 'model', id, { name: m.name });
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Models (admin CRUD) ──────────────────────────────────────────────────────
router.post('/admin/models', auth, upload.fields([{ name: 'photo_main', maxCount: 1 }, { name: 'photos', maxCount: 10 }]), async (req, res, next) => {
  try {
    const { name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, bio, instagram, category, available } = req.body;
    if (!name) return res.status(400).json({ error: 'Укажите имя модели' });
    if (category && !ALLOWED_CATEGORIES.includes(category)) return res.status(400).json({ error: 'Недопустимая категория' });
    let photo_main = null;
    if (req.files?.photo_main?.[0]) {
      const { full } = await convertToWebPWithThumb(req.files.photo_main[0].path);
      photo_main = `/uploads/${path.basename(full)}`;
    }
    const photoFiles = req.files?.photos || [];
    const convertedPhotos = await Promise.all(photoFiles.map(f => convertToWebPWithThumb(f.path)));
    const photos = convertedPhotos.map(({ full }) => `/uploads/${path.basename(full)}`);
    const result = await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,photo_main,photos,instagram,category,available)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      [sanitize(name, 100), +age || null, +height || null, +weight || null, +bust || null, +waist || null, +hips || null, sanitize(shoe_size, 10), sanitize(hair_color, 50), sanitize(eye_color, 50), sanitize(bio, 2000), photo_main, JSON.stringify(photos), sanitize(instagram, 100), category || 'fashion', available === '1' ? 1 : 0]
    );
    cache.delByPrefix('catalog:'); // invalidate catalog cache
    await logAudit(req, 'create', 'model', result.id, { name: sanitize(name, 100) });
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
      // Also delete old thumbnail if it exists
      if (existing.photo_main) deleteFile(deriveThumbUrl(existing.photo_main));
      const { full } = await convertToWebPWithThumb(req.files.photo_main[0].path);
      photo_main = `/uploads/${path.basename(full)}`;
    }
    let photos = JSON.parse(existing.photos || '[]');
    if (req.files?.photos?.length) {
      const convertedPhotos = await Promise.all(req.files.photos.map(f => convertToWebPWithThumb(f.path)));
      photos = [...photos, ...convertedPhotos.map(({ full }) => `/uploads/${path.basename(full)}`)];
    }
    await run(
      `UPDATE models SET name=?,age=?,height=?,weight=?,bust=?,waist=?,hips=?,shoe_size=?,hair_color=?,eye_color=?,bio=?,photo_main=?,photos=?,instagram=?,category=?,available=? WHERE id=?`,
      [sanitize(name, 100), +age || null, +height || null, +weight || null, +bust || null, +waist || null, +hips || null, sanitize(shoe_size, 10), sanitize(hair_color, 50), sanitize(eye_color, 50), sanitize(bio, 2000), photo_main, JSON.stringify(photos), sanitize(instagram, 100), category || existing.category, available === '1' ? 1 : 0, id]
    );
    cache.delByPrefix('catalog:'); // invalidate catalog cache
    res.json({ ok: true });
  } catch (e) { next(e); }
});

router.delete('/admin/models/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT * FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    // Delete all photos and their thumbnails from disk
    deleteFile(m.photo_main);
    deleteFile(deriveThumbUrl(m.photo_main));
    const photos = JSON.parse(m.photos || '[]');
    photos.forEach(p => { deleteFile(p); deleteFile(deriveThumbUrl(p)); });
    await run('DELETE FROM models WHERE id = ?', [id]);
    cache.delByPrefix('catalog:'); // invalidate catalog cache
    await logAudit(req, 'delete', 'model', id, { name: m.name });
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Bulk set featured (admin) ───────────────────────────────────────────────
router.post('/admin/models/bulk-featured', auth, async (req, res, next) => {
  try {
    const { model_ids, featured } = req.body;
    if (!Array.isArray(model_ids) || !model_ids.length) return res.status(400).json({ error: 'Не указаны модели' });
    const validIds = model_ids.map(Number).filter(n => n > 0);
    if (!validIds.length) return res.status(400).json({ error: 'Некорректные ID моделей' });
    const featuredVal = featured ? 1 : 0;
    await run(`UPDATE models SET featured=? WHERE id IN (${validIds.map(() => '?').join(',')})`, [featuredVal, ...validIds]);
    cache.delByPrefix('catalog:');
    res.json({ ok: true, affected: validIds.length });
  } catch (e) { next(e); }
});

// ─── Duplicate model (admin) ─────────────────────────────────────────────────
router.post('/admin/models/:id/duplicate', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT * FROM models WHERE id = ?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });
    const result = await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,instagram,phone,category,city,featured,available,archived,photo_main,photos)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)`,
      [
        (m.name || '') + ' (копия)', m.age, m.height, m.weight, m.bust, m.waist, m.hips,
        m.shoe_size, m.hair_color, m.eye_color, m.bio, m.instagram, m.phone,
        m.category, m.city, m.featured || 0, 0, m.photo_main, m.photos || '[]'
      ]
    );
    cache.delByPrefix('catalog:');
    res.json({ id: result.id, ok: true });
  } catch (e) { next(e); }
});

// ─── Archived models list (admin) ────────────────────────────────────────────
router.get('/admin/models/archived', auth, async (req, res, next) => {
  try {
    const page = Math.max(0, parseInt(req.query.page) || 0);
    const limit = Math.min(50, Math.max(1, parseInt(req.query.limit) || 15));
    const offset = page * limit;
    const total = (await get('SELECT COUNT(*) as cnt FROM models WHERE archived=1'))?.cnt || 0;
    const models = await query(
      `SELECT *, (SELECT COUNT(*) FROM orders WHERE model_id=models.id) as order_count
       FROM models WHERE archived=1 ORDER BY id DESC LIMIT ? OFFSET ?`,
      [limit, offset]
    );
    res.json({ models, total, page });
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
    // Also delete associated thumbnail if it exists
    deleteFile(deriveThumbUrl(photo));
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Model busy dates (calendar) ──────────────────────────────────────────────
router.get('/admin/models/:id/busy-dates', auth, async (req, res, next) => {
  try {
    const dates = await query(
      'SELECT * FROM model_busy_dates WHERE model_id=? ORDER BY busy_date',
      [req.params.id]
    );
    res.json(dates);
  } catch (e) { next(e); }
});

router.post('/admin/models/:id/busy-dates', auth, async (req, res, next) => {
  try {
    const { busy_date, reason } = req.body;
    if (!busy_date || !/^\d{4}-\d{2}-\d{2}$/.test(busy_date)) {
      return res.status(400).json({ error: 'busy_date required (YYYY-MM-DD)' });
    }
    await run(
      'INSERT OR IGNORE INTO model_busy_dates (model_id, busy_date, reason) VALUES (?,?,?)',
      [req.params.id, busy_date, reason || null]
    );
    res.json({ ok: true });
  } catch (e) { next(e); }
});

router.delete('/admin/models/:id/busy-dates/:date', auth, async (req, res, next) => {
  try {
    await run(
      'DELETE FROM model_busy_dates WHERE model_id=? AND busy_date=?',
      [req.params.id, req.params.date]
    );
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// POST /admin/models/:id/generate-description — AI-powered bio generator
router.post('/admin/models/:id/generate-description', auth, async (req, res, next) => {
  try {
    const modelId = parseInt(req.params.id);
    if (!modelId) return res.status(400).json({ error: 'Invalid id' });

    const model = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!model) return res.status(404).json({ error: 'Model not found' });

    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey) return res.status(503).json({ error: 'AI not configured (ANTHROPIC_API_KEY missing)' });

    // Build context from model fields
    const context = [
      model.name && `Имя: ${model.name}`,
      model.age && `Возраст: ${model.age} лет`,
      model.height && `Рост: ${model.height} см`,
      model.city && `Город: ${model.city}`,
      model.category && `Категория: ${model.category}`,
      model.parameters && `Параметры: ${model.parameters}`,
      model.hair_color && `Цвет волос: ${model.hair_color}`,
      model.eye_color && `Цвет глаз: ${model.eye_color}`,
      model.languages && `Языки: ${model.languages}`,
      model.experience && `Опыт: ${model.experience}`,
    ].filter(Boolean).join('\n');

    // Call Anthropic API using Node 18+ global fetch
    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 300,
        messages: [{
          role: 'user',
          content: `Напиши профессиональное описание для модели агентства Nevesty Models. Используй следующие данные:\n\n${context}\n\nОписание должно быть:\n- 2-3 предложения, 80-150 слов\n- На русском языке\n- Профессиональный и привлекательный тон\n- Без упоминания агентства, только о модели\n- Без markdown разметки\n\nОписание:`
        }],
      }),
    });

    if (!response.ok) {
      const err = await response.text();
      return res.status(502).json({ error: 'AI API error', details: err.slice(0, 200) });
    }

    const data = await response.json();
    const description = data.content?.[0]?.text?.trim();
    if (!description) return res.status(502).json({ error: 'Empty AI response' });

    res.json({ description });
  } catch (e) { next(e); }
});

// ─── Model stats (admin) ──────────────────────────────────────────────
// GET /admin/models/:id/stats — aggregated stats for a single model
router.get('/admin/models/:id/stats', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT id, name, view_count FROM models WHERE id=?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });

    const [ordersRow, ratingRow] = await Promise.all([
      get(
        `SELECT
           COUNT(*) as total_orders,
           SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed_orders,
           SUM(CASE WHEN status NOT IN ('completed','cancelled') THEN 1 ELSE 0 END) as active_orders,
           SUM(CASE WHEN status='completed' AND budget IS NOT NULL AND budget != '' THEN CAST(budget AS REAL) ELSE 0 END) as revenue_total
         FROM orders WHERE model_id=?`,
        [id]
      ),
      get(
        `SELECT ROUND(AVG(rating), 2) as avg_rating, COUNT(*) as review_count
         FROM reviews WHERE model_id=? AND approved=1`,
        [id]
      ),
    ]);

    res.json({
      total_orders:     ordersRow?.total_orders     || 0,
      completed_orders: ordersRow?.completed_orders || 0,
      active_orders:    ordersRow?.active_orders    || 0,
      avg_rating:       ratingRow?.avg_rating       || null,
      review_count:     ratingRow?.review_count     || 0,
      view_count:       m.view_count                || 0,
      revenue_total:    ordersRow?.revenue_total    || 0,
    });
  } catch (e) { next(e); }
});

// GET /admin/analytics/model-stats/:id — alias used by admin UI
router.get('/admin/analytics/model-stats/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const m = await get('SELECT * FROM models WHERE id=?', [id]);
    if (!m) return res.status(404).json({ error: 'Модель не найдена' });

    const [ordersAgg, ratingAgg, monthlyRows, ratingDist] = await Promise.all([
      get(
        `SELECT
           COUNT(*) as total,
           SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
           SUM(CASE WHEN status NOT IN ('completed','cancelled') THEN 1 ELSE 0 END) as active,
           ROUND(AVG(CASE WHEN budget IS NOT NULL AND budget != '' AND CAST(budget AS REAL) > 0 THEN CAST(budget AS REAL) END), 0) as avg_budget
         FROM orders WHERE model_id=?`,
        [id]
      ),
      get(
        `SELECT ROUND(AVG(rating), 2) as avg_rating, COUNT(*) as total
         FROM reviews WHERE model_id=? AND approved=1`,
        [id]
      ),
      query(
        `SELECT strftime('%Y-%m-01', created_at) as month, COUNT(*) as count
         FROM orders WHERE model_id=?
         GROUP BY strftime('%Y-%m', created_at)
         ORDER BY month ASC
         LIMIT 12`,
        [id]
      ),
      query(
        `SELECT rating, COUNT(*) as cnt FROM reviews WHERE model_id=? AND approved=1 GROUP BY rating`,
        [id]
      ),
    ]);

    const distribution = {};
    for (const r of ratingDist) distribution[r.rating] = r.cnt;

    res.json({
      model: { id: m.id, name: m.name, category: m.category, city: m.city, view_count: m.view_count || 0 },
      orders: {
        total:      ordersAgg?.total      || 0,
        completed:  ordersAgg?.completed  || 0,
        active:     ordersAgg?.active     || 0,
        avg_budget: ordersAgg?.avg_budget || null,
      },
      reviews: {
        avg_rating:   ratingAgg?.avg_rating || null,
        total:        ratingAgg?.total      || 0,
        distribution,
      },
      monthly_orders: monthlyRows,
    });
  } catch (e) { next(e); }
});

// ─── Public model view tracking ────────────────────────────────────────────────
// In-memory rate limit store: "ip:modelId" -> last-seen timestamp
const _viewRateLimits = new Map();

// POST /models/:id/view — increment view_count (public, rate-limited to 1/hour per IP per model)
router.post('/models/:id/view', async (req, res) => {
  const id = parseInt(req.params.id);
  if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
  const ip = req.ip || req.connection?.remoteAddress || 'unknown';
  const key = `${ip}:${id}`;
  const now = Date.now();
  const last = _viewRateLimits.get(key) || 0;
  if (now - last < 60 * 60 * 1000) {
    return res.status(429).json({ ok: false, reason: 'rate_limited' });
  }
  _viewRateLimits.set(key, now);
  run('UPDATE models SET view_count = COALESCE(view_count, 0) + 1 WHERE id=?', [id]).catch(() => {});
  res.json({ ok: true });
});

// Public: check model availability for a date
router.get('/models/:id/availability', async (req, res, next) => {
  try {
    const { date } = req.query;
    if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      return res.status(400).json({ error: 'date required (YYYY-MM-DD)' });
    }
    const busy  = await get('SELECT id FROM model_busy_dates WHERE model_id=? AND busy_date=?', [req.params.id, date]);
    const model = await get('SELECT available FROM models WHERE id=?', [req.params.id]);
    res.json({ available: !busy && model && model.available === 1 });
  } catch (e) { next(e); }
});

// ─── Orders (public) ──────────────────────────────────────────────────────────
router.post('/orders', bookingLimiter, async (req, res, next) => {
  try {
    const csrfToken = req.headers['x-csrf-token'] || req.body._csrf;
    const { validateToken } = require('../middleware/csrf');
    if (!validateToken(csrfToken, req.ip || '')) {
      return res.status(403).json({ error: 'Invalid CSRF token' });
    }

    const { client_name, client_phone, client_email, client_telegram, client_chat_id,
            model_id, event_type, event_date, event_duration, location, budget, comments,
            utm_source, utm_medium, utm_campaign } = req.body;

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
      utm_source: sanitize(utm_source, 100) || '',
      utm_medium: sanitize(utm_medium, 100) || '',
      utm_campaign: sanitize(utm_campaign, 100) || '',
    };

    const result = await run(
      `INSERT INTO orders (order_number,client_name,client_phone,client_email,client_telegram,client_chat_id,model_id,event_type,event_date,event_duration,location,budget,comments,utm_source,utm_medium,utm_campaign)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
      [order_number, s.client_name, s.client_phone, s.client_email, s.client_telegram, s.client_chat_id, s.model_id, s.event_type, s.event_date, s.event_duration, s.location, s.budget, s.comments, s.utm_source, s.utm_medium, s.utm_campaign]
    );

    if (botInstance) {
      botInstance.notifyNewOrder({ id: result.id, order_number, ...s }).catch(e => console.error('Bot notify error:', e.message));
    }

    // ─── Email notifications (non-blocking) ──────────────────────────────────
    const orderForEmail = { id: result.id, order_number, ...s };
    if (s.client_email) {
      mailer.sendOrderConfirmation(s.client_email, orderForEmail).catch(e => console.error('[mailer] order confirmation error:', e.message));
    }
    const adminEmails = mailer.getAdminEmails();
    for (const adminEmail of adminEmails) {
      mailer.sendManagerNotification(adminEmail, orderForEmail).catch(e => console.error('[mailer] manager notification error:', e.message));
    }

    // ─── CRM webhooks (non-blocking) ─────────────────────────────────────────
    const { notifyCRM } = require('../services/crm');
    notifyCRM('order.created', { ...s, order_number, id: result.id }, getSetting).catch(() => {});

    // ─── SMS booking confirmation (non-blocking) ──────────────────────────────
    if (s.client_phone) {
      const { sendBookingConfirmationSms } = require('../services/sms');
      sendBookingConfirmationSms(s.client_phone, order_number)
        .catch(e => console.error('[SMS] Failed:', e.message));
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

// ─── Public: lookup orders by phone (client cabinet) ─────────────────────────
// GET /api/orders/by-phone?phone=79991234567
// Rate-limited: reuses clientRateLimit (10/hour per IP), defined below at line ~3423.
// NOTE: clientRateLimit is defined later in this file; use a simple inline limiter here.
const _byPhoneLimits = new Map();
function byPhoneLimiter(req, res, next) {
  const ip = req.ip || req.connection?.remoteAddress || 'unknown';
  const now = Date.now();
  const windowMs = 15 * 60 * 1000; // 15 minutes
  const maxReqs = 5;
  const timestamps = (_byPhoneLimits.get(ip) || []).filter(t => now - t < windowMs);
  if (timestamps.length >= maxReqs) {
    return res.status(429).json({ error: 'Слишком много запросов. Попробуйте через 15 минут.' });
  }
  timestamps.push(now);
  _byPhoneLimits.set(ip, timestamps);
  next();
}

router.get('/orders/by-phone', byPhoneLimiter, async (req, res, next) => {
  try {
    const rawPhone = (req.query.phone || '').trim();
    const digits = rawPhone.replace(/\D/g, '');
    // Normalize: strip leading 7 or 8 to get 10-digit base
    let phone10 = null;
    if (digits.length === 11 && (digits[0] === '7' || digits[0] === '8')) phone10 = digits.slice(1);
    else if (digits.length === 10) phone10 = digits;
    if (!phone10) return res.json({ orders: [], total: 0 });

    const patterns = [phone10, '7' + phone10, '+7' + phone10, '8' + phone10];
    const placeholders = patterns.map(() => '?').join(',');

    const EVENT_RU = {
      fashion_show: 'Показ мод', photo_shoot: 'Фотосессия', event: 'Мероприятие',
      commercial: 'Коммерческая съёмка', runway: 'Подиум', other: 'Другое'
    };

    const orders = await query(
      `SELECT o.id, o.order_number, o.created_at, o.event_type, o.event_date,
              o.budget, o.status, o.model_id, o.comments, o.location, o.client_name,
              m.name as model_name, m.photo_main as model_photo
       FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       WHERE REPLACE(REPLACE(REPLACE(REPLACE(o.client_phone, '+', ''), '-', ''), ' ', ''), '(', '') IN (${placeholders})
          OR REPLACE(REPLACE(REPLACE(REPLACE(o.client_phone, ')', ''), '-', ''), ' ', ''), '(', '') IN (${placeholders})
       GROUP BY o.id
       ORDER BY o.created_at DESC LIMIT 20`,
      [...patterns, ...patterns]
    );

    const result = orders.map(o => ({ ...o, event_type_ru: EVENT_RU[o.event_type] || o.event_type }));
    res.json({ orders: result, total: result.length });
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
router.post('/quick-booking', strictLimiter, async (req, res, next) => {
  try {
    const csrfToken = req.headers['x-csrf-token'] || req.body._csrf;
    const { validateToken } = require('../middleware/csrf');
    if (!validateToken(csrfToken, req.ip || '')) {
      return res.status(403).json({ error: 'Invalid CSRF token' });
    }

    const { client_name, client_phone } = req.body;
    if (!sanitize(client_name, 100)) return res.status(400).json({ error: 'Укажите имя' });
    if (!client_phone || !validatePhone(client_phone)) return res.status(400).json({ error: 'Укажите корректный номер телефона' });
    await run(
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
    // Support both from/to (legacy) and date_from/date_to (new)
    const from       = req.query.date_from || req.query.from;
    const to         = req.query.date_to   || req.query.to;
    const model_id   = req.query.model_id;
    const event_type = req.query.event_type;
    const page = Math.max(1, parseInt(req.query.page) || 1);
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit) || 25));
    const offset = (page - 1) * limit;
    let where = '1=1';
    const params = [];
    if (status && ALLOWED_STATUSES.includes(status)) { where += ' AND o.status = ?'; params.push(status); }
    if (model_id && !isNaN(+model_id) && +model_id > 0) { where += ' AND o.model_id = ?'; params.push(+model_id); }
    if (event_type && ALLOWED_EVENT_TYPES.includes(event_type)) { where += ' AND o.event_type = ?'; params.push(event_type); }
    if (search) {
      where += ' AND (o.client_name LIKE ? OR o.order_number LIKE ? OR o.client_phone LIKE ?)';
      params.push(`%${search}%`, `%${search}%`, `%${search}%`);
    }
    if (from && validateDate(from)) { where += ' AND date(o.created_at) >= ?'; params.push(from); }
    if (to   && validateDate(to))   { where += ' AND date(o.created_at) <= ?'; params.push(to); }
    const period = req.query.period;
    if (period === 'today')  { where += " AND date(o.created_at) = date('now')"; }
    else if (period === 'week')  { where += " AND o.created_at >= date('now', '-7 days')"; }
    else if (period === 'month') { where += " AND o.created_at >= date('now', '-30 days')"; }
    // Quick filter: unassigned (no manager)
    if (req.query.unassigned === '1') { where += ' AND o.manager_id IS NULL'; }
    // Quick filter: paid orders
    if (req.query.paid === '1') { where += ' AND o.paid_at IS NOT NULL'; }
    // Quick filter: high budget — sort by budget DESC (CAST to handle text budgets)
    const orderBy = req.query.sort_budget === 'desc'
      ? 'CAST(o.budget AS REAL) DESC'
      : 'o.created_at DESC';
    const [totalRow, orders] = await Promise.all([
      get(`SELECT COUNT(*) as n FROM orders o WHERE ${where}`, params),
      query(`SELECT o.*, m.name as model_name, a.username as manager_name
             FROM orders o
             LEFT JOIN models m ON o.model_id = m.id
             LEFT JOIN admins a ON o.manager_id = a.id
             WHERE ${where}
             ORDER BY ${orderBy}
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
    const { status, search, period, model_id, event_type } = req.query;
    const dateFrom = req.query.date_from || req.query.from;
    const dateTo   = req.query.date_to   || req.query.to;
    let where = '1=1'; const params = [];
    if (status && ALLOWED_STATUSES.includes(status)) { where += ' AND o.status = ?'; params.push(status); }
    if (model_id && !isNaN(+model_id) && +model_id > 0) { where += ' AND o.model_id = ?'; params.push(+model_id); }
    if (event_type && ALLOWED_EVENT_TYPES.includes(event_type)) { where += ' AND o.event_type = ?'; params.push(event_type); }
    if (search) { where += ' AND (o.client_name LIKE ? OR o.order_number LIKE ? OR o.client_phone LIKE ?)'; params.push(`%${search}%`, `%${search}%`, `%${search}%`); }
    if (dateFrom && validateDate(dateFrom)) { where += ' AND date(o.created_at) >= ?'; params.push(dateFrom); }
    if (dateTo   && validateDate(dateTo))   { where += ' AND date(o.created_at) <= ?'; params.push(dateTo); }
    if (period === 'today')  { where += " AND date(o.created_at) = date('now')"; }
    if (period === 'week')   { where += " AND o.created_at >= date('now', '-7 days')"; }
    if (period === 'month')  { where += " AND o.created_at >= date('now', '-30 days')"; }
    const orders = await query(
      `SELECT o.order_number, o.status, o.created_at, o.event_type, o.event_date,
              o.event_duration, o.location, o.client_name, o.client_phone, o.client_email,
              o.client_telegram, m.name as model_name, o.budget, o.comments,
              o.internal_note, o.paid_at
       FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       WHERE ${where} ORDER BY o.created_at DESC`, params
    );
    const STATUS_RU = { new:'Новая', reviewing:'На рассмотрении', confirmed:'Подтверждена', in_progress:'В процессе', completed:'Завершена', cancelled:'Отменена' };
    const EVENT_RU  = { fashion_show:'Показ мод', photo_shoot:'Фотосессия', event:'Мероприятие', commercial:'Коммерческая', runway:'Подиум', other:'Другое' };
    const SEP = ';';
    const csvCell2 = (v) => { let s = (v == null ? '' : String(v)); if (/^[=+\-@]/.test(s)) s = "'" + s; return '"' + s.replace(/"/g, '""') + '"'; };
    const csvRow2  = (cols) => cols.map(csvCell2).join(SEP);
    const headers  = ['Номер','Статус','Создана','Тип','Дата события','Длит.','Место','Клиент','Телефон','Email','Telegram','Модель','Бюджет','Комментарий','Заметка','Оплачено'];
    const rows = [headers.join(SEP), ...orders.map(o => csvRow2([
      o.order_number,
      STATUS_RU[o.status] || o.status,
      o.created_at,
      EVENT_RU[o.event_type] || o.event_type,
      o.event_date || '',
      o.event_duration || '',
      o.location || '',
      o.client_name,
      o.client_phone,
      o.client_email || '',
      o.client_telegram || '',
      o.model_name || '',
      o.budget || '',
      o.comments || '',
      o.internal_note || '',
      o.paid_at || ''
    ]))];
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="orders_${Date.now()}.csv"`);
    res.send('\xEF\xBB\xBF' + rows.join('\n')); // UTF-8 BOM for Excel
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

router.get('/admin/orders/:id/history', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const history = await query(
      `SELECT osh.*, a.username as admin_username
       FROM order_status_history osh
       LEFT JOIN admins a ON osh.changed_by = CAST(a.id AS TEXT)
       WHERE osh.order_id = ?
       ORDER BY osh.created_at ASC`,
      [id]
    );
    res.json(history);
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
    // Log status change to history
    if (status && status !== order.status) {
      await run(
        'INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, notes) VALUES (?,?,?,?,?)',
        [id, order.status, status, req.admin.username || 'admin', admin_notes || null]
      ).catch(() => {}); // non-blocking
    }
    if (botInstance && order.client_chat_id && status && status !== order.status) {
      botInstance.notifyStatusChange(order.client_chat_id, order.order_number, status);
    }
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Create payment link for order (admin) ────────────────────────────────────
router.post('/admin/orders/:id/pay', auth, async (req, res, next) => {
  try {
    const { id } = req.params;
    const { amount, description, provider = 'yookassa' } = req.body;
    if (!amount || isNaN(amount) || amount < 1) return res.status(400).json({ error: 'amount required (integer RUB)' });

    const order = await get('SELECT * FROM orders WHERE id=?', [id]);
    if (!order) return res.status(404).json({ error: 'Order not found' });

    const siteUrl = (process.env.SITE_URL || 'https://example.com').replace(/\/$/, '');
    const returnUrl = `${siteUrl}/order-status.html?order=${order.order_number}`;
    const desc = description || `Оплата заявки #${order.order_number} — ${order.event_type || 'Модель'}`;

    let result;
    if (provider === 'stripe') {
      result = await payment.createStripePayment(id, parseInt(amount), desc);
    } else {
      result = await payment.createYooKassaPayment(id, parseInt(amount), desc, returnUrl);
    }
    if (result.error) return res.status(400).json({ error: result.error });

    await run(
      'UPDATE orders SET payment_status=?, payment_id=?, payment_url=?, payment_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
      ['pending', result.payment_id || result.session_id, result.payment_url || null, parseInt(amount), id]
    );

    res.json({
      payment_url:   result.payment_url || null,
      payment_id:    result.payment_id || result.session_id,
      client_secret: result.client_secret || null,
      provider,
    });
  } catch (e) { next(e); }
});

// ─── Generate payment link stub (admin) ────────────────────────────────────────
router.post('/admin/orders/:id/payment-link', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    const order = await get('SELECT * FROM orders WHERE id=?', [id]);
    if (!order) return res.status(404).json({ error: 'Order not found' });

    // In real implementation, call Yookassa API here
    // For now, return a stub link
    const yookassaShopId = process.env.YOOKASSA_SHOP_ID || '';
    const yookassaKey = process.env.YOOKASSA_SECRET_KEY || '';

    if (!yookassaShopId || !yookassaKey) {
      return res.json({
        ok: true,
        link: null,
        message: 'Yookassa not configured. Set YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY in .env'
      });
    }

    // Stub response
    res.json({ ok: true, link: `https://yookassa.ru/checkout/payment/stub-${id}`, order_id: id });
  } catch(e) { next(e); }
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

// ─── Bulk status change for orders (admin) ───────────────────────────────────
router.post('/admin/orders/bulk-status', auth, async (req, res, next) => {
  try {
    const { order_ids, status } = req.body;
    if (!Array.isArray(order_ids) || !order_ids.length) return res.status(400).json({ error: 'Не указаны заявки' });
    if (!ALLOWED_STATUSES.includes(status)) return res.status(400).json({ error: 'Недопустимый статус' });
    const validIds = order_ids.map(Number).filter(n => n > 0);
    if (!validIds.length) return res.status(400).json({ error: 'Некорректные ID заявок' });
    const orders = await query(`SELECT id, client_chat_id, order_number, status FROM orders WHERE id IN (${validIds.map(() => '?').join(',')})`, validIds);
    await run(`UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id IN (${validIds.map(() => '?').join(',')})`, [status, ...validIds]);
    if (botInstance) {
      const toNotify = orders.filter(o => o.status !== status && o.client_chat_id);
      await Promise.allSettled(toNotify.map(o => botInstance.notifyStatusChange(o.client_chat_id, o.order_number, status)));
    }
    res.json({ ok: true, affected: validIds.length });
  } catch (e) { next(e); }
});

// ─── Search orders by phone or client name (admin) ───────────────────────────
router.get('/admin/orders/search', auth, async (req, res, next) => {
  try {
    const q = (req.query.q || '').trim();
    if (!q) return res.status(400).json({ error: 'Параметр поиска q обязателен' });
    const like = `%${q}%`;
    const orders = await query(
      `SELECT o.*, m.name as model_name
       FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       WHERE o.client_phone LIKE ? OR o.client_name LIKE ? OR o.order_number LIKE ?
       ORDER BY o.created_at DESC LIMIT 50`,
      [like, like, like]
    );
    res.json({ orders, total: orders.length });
  } catch (e) { next(e); }
});

// ─── PATCH /admin/orders/bulk-status — bulk status update (REST alias) ───────
router.patch('/admin/orders/bulk-status', auth, async (req, res, next) => {
  try {
    const { ids, status } = req.body;
    const VALID_STATUSES = ['new', 'reviewing', 'confirmed', 'in_progress', 'completed', 'cancelled'];
    if (!Array.isArray(ids) || !ids.length) return res.status(400).json({ error: 'ids required' });
    if (!VALID_STATUSES.includes(status)) return res.status(400).json({ error: 'Invalid status' });
    const validIds = ids.map(Number).filter(n => Number.isInteger(n) && n > 0);
    if (!validIds.length) return res.status(400).json({ error: 'No valid IDs' });
    const orders = await query(`SELECT id, client_chat_id, order_number, status FROM orders WHERE id IN (${validIds.map(() => '?').join(',')})`, validIds);
    await run(
      `UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id IN (${validIds.map(() => '?').join(',')})`,
      [status, ...validIds]
    );
    if (botInstance) {
      const toNotify = orders.filter(o => o.status !== status && o.client_chat_id);
      await Promise.allSettled(toNotify.map(o => botInstance.notifyStatusChange(o.client_chat_id, o.order_number, status)));
    }
    res.json({ updated: validIds.length });
  } catch (e) { next(e); }
});

// ─── Export orders with advanced filters (admin) ─────────────────────────────
router.get('/admin/export/orders', auth, async (req, res, next) => {
  // Legacy alias — redirect to enhanced endpoint
  res.redirect('/api/admin/orders/export?' + new URLSearchParams(req.query).toString());
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
      await botInstance.notifyAdmin(`📢 *${escMd(req.admin.username)}:*\n${escMd(text)}`, { parse_mode: 'MarkdownV2' });
    }
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// GET /api/admin/broadcasts — list all scheduled_broadcasts with stats
router.get('/admin/broadcasts', auth, async (req, res, next) => {
  try {
    const limit = Math.min(50, parseInt(req.query.limit) || 20);
    const rows = await query(
      `SELECT id, text, photo_url, segment, scheduled_at, status, sent_count, error_count, sent_at, created_at
       FROM scheduled_broadcasts ORDER BY created_at DESC LIMIT ?`,
      [limit]
    );
    res.json(rows);
  } catch (e) { next(e); }
});

// POST /api/admin/broadcasts — create a new scheduled (or immediate) broadcast
router.post('/admin/broadcasts', auth, async (req, res, next) => {
  try {
    const text = sanitize(req.body.text, 4096);
    if (!text) return res.status(400).json({ error: 'Текст не может быть пустым' });

    const rawSegment = req.body.segment || 'all';
    const segment = (['all', 'completed', 'active', 'new'].includes(rawSegment) || /^city_[a-zA-Zа-яА-ЯёЁ0-9\s\-]+$/.test(rawSegment))
      ? rawSegment : 'all';
    const photoUrl = req.body.photo_url ? sanitize(String(req.body.photo_url), 500) : null;

    let scheduledAt;
    if (req.body.scheduled_at) {
      const d = new Date(req.body.scheduled_at);
      if (isNaN(d.getTime()) || d < new Date()) return res.status(400).json({ error: 'Неверная дата или дата в прошлом' });
      scheduledAt = d.toISOString();
    } else {
      scheduledAt = new Date().toISOString(); // immediate (scheduler picks it up on next tick)
    }

    const result = await run(
      `INSERT INTO scheduled_broadcasts (text, photo_url, segment, scheduled_at, status, created_by)
       VALUES (?, ?, ?, ?, 'pending', ?)`,
      [text, photoUrl, segment, scheduledAt, req.admin.username]
    );
    res.json({ id: result.id, scheduled_at: scheduledAt });
  } catch (e) { next(e); }
});

// GET /api/admin/broadcasts/count?segment= — count recipients for a segment (for preview)
router.get('/admin/broadcasts/count', auth, async (req, res, next) => {
  try {
    const seg = req.query.segment || 'all';
    let rows = [];
    if (seg === 'completed') {
      rows = await query(
        "SELECT COUNT(DISTINCT client_chat_id) as cnt FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND status='completed'"
      );
    } else if (seg === 'active') {
      rows = await query(
        "SELECT COUNT(DISTINCT client_chat_id) as cnt FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND created_at >= datetime('now', '-30 days')"
      );
    } else if (seg === 'new') {
      rows = await query(
        "SELECT COUNT(DISTINCT client_chat_id) as cnt FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND client_chat_id NOT IN (SELECT DISTINCT client_chat_id FROM orders WHERE status IN ('confirmed','in_progress','completed') AND client_chat_id IS NOT NULL AND client_chat_id != '')"
      );
    } else if (/^city_/.test(seg)) {
      const city = seg.slice(5);
      rows = await query(
        `SELECT COUNT(DISTINCT o.client_chat_id) as cnt FROM orders o JOIN models m ON o.model_id=m.id WHERE o.client_chat_id IS NOT NULL AND o.client_chat_id != '' AND m.city=?`,
        [city]
      );
    } else {
      rows = await query(
        "SELECT COUNT(DISTINCT client_chat_id) as cnt FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != ''"
      );
    }
    res.json({ count: rows[0]?.cnt || 0 });
  } catch (e) { next(e); }
});

// DELETE /api/admin/broadcasts/:id — cancel a pending broadcast
router.delete('/admin/broadcasts/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const row = await get('SELECT id, status FROM scheduled_broadcasts WHERE id=?', [id]);
    if (!row) return res.status(404).json({ error: 'Рассылка не найдена' });
    if (row.status !== 'pending') return res.status(400).json({ error: 'Можно отменить только ожидающую рассылку' });
    await run("UPDATE scheduled_broadcasts SET status='cancelled' WHERE id=?", [id]);
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Managers ─────────────────────────────────────────────────────────────────
router.get('/admin/managers', auth, async (req, res, next) => {
  try {
    if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
    const managers = await query(
      `SELECT a.id, a.username, a.email, a.role, a.telegram_id, a.created_at,
        (SELECT COUNT(*) FROM orders WHERE manager_id=a.id) as total_orders,
        (SELECT COUNT(*) FROM orders WHERE manager_id=a.id AND status='completed') as completed_orders
       FROM admins a ORDER BY a.created_at DESC`
    );
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

router.get('/admin/managers/:id/stats', auth, async (req, res) => {
  if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
  const managerId = parseInt(req.params.id);
  try {
    const [assignedTotal, assignedCompleted, assignedActive] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders WHERE manager_id=?', [managerId]),
      get("SELECT COUNT(*) as n FROM orders WHERE manager_id=? AND status='completed'", [managerId]),
      get("SELECT COUNT(*) as n FROM orders WHERE manager_id=? AND status IN ('new','reviewing','confirmed','in_progress')", [managerId]),
    ]);
    const cycleRow = await get(
      `SELECT AVG(CAST(julianday(updated_at) - julianday(created_at) AS INTEGER)) as avg_days
       FROM orders WHERE manager_id=? AND status='completed'`,
      [managerId]
    ).catch(() => null);

    res.json({
      ok: true,
      stats: {
        total_assigned: assignedTotal?.n || 0,
        completed: assignedCompleted?.n || 0,
        active: assignedActive?.n || 0,
        completion_rate: assignedTotal?.n > 0 ? Math.round((assignedCompleted?.n / assignedTotal?.n) * 100) : 0,
        avg_days_to_complete: cycleRow?.avg_days ? Math.round(cycleRow.avg_days) : null,
      }
    });
  } catch (e) { res.json({ ok: false, error: e.message }); }
});

// GET /api/settings/public — public read-only settings (no auth required, cached 5 min)
router.get('/settings/public', async (req, res) => {
  try {
    const SAFE_KEYS = [
      'contacts_phone', 'contacts_email', 'contacts_insta', 'contacts_addr',
      'contacts_instagram', 'contacts_address',
      'contacts_whatsapp', 'about', 'about_text', 'greeting', 'agency_name', 'tagline',
      'catalog_per_page', 'site_url', 'manager_hours',
      'pricing_start_from', 'pricing_event_from', 'pricing_premium_from',
      'ga_measurement_id', 'ym_counter_id',
      'tg_channel', 'telegram_channel_id'
    ];
    const cacheKey = 'settings:public';
    const cached = cache.get(cacheKey);
    if (cached !== undefined) return res.json(cached);

    const placeholders = SAFE_KEYS.map(() => '?').join(',');
    const rows = await query(
      `SELECT key, value FROM bot_settings WHERE key IN (${placeholders})`,
      SAFE_KEYS
    );
    const settings = {};
    rows.forEach(r => { settings[r.key] = r.value; });
    cache.set(cacheKey, settings);
    res.json(settings);
  } catch (e) {
    res.status(500).json({ error: 'Settings unavailable' });
  }
});

// GET /api/stats/public — public statistics for about page (cached 10 min)
router.get('/stats/public', async (req, res) => {
  try {
    const cacheKey = 'stats:public';
    const cached = cache.get(cacheKey);
    if (cached !== undefined) return res.json(cached);

    const [modelsRow, ordersRow, citiesRow] = await Promise.all([
      get('SELECT COUNT(*) as cnt FROM models WHERE active = 1').catch(() => ({ cnt: 0 })),
      get('SELECT COUNT(*) as cnt FROM orders WHERE status IN (\'confirmed\',\'completed\')').catch(() => ({ cnt: 0 })),
      get('SELECT COUNT(DISTINCT city) as cnt FROM models WHERE active = 1 AND city IS NOT NULL AND city != \'\'').catch(() => ({ cnt: 0 })),
    ]);

    const stats = {
      total_models:    modelsRow?.cnt || 0,
      completed_orders: ordersRow?.cnt || 0,
      cities_count:    citiesRow?.cnt || 0,
    };
    cache.set(cacheKey, stats, 10 * 60 * 1000); // 10 min TTL
    res.json(stats);
  } catch (e) {
    res.status(500).json({ error: 'Stats unavailable' });
  }
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

// GET /api/admin/settings — alias returning all settings as array of {key,value}
router.get('/admin/settings', auth, async (req, res, next) => {
  try {
    const rows = await query('SELECT key, value FROM bot_settings ORDER BY key');
    res.json(rows);
  } catch(e) { next(e); }
});

// GET /api/admin/settings/export — exports all settings as JSON file
router.get('/admin/settings/export', auth, async (req, res, next) => {
  try {
    const settings = await query('SELECT key, value FROM bot_settings ORDER BY key');
    const obj = Object.fromEntries(settings.map(s => [s.key, s.value]));
    res.set('Content-Disposition', 'attachment; filename="settings.json"');
    res.json(obj);
  } catch (e) { next(e); }
});

// POST /api/admin/settings/import — imports settings from JSON object
router.post('/admin/settings/import', auth, async (req, res, next) => {
  try {
    const settings = req.body;
    if (typeof settings !== 'object' || Array.isArray(settings)) {
      return res.status(400).json({ error: 'Expected JSON object' });
    }

    // Skip sensitive keys on import
    const SKIP_KEYS = ['admin_password', 'jwt_secret', 'totp_secret'];

    let imported = 0;
    for (const [key, value] of Object.entries(settings)) {
      if (SKIP_KEYS.includes(key)) continue;
      if (typeof value !== 'string' || value.length > 10000) continue;
      await run('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', [key, value]);
      imported++;
    }

    await logAudit(req, 'import_settings', 'setting', null, { imported });
    res.json({ ok: true, imported });
  } catch (e) { next(e); }
});

// POST /api/admin/settings/reset — resets a specific setting to its default value
router.post('/admin/settings/reset', auth, async (req, res, next) => {
  try {
    const { key } = req.body;
    if (!key) return res.status(400).json({ error: 'key required' });

    const DEFAULTS = {
      greeting: 'Добро пожаловать в Nevesty Models!',
      reviews_enabled: '1',
      wishlist_enabled: '1',
      quick_booking_enabled: '1',
      catalog_per_page: '6',
      catalog_sort: 'featured',
      event_reminders_enabled: '1',
    };

    if (!DEFAULTS[key]) return res.status(400).json({ error: 'No default for this key' });
    await run('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', [key, DEFAULTS[key]]);
    await logAudit(req, 'reset_setting', 'setting', null, { key, value: DEFAULTS[key] });
    res.json({ ok: true, value: DEFAULTS[key] });
  } catch (e) { next(e); }
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
    // Bot texts
    'greeting', 'about_text', 'about', 'pricing', 'manager_hours', 'manager_reply', 'site_url',
    // Contacts
    'contacts_phone', 'contacts_email', 'contacts_instagram', 'contacts_insta',
    'contacts_whatsapp', 'contacts_address', 'contacts_addr',
    // Booking
    'quick_booking_enabled', 'booking_auto_confirm', 'booking_require_email',
    'booking_min_budget', 'booking_confirm_msg',
    // Catalog
    'catalog_per_page', 'catalog_default_sort', 'catalog_show_city', 'catalog_badge_top',
    'cities_list',
    // Notifications
    'notif_new_order', 'notify_new_order', 'notif_status', 'notify_status_change',
    'notif_message', 'notify_review', 'notify_message',
    // Features
    'wishlist_enabled', 'search_enabled', 'reviews_enabled', 'loyalty_enabled', 'referral_enabled',
    // Appearance / integrations
    'agency_name', 'tagline', 'hero_image', 'webhook_url', 'tg_notif_enabled',
    // Payment
    'payment_provider', 'payment_min_amount', 'payment_prepay_percent',
    // FAQ
    'faq_items',
    // CRM webhooks
    'crm_webhook_url', 'crm_webhook_secret', 'amocrm_webhook_url', 'amocrm_api_key', 'bitrix24_webhook_url',
    // Pricing tier minimums
    'pricing_start_from', 'pricing_event_from', 'pricing_premium_from',
    // Telegram channel
    'telegram_channel_id', 'tg_channel',
    // Bot settings extras
    'welcome_photo_url', 'main_menu_text', 'pricing_text', 'booking_thanks_text',
    'calc_enabled', 'catalog_title', 'catalog_sort',
    // Limits
    'model_max_photos', 'client_max_active_orders',
    // Additional notifications
    'notifications_reviews', 'notifications_messages',
    // Booking additional
    'booking_autoconfirm', 'booking_quick_enabled',
    // Analytics
    'ga_measurement_id', 'ym_counter_id',
    'sms_enabled',
  ];
  try {
    const body = req.body;
    if (typeof body !== 'object' || !body) return res.status(400).json({ error: 'Invalid body' });
    for (const [key, value] of Object.entries(body)) {
      if (!ALLOWED_KEYS.includes(key)) continue;
      const v = String(value ?? '').trim().slice(0, 2000);
      await run('INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', [key, v]);
      cache.del(`setting:${key}`); // invalidate individual setting cache entry
    }
    cache.del('settings:public'); // invalidate public settings bundle
    res.json({ ok: true });
  } catch(e) { next(e); }
});

// ─── Reviews (public) ─────────────────────────────────────────────────────────
router.get('/reviews', async (req, res, next) => {
  try {
    const usePagination = req.query.page !== undefined;
    const page = Math.max(1, parseInt(req.query.page) || 1);
    const limit = Math.min(200, Math.max(1, parseInt(req.query.limit) || 20));
    const offset = (page - 1) * limit;
    const model_id = req.query.model_id ? parseInt(req.query.model_id) : null;
    let where = 'r.approved = 1';
    const params = [];
    if (model_id && Number.isInteger(model_id) && model_id > 0) {
      where += ' AND r.model_id = ?';
      params.push(model_id);
    }
    if (usePagination) {
      const [totalRow, reviews] = await Promise.all([
        get(`SELECT COUNT(*) as n FROM reviews r WHERE ${where}`, [...params]),
        query(
          `SELECT r.id, r.client_name, r.rating, r.text, r.model_id, r.created_at,
                  r.admin_reply, r.reply_at, m.name as model_name
           FROM reviews r LEFT JOIN models m ON r.model_id = m.id
           WHERE ${where} ORDER BY r.created_at DESC LIMIT ? OFFSET ?`,
          [...params, limit, offset]
        )
      ]);
      return res.json({ reviews, total: totalRow.n, page, pages: Math.ceil(totalRow.n / limit), limit });
    }
    // Legacy: return plain array (backward-compat)
    const reviews = await query(
      `SELECT r.id, r.client_name, r.rating, r.text, r.model_id, r.created_at,
              r.admin_reply, r.reply_at, m.name as model_name
       FROM reviews r LEFT JOIN models m ON r.model_id = m.id
       WHERE ${where} ORDER BY r.created_at DESC LIMIT ?`,
      [...params, limit]
    );
    res.json(reviews);
  } catch (e) { next(e); }
});

// ─── Reviews recent (public, for homepage) ────────────────────────────────────
router.get('/reviews/recent', async (req, res, next) => {
  try {
    const limit = Math.min(20, Math.max(1, parseInt(req.query.limit) || 5));
    const rows = await query(
      `SELECT r.id, r.rating, r.text, r.client_name, r.created_at,
              r.admin_reply, r.reply_at, m.name as model_name
       FROM reviews r
       LEFT JOIN models m ON m.id = r.model_id
       WHERE r.approved = 1
       ORDER BY r.created_at DESC
       LIMIT ?`,
      [limit]
    );
    res.json(rows);
  } catch (e) { next(e); }
});

// ─── Reviews (public, explicit endpoint) ──────────────────────────────────────
router.get('/reviews/public', async (req, res, next) => {
  try {
    const limit = Math.min(parseInt(req.query.limit) || 6, 20);
    const rows = await query(
      `SELECT r.id, r.rating, r.text, r.client_name as author_name, r.created_at,
              m.name as model_name
       FROM reviews r
       LEFT JOIN models m ON m.id = r.model_id
       WHERE r.approved = 1
       ORDER BY r.created_at DESC
       LIMIT ?`,
      [limit]
    );
    res.json(rows);
  } catch (e) { next(e); }
});

// ─── Reviews (admin) ──────────────────────────────────────────────────────────
router.get('/admin/reviews', auth, async (req, res, next) => {
  try {
    const page = Math.max(1, parseInt(req.query.page) || 1);
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit) || 25));
    const offset = (page - 1) * limit;
    // Support both ?approved=0/1 and ?filter=pending/approved/all
    let approvedVal = req.query.approved; // '0', '1', or undefined
    if (!approvedVal && req.query.filter) {
      if (req.query.filter === 'pending') approvedVal = '0';
      else if (req.query.filter === 'approved') approvedVal = '1';
    }
    let where = '1=1';
    const params = [];
    if (approvedVal === '0' || approvedVal === '1') {
      where += ' AND r.approved = ?';
      params.push(parseInt(approvedVal));
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
    await logAudit(req, 'toggle_approve', 'review', id, { approved: newApproved });
    res.json({ ok: true, approved: newApproved });
  } catch (e) { next(e); }
});

// PATCH /admin/reviews/:id — update approved status
router.patch('/admin/reviews/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const review = await get('SELECT id FROM reviews WHERE id = ?', [id]);
    if (!review) return res.status(404).json({ error: 'Отзыв не найден' });
    const { approved } = req.body;
    if (approved !== 0 && approved !== 1) return res.status(400).json({ error: 'approved must be 0 or 1' });
    await run('UPDATE reviews SET approved = ? WHERE id = ?', [approved, id]);
    await logAudit(req, 'update_approved', 'review', id, { approved });
    res.json({ ok: true, approved });
  } catch (e) { next(e); }
});

// POST /admin/reviews/bulk-approve — approve multiple reviews at once
router.post('/admin/reviews/bulk-approve', auth, async (req, res, next) => {
  try {
    const { ids } = req.body;
    if (!Array.isArray(ids) || !ids.length) return res.status(400).json({ error: 'ids required' });
    const validIds = ids.map(Number).filter(n => Number.isInteger(n) && n > 0);
    if (!validIds.length) return res.status(400).json({ error: 'No valid IDs' });
    const phs = validIds.map(() => '?').join(',');
    const result = await run(`UPDATE reviews SET approved=1 WHERE id IN (${phs}) AND approved=0`, validIds);
    await logAudit(req, 'bulk_approve', 'review', null, { ids: validIds, updated: result.changes });
    res.json({ ok: true, updated: result.changes });
  } catch (e) { next(e); }
});

// PATCH /admin/reviews/:id/approve — explicitly approve a review
router.patch('/admin/reviews/:id/approve', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const review = await get('SELECT id FROM reviews WHERE id = ?', [id]);
    if (!review) return res.status(404).json({ error: 'Отзыв не найден' });
    await run('UPDATE reviews SET approved = 1 WHERE id = ?', [id]);
    await logAudit(req, 'approve', 'review', id, { approved: 1 });
    res.json({ ok: true, approved: 1 });
  } catch (e) { next(e); }
});

// PATCH /admin/reviews/:id/reject — reject (unpublish) a review
router.patch('/admin/reviews/:id/reject', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const review = await get('SELECT id FROM reviews WHERE id = ?', [id]);
    if (!review) return res.status(404).json({ error: 'Отзыв не найден' });
    await run('UPDATE reviews SET approved = 0 WHERE id = ?', [id]);
    await logAudit(req, 'reject', 'review', id, { approved: 0 });
    res.json({ ok: true, approved: 0 });
  } catch (e) { next(e); }
});

// PATCH /admin/reviews/:id/reply — save admin reply to review
router.patch('/admin/reviews/:id/reply', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid id' });
    const { reply } = req.body;
    if (typeof reply !== 'string') return res.status(400).json({ error: 'reply must be string' });
    const replyText = reply.slice(0, 1000) || null;
    await run('UPDATE reviews SET admin_reply = ?, reply_at = CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id = ?', [replyText, replyText, id]);
    res.json({ ok: true });
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

// ─── Admin audit log ─────────────────────────────────────────────────────────

// Map event_type chips to action LIKE patterns
function auditEventTypeFilter(eventType) {
  const map = {
    auth:       ['login%', 'logout%', 'auth%', '%password%', '%totp%', '%2fa%'],
    orders:     ['%order%', '%booking%', '%заявк%'],
    models:     ['%model%', '%модел%', '%photo%'],
    settings:   ['%setting%', '%config%', '%настройк%'],
    broadcasts: ['%broadcast%', '%рассылк%', '%send_broadcast%'],
    factory:    ['%factory%', '%agent%', '%ai%', '%generate%'],
  };
  return map[eventType] || null;
}

function auditSinceClause(since) {
  const now = new Date();
  if (since === 'today') {
    const d = now.toISOString().slice(0, 10);
    return `al.created_at >= '${d} 00:00:00'`;
  }
  if (since === '7d') {
    const d = new Date(now - 7 * 86400000).toISOString().slice(0, 19).replace('T', ' ');
    return `al.created_at >= '${d}'`;
  }
  if (since === '30d') {
    const d = new Date(now - 30 * 86400000).toISOString().slice(0, 19).replace('T', ' ');
    return `al.created_at >= '${d}'`;
  }
  return null;
}

router.get('/admin/audit-log', auth, async (req, res, next) => {
  try {
    const limit = Math.min(parseInt(req.query.limit) || 50, 200);
    const offset = Math.max(parseInt(req.query.offset) || 0, 0);
    const adminFilter = req.query.admin ? sanitize(req.query.admin, 100) : null;
    const action = req.query.action ? sanitize(req.query.action, 50) : null;
    const eventType = req.query.event_type || null;
    const since = req.query.since || null;

    let sql = `SELECT al.* FROM audit_log al WHERE 1=1`;
    const params = [];
    if (adminFilter) { sql += ` AND (al.admin_username LIKE ? OR al.admin_chat_id = ?)`; params.push('%' + adminFilter + '%', adminFilter); }
    if (action) { sql += ` AND al.action = ?`; params.push(action); }
    if (eventType) {
      const patterns = auditEventTypeFilter(eventType);
      if (patterns) {
        sql += ` AND (` + patterns.map(() => `al.action LIKE ?`).join(' OR ') + `)`;
        params.push(...patterns);
      }
    }
    const sinceClause = since ? auditSinceClause(since) : null;
    if (sinceClause) sql += ` AND ${sinceClause}`;
    sql += ` ORDER BY al.created_at DESC LIMIT ? OFFSET ?`;
    params.push(limit, offset);

    const rows = await query(sql, params);

    let countSql = `SELECT COUNT(*) as n FROM audit_log al WHERE 1=1`;
    const countParams = [];
    if (adminFilter) { countSql += ` AND (al.admin_username LIKE ? OR al.admin_chat_id = ?)`; countParams.push('%' + adminFilter + '%', adminFilter); }
    if (action) { countSql += ` AND al.action = ?`; countParams.push(action); }
    if (eventType) {
      const patterns = auditEventTypeFilter(eventType);
      if (patterns) {
        countSql += ` AND (` + patterns.map(() => `al.action LIKE ?`).join(' OR ') + `)`;
        countParams.push(...patterns);
      }
    }
    if (sinceClause) countSql += ` AND ${sinceClause}`;
    const total = await get(countSql, countParams);

    // Also return distinct actions for frontend filter dropdowns
    const actions = await query(`SELECT DISTINCT action FROM audit_log WHERE action IS NOT NULL ORDER BY action`, []);

    res.json({ rows, total: total?.n || 0, actions: actions.map(a => a.action) });
  } catch(e) { next(e); }
});

// ─── Admin audit log CSV export ───────────────────────────────────────────────
router.get('/admin/audit/export', auth, async (req, res, next) => {
  try {
    const limit = 1000;
    const eventType = req.query.event_type || null;
    const since = req.query.since || null;

    let sql = `SELECT al.id, al.action, al.entity, al.entity_type, al.entity_id,
                      al.created_at, al.ip, al.admin_username, al.admin_chat_id
               FROM audit_log al WHERE 1=1`;
    const params = [];
    if (eventType) {
      const patterns = auditEventTypeFilter(eventType);
      if (patterns) {
        sql += ` AND (` + patterns.map(() => `al.action LIKE ?`).join(' OR ') + `)`;
        params.push(...patterns);
      }
    }
    const sinceClause = since ? auditSinceClause(since) : null;
    if (sinceClause) sql += ` AND ${sinceClause}`;
    sql += ` ORDER BY al.created_at DESC LIMIT ?`;
    params.push(limit);

    const rows = await query(sql, params);

    const csv = [
      'ID,Action,Entity,Entity_Type,Entity_ID,Admin,Admin_ChatID,IP,Timestamp',
      ...rows.map(r => [
        r.id, r.action, r.entity || '', r.entity_type || '', r.entity_id || '',
        r.admin_username || '', r.admin_chat_id || '', r.ip || '', r.created_at
      ].map(v => `"${String(v || '').replace(/"/g, '""')}"`).join(','))
    ].join('\n');

    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="audit_${Date.now()}.csv"`);
    res.send('﻿' + csv); // BOM for Excel
  } catch(e) { next(e); }
});

// ─── Order messages list (admin) ─────────────────────────────────────────────
router.get('/admin/orders/:id/messages', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const messages = await query(
      `SELECT id, sender_type, sender_name, content, created_at FROM messages WHERE order_id = ? ORDER BY created_at ASC`,
      [id]
    );
    res.json(messages);
  } catch (e) { next(e); }
});

// ─── Recent client messages (admin) ──────────────────────────────────────────
router.get('/admin/messages/recent', auth, async (req, res, next) => {
  try {
    const limit = Math.min(20, Math.max(1, parseInt(req.query.limit) || 10));
    const rows = await query(`
      SELECT m.id, m.order_id, m.content, m.created_at, m.sender_type,
             o.order_number, o.client_name, o.client_chat_id
      FROM messages m
      JOIN orders o ON m.order_id = o.id
      WHERE m.sender_type = 'client'
      ORDER BY m.created_at DESC
      LIMIT ?
    `, [limit]);
    res.json({ ok: true, messages: rows });
  } catch (e) { next(e); }
});

// ─── Order detail (admin, with model join) ───────────────────────────────────
// ─── Paginated client messages list (admin) ──────────────────────────────────
router.get('/admin/messages', auth, async (req, res, next) => {
  try {
    const limit = Math.min(100, Math.max(1, parseInt(req.query.limit) || 50));
    const offset = Math.max(0, parseInt(req.query.offset) || 0);
    const filter = req.query.filter || 'all'; // all | unread | today

    let sql = `
      SELECT m.id, m.order_id, m.content, m.created_at, m.sender_type,
             o.order_number, o.client_name, o.client_chat_id
      FROM messages m
      JOIN orders o ON m.order_id = o.id
      WHERE m.sender_type = 'client'
    `;
    const params = [];

    if (filter === 'unread') {
      sql += ` AND NOT EXISTS (
        SELECT 1 FROM messages m2
        WHERE m2.order_id = m.order_id AND m2.sender_type = 'admin'
          AND m2.created_at > m.created_at
      )`;
    } else if (filter === 'today') {
      sql += ` AND date(m.created_at) = date('now')`;
    }

    sql += ` ORDER BY m.created_at DESC LIMIT ? OFFSET ?`;
    params.push(limit, offset);

    const rows = await query(sql, params);

    let countSql = `SELECT COUNT(*) as n FROM messages m JOIN orders o ON m.order_id = o.id WHERE m.sender_type = 'client'`;
    const countParams = [];
    if (filter === 'unread') {
      countSql += ` AND NOT EXISTS (SELECT 1 FROM messages m2 WHERE m2.order_id = m.order_id AND m2.sender_type = 'admin' AND m2.created_at > m.created_at)`;
    } else if (filter === 'today') {
      countSql += ` AND date(m.created_at) = date('now')`;
    }
    const total = await get(countSql, countParams);

    res.json({ ok: true, messages: rows, total: total?.n || 0 });
  } catch (e) { next(e); }
});

router.get('/admin/orders/:id/detail', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const order = await get(
      `SELECT o.*, m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       WHERE o.id=?`, [id]);
    if (!order) return res.status(404).json({ error: 'Not found' });
    res.json(order);
  } catch (e) { res.status(500).json({ error: 'DB error' }); }
});

// ─── Order notes (admin, /admin/ prefix) ─────────────────────────────────────
router.get('/admin/orders/:id/notes', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const notes = await query(
      `SELECT * FROM order_notes WHERE order_id=? ORDER BY created_at DESC LIMIT 20`, [id]);
    res.json({ notes });
  } catch (e) { res.status(500).json({ error: 'DB error' }); }
});

router.post('/admin/orders/:id/notes', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const note = sanitize(req.body.note, 2000);
    if (!note) return res.status(400).json({ error: 'Note required' });
    await run(`INSERT INTO order_notes (order_id, admin_note) VALUES (?,?)`, [id, note]);
    res.json({ success: true });
  } catch (e) { res.status(500).json({ error: 'DB error' }); }
});

// ─── Order internal note (quick patch) ───────────────────────────────────────
router.patch('/admin/orders/:id/note', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const note = req.body.note !== undefined ? String(req.body.note).slice(0, 2000) : '';
    await run('UPDATE orders SET internal_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', [note, id]);
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Order payment status patch ───────────────────────────────────────────────
router.patch('/admin/orders/:id/payment', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const { paid } = req.body;
    if (paid !== true && paid !== false) return res.status(400).json({ error: 'paid must be boolean' });
    const order = await get('SELECT id FROM orders WHERE id=?', [id]);
    if (!order) return res.status(404).json({ error: 'Not found' });
    if (paid) {
      await run(`UPDATE orders SET paid_at=datetime('now'), updated_at=CURRENT_TIMESTAMP WHERE id=?`, [id]);
    } else {
      await run(`UPDATE orders SET paid_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?`, [id]);
    }
    const updated = await get('SELECT id, paid_at FROM orders WHERE id=?', [id]);
    res.json({ ok: true, paid_at: updated.paid_at });
  } catch (e) { next(e); }
});

// ─── Send invoice (mark invoice_sent_at) ─────────────────────────────────────
router.post('/admin/orders/:id/send-invoice', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const order = await get('SELECT id, order_number, client_chat_id, client_name FROM orders WHERE id=?', [id]);
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });
    await run(
      `UPDATE orders SET invoice_sent_at=datetime('now'), updated_at=CURRENT_TIMESTAMP WHERE id=?`,
      [id]
    );
    await logAudit(req, 'invoice_sent', 'order', id, { order_number: order.order_number });
    res.json({ ok: true, invoice_sent_at: new Date().toISOString() });
  } catch (e) { next(e); }
});

// ─── Order status patch ────────────────────────────────────────────────────────
router.patch('/admin/orders/:id/status', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });
    const { status } = req.body;
    if (!ALLOWED_STATUSES.includes(status)) return res.status(400).json({ error: 'Invalid status' });
    const order = await get('SELECT id, client_chat_id, client_email, client_phone, order_number, status as prev_status, client_name, event_type, event_date, event_duration, location, budget FROM orders WHERE id=?', [id]);
    if (!order) return res.status(404).json({ error: 'Not found' });
    await run(`UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?`, [status, id]);
    // Log status change to history
    if (status !== order.prev_status) {
      await run(
        'INSERT INTO order_status_history (order_id, old_status, new_status, changed_by) VALUES (?,?,?,?)',
        [id, order.prev_status, status, req.admin.username || 'admin']
      ).catch(() => {}); // non-blocking
    }
    if (botInstance && order.client_chat_id && status !== order.prev_status) {
      botInstance.notifyStatusChange(order.client_chat_id, order.order_number, status).catch(() => {});
    }
    // ─── Email notification on status change ─────────────────────────────────
    if (order.client_email && status !== order.prev_status) {
      mailer.sendStatusChange(order.client_email, order, order.prev_status, status)
        .catch(e => console.error('[mailer] status change error:', e.message));
    }
    // ─── SMS notification on status change ───────────────────────────────────
    if (order.client_phone && status !== order.prev_status) {
      try {
        const smsRow = await get('SELECT value FROM bot_settings WHERE key=?', ['sms_enabled']).catch(() => null);
        if (smsRow && smsRow.value === '1') {
          const { sendStatusChangeSMS } = require('../services/sms');
          await sendStatusChangeSMS(order.client_phone, order.order_number, status).catch(() => {});
        }
      } catch {}
    }
    // ─── WebSocket real-time notification ────────────────────────────────────
    if (status !== order.prev_status) {
      const wsServer = req.app.get('wsServer');
      if (wsServer) {
        const phone10 = order.client_phone
          ? String(order.client_phone).replace(/\D/g, '').slice(-10)
          : null;
        wsServer.notifyOrderUpdate(id, status, phone10);
      }
    }
    await logAudit(req, 'status_change', 'order', id, { from: order.prev_status, to: status });
    // ─── CRM webhooks on status change (non-blocking) ────────────────────────
    if (status !== order.prev_status) {
      const { notifyCRM } = require('../services/crm');
      const updatedOrder = await get('SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id=m.id WHERE o.id=?', [id]);
      notifyCRM('order.status_changed', updatedOrder, getSetting).catch(() => {});
    }
    // ─── Notify manager(s) via email when order is confirmed ─────────────────
    if (status === 'confirmed' && status !== order.prev_status) {
      try {
        const adminEmails = mailer.getAdminEmails();
        for (const email of adminEmails) {
          mailer.sendManagerNotification(email, { ...order, id }).catch(() => {});
        }
      } catch {}
    }
    res.json({ success: true });
  } catch (e) { res.status(500).json({ error: 'DB error' }); }
});

// ─── WhatsApp deep-link for order (admin) ─────────────────────────────────────
router.post('/admin/orders/:id/whatsapp', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid ID' });

    const order = await get('SELECT id, order_number, client_name, client_phone FROM orders WHERE id = ?', [id]);
    if (!order) return res.status(404).json({ error: 'Order not found' });

    const phone = (order.client_phone || '').replace(/\D/g, '');
    // Normalize Russian 8-prefix
    const normalized = phone.length === 11 && phone.startsWith('8') ? '7' + phone.slice(1) : phone;
    if (!normalized || normalized.length < 10) {
      return res.status(400).json({ error: 'No valid phone for order' });
    }

    const { message } = req.body;
    const text = message || `Здравствуйте, ${order.client_name}! Это Nevesty Models по заявке ${order.order_number}. Менеджер на связи!`;
    const encoded = encodeURIComponent(text);
    const whatsapp_url = `https://wa.me/${normalized}?text=${encoded}`;

    res.json({ ok: true, whatsapp_url, phone: `+${normalized}` });
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

// ─── Model CSV import ─────────────────────────────────────────────────────────
router.post('/admin/models/import-csv', auth, upload.single('file'), async (req, res, next) => {
  try {
    if (!req.file) return res.status(400).json({ error: 'CSV file required' });
    const content = req.file.buffer ? req.file.buffer.toString('utf8') : fs.readFileSync(req.file.path, 'utf8');
    const lines = content.split(/\r?\n/).filter(l => l.trim());
    if (lines.length < 2) return res.status(400).json({ error: 'CSV must have header + at least 1 data row' });

    // Parse header
    const parseRow = (line) => {
      const result = []; let cur = ''; let inQuote = false;
      for (const ch of line) {
        if (ch === '"') { inQuote = !inQuote; }
        else if (ch === ',' && !inQuote) { result.push(cur.trim()); cur = ''; }
        else { cur += ch; }
      }
      result.push(cur.trim());
      return result;
    };

    const headers = parseRow(lines[0]).map(h => h.toLowerCase().replace(/[^a-z0-9_]/g, '_'));
    const ALLOWED = ['name','age','height','weight','bust','waist','hips','shoe_size','hair_color','eye_color','bio','city','category','instagram','available','photo_main'];

    let created = 0; const errors = [];
    for (let i = 1; i < lines.length; i++) {
      const row = parseRow(lines[i]);
      if (row.every(c => !c)) continue;
      const obj = {};
      headers.forEach((h, idx) => { if (ALLOWED.includes(h)) obj[h] = row[idx] || null; });
      if (!obj.name) { errors.push(`Row ${i + 1}: name required`); continue; }
      if (!ALLOWED_CATEGORIES.includes(obj.category)) obj.category = 'fashion';
      obj.available = obj.available === '0' || obj.available === 'false' || obj.available === 'нет' ? 0 : 1;
      const cols = Object.keys(obj); const vals = Object.values(obj);
      const placeholders = cols.map(() => '?').join(',');
      await run(`INSERT INTO models (${cols.join(',')}) VALUES (${placeholders})`, vals).catch(e => {
        errors.push(`Row ${i + 1}: ${e.message}`);
      });
      created++;
    }
    // Clean up temp file if multer wrote to disk
    if (req.file.path) fs.unlink(req.file.path, () => {});
    res.json({ created, errors, total: lines.length - 1 });
  } catch (e) { next(e); }
});

router.get('/export/models', auth, async (req, res, next) => {
  try {
    const models = await query('SELECT id, name, age, height, city, category, available, photo_main, bio, instagram, hair_color, eye_color, weight, bust, waist, hips, shoe_size, photos FROM models ORDER BY name');
    res.setHeader('Content-Disposition', `attachment; filename="models-${Date.now()}.json"`);
    res.json(models);
  } catch(e) { next(e); }
});

// ─── Model import (JSON body or file — JSON array / CSV) ──────────────────────
router.post('/admin/models/import', auth, upload.single('file'), async (req, res, next) => {
  try {
    let models = [];
    if (req.file) {
      const content = req.file.buffer ? req.file.buffer.toString('utf8') : fs.readFileSync(req.file.path, 'utf8');
      if (content.trim().startsWith('[')) {
        models = JSON.parse(content);
      } else {
        // Parse CSV: name,age,height,weight,category,city,bio,...
        const lines = content.split(/\r?\n/).filter(l => l.trim());
        const headers = lines[0].split(',').map(h => h.trim().toLowerCase());
        models = lines.slice(1).map(line => {
          const values = line.split(',');
          return Object.fromEntries(headers.map((h, i) => [h, values[i]?.trim() || '']));
        });
      }
      if (req.file.path) fs.unlink(req.file.path, () => {});
    } else if (req.body.models) {
      models = req.body.models;
    }

    if (!Array.isArray(models) || !models.length) {
      return res.status(400).json({ error: 'No models data provided' });
    }

    const ALLOWED_FIELDS = ['name', 'age', 'height', 'weight', 'category', 'city', 'bio', 'instagram', 'params'];
    let imported = 0;
    const errors = [];

    for (const m of models.slice(0, 50)) { // max 50 at once
      if (!m.name || !m.name.trim()) { errors.push('Missing name'); continue; }
      try {
        const fields = ALLOWED_FIELDS.filter(f => m[f] !== undefined && m[f] !== '');
        const extraFields = fields.filter(f => f !== 'name');
        const cols = ['name', 'available', ...extraFields].join(', ');
        const placeholders = ['?', '1', ...extraFields.map(() => '?')].join(', ');
        const vals = [m.name.trim(), ...extraFields.map(f => m[f])];
        await run(`INSERT INTO models (${cols}) VALUES (${placeholders})`, vals);
        imported++;
      } catch (e) {
        errors.push(`${m.name}: ${e.message}`);
      }
    }

    await logAudit(req, 'import_models', 'model', null, { imported, errors: errors.length });
    cache.delByPrefix('catalog:');
    res.json({ ok: true, imported, errors });
  } catch (e) { next(e); }
});

// ─── Bulk model operations (feature/unfeature/enable/disable/archive/restore) ──
router.patch('/admin/models/bulk', auth, async (req, res, next) => {
  try {
    const { ids, action } = req.body;
    if (!Array.isArray(ids) || !ids.length) return res.status(400).json({ error: 'ids required' });
    const validIds = ids.map(Number).filter(n => n > 0);
    if (!validIds.length) return res.status(400).json({ error: 'no valid ids' });

    const placeholders = validIds.map(() => '?').join(',');

    const actionMap = {
      'feature':   `UPDATE models SET featured=1 WHERE id IN (${placeholders})`,
      'unfeature': `UPDATE models SET featured=0 WHERE id IN (${placeholders})`,
      'enable':    `UPDATE models SET available=1, archived=0 WHERE id IN (${placeholders})`,
      'disable':   `UPDATE models SET available=0 WHERE id IN (${placeholders})`,
      'archive':   `UPDATE models SET archived=1, available=0 WHERE id IN (${placeholders})`,
      'restore':   `UPDATE models SET archived=0 WHERE id IN (${placeholders})`,
    };

    const sql = actionMap[action];
    if (!sql) return res.status(400).json({ error: 'Invalid action' });

    const result = await run(sql, validIds);
    await logAudit(req, `bulk_${action}_models`, 'model', null, { ids: validIds, changes: result.changes });
    cache.delByPrefix('catalog:');
    res.json({ ok: true, updated: result.changes });
  } catch (e) { next(e); }
});

// ─── Model CSV export ─────────────────────────────────────────────────────────
router.get('/admin/models/export', auth, async (req, res, next) => {
  try {
    const models = await query(
      `SELECT name, age, height, weight, category, city, bio, instagram, params, available, featured
       FROM models WHERE archived=0 ORDER BY name`
    );
    const BOM = '\xEF\xBB\xBF';
    const headerRow = 'Имя;Возраст;Рост;Вес;Категория;Город;Описание;Instagram;Параметры;Доступна;Топ';
    const rows = models.map(m => [
      m.name,
      m.age || '',
      m.height || '',
      m.weight || '',
      m.category || '',
      m.city || '',
      (m.bio || '').replace(/;/g, ',').replace(/\r?\n/g, ' '),
      m.instagram || '',
      m.params || '',
      m.available ? 'Да' : 'Нет',
      m.featured ? 'Да' : 'Нет'
    ].join(';'));
    res.set('Content-Type', 'text/csv; charset=utf-8');
    res.set('Content-Disposition', 'attachment; filename="models.csv"');
    res.send(BOM + headerRow + '\n' + rows.join('\n'));
  } catch (e) { next(e); }
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

// ─── Factory tasks list ───────────────────────────────────────────────────────
router.get('/admin/factory-tasks', auth, async (req, res, next) => {
  try {
    const tasks = await query(`SELECT * FROM factory_tasks ORDER BY
      CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
      created_at DESC LIMIT 50`);
    const stats = await get(`SELECT
      SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
      SUM(CASE WHEN status='done'    THEN 1 ELSE 0 END) as done,
      MAX(created_at) as last_cycle
    FROM factory_tasks`);
    res.json({ tasks, stats });
  } catch(e) { next(e); }
});

// ─── Update factory task status ───────────────────────────────────────────────
router.patch('/admin/factory-tasks/:id', auth, async (req, res, next) => {
  try {
    const { status } = req.body;
    if (!['pending', 'done', 'skipped'].includes(status))
      return res.status(400).json({ error: 'Invalid status' });
    const id = parseInt(req.params.id, 10);
    if (!Number.isFinite(id)) return res.status(400).json({ error: 'Invalid id' });
    await run(`UPDATE factory_tasks SET status=? WHERE id=?`, [status, id]);
    res.json({ success: true });
  } catch(e) { next(e); }
});

// ─── Manual factory cycle trigger ─────────────────────────────────────────────
router.post('/admin/factory/run', auth, async (req, res, next) => {
  try {
    const { spawn } = require('child_process');
    const factoryScript = '/home/user/Pablo/factory/factory_main.py';
    const proc = spawn('python3', [factoryScript, '--once'], {
      detached: true,
      stdio: 'ignore',
      env: { ...process.env }
    });
    proc.unref();
    res.json({ message: 'Цикл Factory запущен в фоне. Результаты появятся через несколько минут.' });
  } catch(e) { next(e); }
});

// ─── Factory experiments (reads factory.db directly) ──────────────────────────
router.get('/admin/factory-experiments', auth, async (req, res, next) => {
  try {
    const Database = require('better-sqlite3');
    const factoryDbPath = require('path').join(__dirname, '../../factory/factory.db');
    let rows = [];
    try {
      const fdb = new Database(factoryDbPath, { readonly: true });
      rows = fdb.prepare(`
        SELECT id, action_type, channel, metric_name, metric_baseline,
               metric_target, metric_current, outcome, evaluated_at, created_at
        FROM growth_actions
        WHERE metric_name IS NOT NULL
        ORDER BY created_at DESC LIMIT 50
      `).all();
      fdb.close();
    } catch (_) {}
    res.json(rows);
  } catch(e) { next(e); }
});

// ─── Factory channel posts (reads factory.db growth_actions for content) ─────
router.get('/admin/factory-content', auth, async (req, res, next) => {
  try {
    const Database = require('better-sqlite3');
    const factoryDbPath = require('path').join(__dirname, '../../factory/factory.db');
    let rows = [];
    try {
      const fdb = new Database(factoryDbPath, { readonly: true });
      rows = fdb.prepare(`
        SELECT id, action_type, channel, action as content, status, created_at
        FROM growth_actions
        WHERE channel = 'telegram' AND action IS NOT NULL
        ORDER BY created_at DESC LIMIT 20
      `).all();
      fdb.close();
    } catch (_) {}
    res.json(rows);
  } catch(e) { next(e); }
});

// ─── Publish factory content post to Telegram channel ────────────────────────
router.post('/admin/factory-content/:id/publish', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id, 10);
    if (!Number.isFinite(id)) return res.status(400).json({ error: 'Invalid id' });

    let channelId = await getSetting('tg_channel').catch(() => null) ||
                    await getSetting('telegram_channel_id').catch(() => null);
    if (!channelId) {
      return res.status(400).json({ error: 'Telegram channel not configured. Set tg_channel in Settings → Bot.' });
    }
    // Ensure @username format for public channels
    if (!channelId.startsWith('@') && !channelId.startsWith('-')) {
      channelId = '@' + channelId;
    }

    const Database = require('better-sqlite3');
    const factoryDbPath = require('path').join(__dirname, '../../factory/factory.db');
    let content = null;
    try {
      const fdb = new Database(factoryDbPath, { readonly: true });
      const row = fdb.prepare('SELECT action as content FROM growth_actions WHERE id=?').get(id);
      fdb.close();
      if (row) content = row.content;
    } catch (_) {}

    if (!content) return res.status(404).json({ error: 'Post not found' });

    const botRef = req.app.get('botInstance') || global._botInstance;
    if (!botRef?.instance?.sendMessage) {
      return res.status(503).json({ error: 'Bot not initialized' });
    }

    await botRef.instance.sendMessage(channelId, content, { parse_mode: 'HTML' });
    logAudit(req, 'publish_to_channel', 'factory_post', id, `channel=${channelId}`);
    res.json({ success: true, channel_id: channelId });
  } catch(e) { next(e); }
});

// ─── Factory monthly CEO report ───────────────────────────────────────────────
router.get('/admin/factory-monthly', auth, (req, res, next) => {
  try {
    const Database = require('better-sqlite3');
    const factoryDbPath = path.join(__dirname, '..', '..', 'factory', 'factory.db');
    if (!fs.existsSync(factoryDbPath)) return res.json({ report: null });
    const fdb = new Database(factoryDbPath, { readonly: true });
    const row = fdb.prepare('SELECT * FROM monthly_reports ORDER BY created_at DESC LIMIT 1').get();
    fdb.close();
    if (!row) return res.json({ report: null });
    let data = {};
    try { data = JSON.parse(row.report_json || '{}'); } catch {}
    res.json({ ...row, data });
  } catch (e) { next(e); }
});

// ─── Factory CEO decisions (reads factory.db decisions table) ─────────────────
router.get('/admin/factory-ceo-decisions', auth, (req, res, next) => {
  if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
  try {
    const Database = require('better-sqlite3');
    const factoryDbPath = path.join(__dirname, '..', '..', 'factory', 'factory.db');
    let rows = [];
    if (fs.existsSync(factoryDbPath)) {
      try {
        const fdb = new Database(factoryDbPath, { readonly: true });
        rows = fdb.prepare(`
          SELECT d.id, d.cycle_id, d.decision_type, d.rationale, d.executed, d.created_at,
                 c.health_score, c.phase as cycle_phase, c.summary as cycle_summary
          FROM decisions d
          LEFT JOIN cycles c ON c.id = d.cycle_id
          ORDER BY d.created_at DESC LIMIT 10
        `).all();
        fdb.close();
      } catch (_) {}
    }
    res.json(rows);
  } catch (e) { next(e); }
});

// ─── Factory health metrics (cycles summary) ──────────────────────────────────
router.get('/admin/factory-health', auth, (req, res, next) => {
  if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Forbidden' });
  try {
    const Database = require('better-sqlite3');
    const factoryDbPath = path.join(__dirname, '..', '..', 'factory', 'factory.db');
    const data = { total_cycles: 0, last_cycle_at: null, active_experiments: 0, pending_actions: 0, health_score: null };
    if (fs.existsSync(factoryDbPath)) {
      try {
        const fdb = new Database(factoryDbPath, { readonly: true });
        const cycleRow = fdb.prepare(`SELECT COUNT(*) as cnt, MAX(finished_at) as last_at FROM cycles WHERE phase='done'`).get();
        const lastCycle = fdb.prepare(`SELECT health_score FROM cycles WHERE phase='done' ORDER BY finished_at DESC LIMIT 1`).get();
        const expRow = fdb.prepare(`SELECT COUNT(*) as cnt FROM experiments WHERE status='running'`).get();
        const actRow = fdb.prepare(`SELECT COUNT(*) as cnt FROM growth_actions WHERE status='pending'`).get();
        fdb.close();
        data.total_cycles = cycleRow?.cnt || 0;
        data.last_cycle_at = cycleRow?.last_at || null;
        data.active_experiments = expRow?.cnt || 0;
        data.pending_actions = actRow?.cnt || 0;
        data.health_score = lastCycle?.health_score ?? null;
      } catch (_) {}
    }
    res.json(data);
  } catch (e) { next(e); }
});

// ─── Scale experiment ─────────────────────────────────────────────────────────
router.post('/admin/factory-experiments/:id/scale', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id, 10);
    if (!Number.isFinite(id)) return res.status(400).json({ error: 'Invalid id' });
    const Database = require('better-sqlite3');
    const factoryDbPath = path.join(__dirname, '..', '..', 'factory', 'factory.db');
    if (!fs.existsSync(factoryDbPath)) return res.status(404).json({ error: 'Factory DB not found' });
    const fdb = new Database(factoryDbPath);
    fdb.prepare(`UPDATE experiments SET status='scaling' WHERE id=?`).run(id);
    fdb.close();
    logAudit(req, 'scale_experiment', 'experiment', id, null);
    res.json({ success: true });
  } catch (e) { next(e); }
});

// ─── Factory REST aliases (БЛОК 5.6) ─────────────────────────────────────────
// GET /api/admin/factory/actions — growth_actions from factory.db
router.get('/admin/factory/actions', auth, async (req, res, next) => {
  try {
    const limit = Math.min(Math.max(1, parseInt(req.query.limit) || 20), 100);
    const Database = require('better-sqlite3');
    const factoryDbPath = path.join(__dirname, '..', '..', 'factory', 'factory.db');
    let actions = [];
    if (fs.existsSync(factoryDbPath)) {
      try {
        const fdb = new Database(factoryDbPath, { readonly: true });
        actions = fdb.prepare(
          'SELECT * FROM growth_actions ORDER BY created_at DESC LIMIT ?'
        ).all(limit);
        fdb.close();
      } catch (_) {}
    }
    res.json({ actions });
  } catch (e) { next(e); }
});

// GET /api/admin/factory/decisions — decisions from factory.db
router.get('/admin/factory/decisions', auth, async (req, res, next) => {
  try {
    const limit = Math.min(Math.max(1, parseInt(req.query.limit) || 10), 50);
    const Database = require('better-sqlite3');
    const factoryDbPath = path.join(__dirname, '..', '..', 'factory', 'factory.db');
    let decisions = [];
    if (fs.existsSync(factoryDbPath)) {
      try {
        const fdb = new Database(factoryDbPath, { readonly: true });
        decisions = fdb.prepare(`
          SELECT d.id, d.cycle_id, d.decision_type, d.rationale, d.executed, d.created_at,
                 c.health_score, c.phase as cycle_phase
          FROM decisions d
          LEFT JOIN cycles c ON c.id = d.cycle_id
          ORDER BY d.created_at DESC LIMIT ?
        `).all(limit);
        fdb.close();
      } catch (_) {}
    }
    res.json({ decisions });
  } catch (e) { next(e); }
});

// GET /api/admin/factory/experiments — experiments from factory.db
router.get('/admin/factory/experiments', auth, async (req, res, next) => {
  try {
    const limit = Math.min(Math.max(1, parseInt(req.query.limit) || 10), 50);
    const Database = require('better-sqlite3');
    const factoryDbPath = path.join(__dirname, '..', '..', 'factory', 'factory.db');
    let experiments = [];
    if (fs.existsSync(factoryDbPath)) {
      try {
        const fdb = new Database(factoryDbPath, { readonly: true });
        experiments = fdb.prepare(
          'SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?'
        ).all(limit);
        fdb.close();
      } catch (_) {}
    }
    res.json({ experiments });
  } catch (e) { next(e); }
});

// ── CRM Integration Webhooks (БЛОК 10.3) ──────────────────────────────────────

// POST /api/admin/crm/sync/:provider — push order to CRM (stub, real integration via .env)
router.post('/admin/crm/sync/:provider', auth, async (req, res, next) => {
  try {
    const { provider } = req.params;
    const validProviders = ['amocrm', 'bitrix24'];
    if (!validProviders.includes(provider)) {
      return res.status(400).json({ error: 'Unknown provider. Supported: amocrm, bitrix24' });
    }
    const { order_id } = req.body;
    if (!order_id) return res.status(400).json({ error: 'order_id required' });

    const order = await get('SELECT * FROM orders WHERE id = ?', [order_id]);
    if (!order) return res.status(404).json({ error: 'Order not found' });

    // Log the sync attempt (audit is best-effort)
    await logAudit(req, `crm_sync_${provider}`, 'order', order_id, { provider }).catch(() => {});

    // Stub — real integration reads WEBHOOK_URL_{PROVIDER} from .env
    res.json({
      ok: true,
      provider,
      order_id,
      external_id: `${provider}_${order_id}_stub`,
      synced_at: new Date().toISOString(),
      message: `Order synced to ${provider} (stub — configure WEBHOOK_URL_${provider.toUpperCase()} in .env to enable real sync)`
    });
  } catch (e) { next(e); }
});

// POST /api/webhooks/crm/:provider — incoming CRM webhook (status change → update order)
router.post('/webhooks/crm/:provider', async (req, res, next) => {
  try {
    const { provider } = req.params;
    const validProviders = ['amocrm', 'bitrix24'];
    if (!validProviders.includes(provider)) {
      return res.status(400).json({ error: 'Invalid provider' });
    }
    const payload = req.body;

    console.log(`[CRM Webhook] ${provider}:`, JSON.stringify(payload).substring(0, 200));

    // AmoCRM: status change payload has leads.update array
    if (provider === 'amocrm' && payload.leads) {
      const updates = payload.leads?.update || [];
      for (const lead of updates) {
        const orderIdField = lead.custom_fields?.find(f => f.name === 'order_id');
        const orderId = orderIdField?.values?.[0]?.value;
        if (orderId) {
          // AmoCRM status → internal status mapping
          const statusMap = { 142: 'confirmed', 143: 'completed', 144: 'cancelled' };
          const newStatus = statusMap[lead.status_id];
          if (newStatus) {
            await run(
              'UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
              [newStatus, orderId]
            ).catch(() => {});
          }
        }
      }
    }

    // Bitrix24: deal status change
    if (provider === 'bitrix24' && payload.event === 'ONCRMDEALSTAGESET') {
      const dealId = payload.data?.FIELDS_BEFORE?.ID;
      const stageId = payload.data?.FIELDS_AFTER?.STAGE_ID;
      if (dealId && stageId) {
        // Bitrix24 stage → internal status mapping (customise to match your pipeline)
        const stageMap = { 'WON': 'completed', 'LOSE': 'cancelled', 'C2:PREPARATION': 'confirmed' };
        const newStatus = stageMap[stageId];
        if (newStatus) {
          await run(
            'UPDATE orders SET status=?, updated_at=CURRENT_TIMESTAMP WHERE payment_id=?',
            [newStatus, String(dealId)]
          ).catch(() => {});
        }
      }
    }

    res.json({ ok: true, received: true });
  } catch (e) { next(e); }
});

// ─── DB stats endpoint ────────────────────────────────────────────────────────
router.get('/admin/crm-status', auth, (req, res) => {
  res.json({
    generic:  !!process.env.CRM_WEBHOOK_URL,
    amocrm:   !!process.env.AMOCRM_WEBHOOK_URL,
    bitrix24: !!process.env.BITRIX24_WEBHOOK_URL,
  });
});

router.get('/admin/db-stats', auth, async (req, res, next) => {
  try {
    const tables = await query(`
      SELECT name, (SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND tbl_name=sm.name) as index_count
      FROM sqlite_master sm WHERE type='table' AND name NOT LIKE 'sqlite_%'
      ORDER BY name`);

    const tableCounts = await Promise.all(tables.map(async t => ({
      name: t.name,
      count: (await get(`SELECT COUNT(*) as cnt FROM "${t.name}"`))?.cnt || 0,
      indexes: t.index_count
    })));

    const walInfo = await get('PRAGMA wal_checkpoint(PASSIVE)');
    const dbSize = await get(`SELECT page_count * page_size as size FROM pragma_page_count(), pragma_page_size()`);
    const schemaVers = await query('SELECT * FROM schema_versions ORDER BY version DESC LIMIT 5').catch(()=>[]);

    res.json({
      tables: tableCounts,
      wal: walInfo,
      size_bytes: dbSize?.size || 0,
      size_mb: Math.round((dbSize?.size || 0) / 1024 / 1024 * 100) / 100,
      schema_versions: schemaVers
    });
  } catch (e) { next(e); }
});

// ─── Manual VACUUM endpoint ───────────────────────────────────────────────────
router.post('/admin/db-vacuum', auth, async (req, res, next) => {
  try {
    await run('VACUUM');
    await run('ANALYZE');
    res.json({ success: true, message: 'Database vacuumed and analyzed' });
  } catch (e) { next(e); }
});

// ─── Cache stats & control (admin) ────────────────────────────────────────────
// GET  /api/admin/cache/stats  → hit/miss/keys count
// DELETE /api/admin/cache      → clear entire in-memory cache
router.get('/admin/cache/stats', auth, (req, res) => {
  res.json(cache.stats());
});

router.delete('/admin/cache', auth, (req, res) => {
  cache.clear();
  res.json({ ok: true, message: 'Cache cleared' });
});

// ─── Analytics: KPI ──────────────────────────────────────────────────────────
router.get('/admin/analytics/kpi', auth, async (req, res, next) => {
  try {
    const days = Math.min(365, Math.max(1, parseInt(req.query.days) || 30));
    const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();

    const [total, completed, active, new_clients, statuses] = await Promise.all([
      get(`SELECT COUNT(*) as cnt FROM orders WHERE created_at >= ?`, [since]),
      get(`SELECT COUNT(*) as cnt FROM orders WHERE status='completed' AND created_at >= ?`, [since]),
      get(`SELECT COUNT(*) as cnt FROM orders WHERE status IN ('new','reviewing','confirmed','in_progress')`),
      get(`SELECT COUNT(DISTINCT client_chat_id) as cnt FROM orders WHERE created_at >= ? AND client_chat_id IS NOT NULL`, [since]),
      query(`SELECT status, COUNT(*) as cnt FROM orders WHERE created_at >= ? GROUP BY status`, [since]),
    ]);

    const statusMap = {};
    statuses.forEach(s => { statusMap[s.status] = s.cnt; });

    res.json({
      total: total?.cnt || 0,
      completed: completed?.cnt || 0,
      active: active?.cnt || 0,
      new_clients: new_clients?.cnt || 0,
      ...Object.fromEntries(Object.entries(statusMap).map(([k, v]) => [k + '_count', v]))
    });
  } catch (e) { next(e); }
});

// ─── Analytics: Conversion funnel ────────────────────────────────────────────
router.get('/admin/analytics/funnel', auth, async (req, res, next) => {
  try {
    const days = Math.min(365, Math.max(1, parseInt(req.query.days) || 30));
    const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
    const statuses = ['new', 'reviewing', 'confirmed', 'in_progress', 'completed'];
    const labelMap = {
      new: '🆕 Новые',
      reviewing: '🔍 На рассмотрении',
      confirmed: '✅ Подтверждены',
      in_progress: '🔄 В работе',
      completed: '🏁 Завершены'
    };
    const [stages, cancelledRow, viewRow] = await Promise.all([
      Promise.all(statuses.map(async s => ({
        label: labelMap[s],
        status: s,
        count: (await get(`SELECT COUNT(*) as cnt FROM orders WHERE status=? AND created_at >= ?`, [s, since]))?.cnt || 0
      }))),
      get(`SELECT COUNT(*) as cnt FROM orders WHERE status='cancelled' AND created_at >= ?`, [since]),
      get(`SELECT COALESCE(SUM(view_count), 0) as total_views FROM models WHERE archived=0`),
    ]);
    const cancelled = cancelledRow?.cnt || 0;
    const total = stages.reduce((s, st) => s + st.count, 0) + cancelled;
    const completed = stages.find(s => s.status === 'completed')?.count || 0;
    const conversion_rate = total > 0 ? Math.round((completed / total) * 100) : 0;
    const viewCount = viewRow?.total_views || 0;
    // Prepend model views as top-of-funnel stage
    const allStages = viewCount > 0
      ? [{ label: '👁 Просмотры моделей', status: 'views', count: viewCount }, ...stages]
      : stages;
    res.json({ stages: allStages, cancelled, total, conversion_rate, view_count: viewCount });
  } catch (e) { next(e); }
});

// ─── Analytics: Top models ────────────────────────────────────────────────────
router.get('/admin/analytics/top-models', auth, async (req, res, next) => {
  try {
    const days = Math.min(365, Math.max(1, parseInt(req.query.days) || 30));
    const limit = Math.min(20, Math.max(1, parseInt(req.query.limit) || 5));
    const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
    const models = await query(`
      SELECT m.id, m.name, m.city, m.category, m.view_count as views,
        COUNT(o.id) AS orders,
        SUM(CASE WHEN o.status='completed' THEN 1 ELSE 0 END) AS completed,
        AVG(COALESCE(CAST(REPLACE(REPLACE(o.budget,'₽',''),' ','') AS INTEGER), 0)) AS avg_budget
      FROM models m
      LEFT JOIN orders o ON m.id = o.model_id AND o.created_at >= ?
      WHERE m.archived = 0
      GROUP BY m.id
      ORDER BY completed DESC, orders DESC, views DESC
      LIMIT ?`, [since, limit]);
    res.json({ models: models.map(m => ({ ...m, avg_budget: Math.round(m.avg_budget || 0) })) });
  } catch (e) { next(e); }
});

// ─── Analytics: Event types distribution ─────────────────────────────────────
router.get('/admin/analytics/event-types', auth, async (req, res, next) => {
  try {
    const days = Math.min(365, Math.max(1, parseInt(req.query.days) || 30));
    const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
    const types = await query(
      `SELECT event_type as type, COUNT(*) as count FROM orders WHERE created_at >= ? GROUP BY event_type ORDER BY count DESC`,
      [since]
    );
    res.json({ types });
  } catch (e) { next(e); }
});

// ─── Analytics: Order sources (utm_source) ────────────────────────────────────
router.get('/admin/analytics/sources', auth, async (req, res, next) => {
  try {
    const days = Math.min(365, Math.max(1, parseInt(req.query.days) || 30));
    const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
    const sources = await query(
      `SELECT COALESCE(NULLIF(utm_source,''), 'unknown') as source, COUNT(*) as count FROM orders WHERE created_at >= ? GROUP BY source ORDER BY count DESC`,
      [since]
    );
    res.json({ sources });
  } catch (e) { next(e); }
});

// ─── Monthly analytics trend ──────────────────────────────────────────────────
router.get('/admin/analytics/monthly', auth, async (req, res, next) => {
  try {
    const months = Math.min(24, Math.max(3, parseInt(req.query.months) || 12));
    const rows = await query(`
      SELECT
        strftime('%Y-%m', created_at) as month,
        COUNT(*) as orders_count,
        SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
        SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled,
        SUM(CASE WHEN budget IS NOT NULL AND budget != '' THEN CAST(budget AS INTEGER) ELSE 0 END) as revenue
      FROM orders
      WHERE created_at >= date('now', '-' || ? || ' months')
      GROUP BY month
      ORDER BY month ASC
    `, [months]);
    res.json({ months: rows, count: rows.length });
  } catch(e) { next(e); }
});

// GET /admin/analytics/extended — top cities, repeat clients rate, avg cycle
router.get('/admin/analytics/extended', auth, async (req, res, next) => {
  try {
    const days = Math.min(365, Math.max(1, parseInt(req.query.days) || 30));
    const since = new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();

    // Top cities by order count
    const topCities = await query(
      `SELECT m.city, COUNT(o.id) as cnt
       FROM orders o JOIN models m ON o.model_id = m.id
       WHERE m.city IS NOT NULL AND m.city != '' AND o.created_at >= ?
       GROUP BY m.city ORDER BY cnt DESC LIMIT 5`,
      [since]
    );

    // Repeat clients (clients with > 1 order)
    const repeatRow = await get(
      `SELECT
         COUNT(DISTINCT client_chat_id) as repeat_clients,
         (SELECT COUNT(DISTINCT client_chat_id) FROM orders WHERE client_chat_id IS NOT NULL) as total_clients
       FROM orders
       WHERE client_chat_id IN (
         SELECT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL GROUP BY client_chat_id HAVING COUNT(*) > 1
       )`
    );
    const repeatRate = repeatRow && repeatRow.total_clients > 0
      ? Math.round((repeatRow.repeat_clients / repeatRow.total_clients) * 100)
      : 0;

    // Average deal cycle in days (new → completed)
    const cycleRow = await get(
      `SELECT AVG(CAST(julianday(updated_at) - julianday(created_at) AS REAL)) as avg_days
       FROM orders WHERE status='completed' AND updated_at IS NOT NULL AND created_at IS NOT NULL`
    );
    const avgCycleDays = cycleRow && cycleRow.avg_days ? Math.round(cycleRow.avg_days * 10) / 10 : null;

    // Rating average from approved reviews
    const ratingRow = await get(`SELECT AVG(rating) as avg_rating, COUNT(*) as cnt FROM reviews WHERE approved=1`);

    // Average budget (mid-range estimate) from confirmed/completed/in_progress orders
    const avgBudgetRow = await get(
      `SELECT ROUND(AVG(CAST(budget AS REAL)), 0) as avg
       FROM orders
       WHERE status IN ('confirmed','completed','in_progress')
         AND budget IS NOT NULL AND budget != '' AND CAST(budget AS REAL) > 0
         AND created_at >= ?`,
      [since]
    );

    res.json({
      top_cities: topCities,
      repeat_rate: repeatRate,
      repeat_clients: repeatRow?.repeat_clients || 0,
      total_clients: repeatRow?.total_clients || 0,
      avg_cycle_days: avgCycleDays,
      avg_rating: ratingRow?.avg_rating ? Math.round(ratingRow.avg_rating * 10) / 10 : null,
      reviews_count: ratingRow?.cnt || 0,
      avg_budget: avgBudgetRow?.avg ? Math.round(avgBudgetRow.avg) : null,
    });
  } catch (e) { next(e); }
});

// ─── Analytics: Revenue by month ─────────────────────────────────────────────
router.get('/admin/analytics/revenue', auth, async (req, res) => {
  try {
    const months = Math.min(24, Math.max(1, parseInt(req.query.months) || 6));
    const rows = await query(`
      SELECT
        strftime('%Y-%m', created_at) as month,
        SUM(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)) as revenue,
        COUNT(*) as order_count
      FROM orders
      WHERE status IN ('confirmed','completed')
        AND budget GLOB '[0-9]*'
        AND created_at >= datetime('now','-' || ? || ' months')
      GROUP BY strftime('%Y-%m', created_at)
      ORDER BY month DESC
      LIMIT ?
    `, [months, months]);
    res.json({ ok: true, months: rows.reverse(), data: rows });
  } catch (e) { res.json({ ok: false, error: e.message }); }
});

// ─── Analytics: Repeat vs new clients ────────────────────────────────────────
router.get('/admin/analytics/repeat-clients', auth, async (req, res) => {
  try {
    const [total, repeat] = await Promise.all([
      get(`SELECT COUNT(DISTINCT client_chat_id) as n FROM orders WHERE client_chat_id IS NOT NULL`),
      get(`SELECT COUNT(*) as n FROM (SELECT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL GROUP BY client_chat_id HAVING COUNT(*) > 1)`),
    ]);
    const newClients = (total?.n || 0) - (repeat?.n || 0);
    res.json({ ok: true, data: { total: total?.n || 0, repeat: repeat?.n || 0, new: newClients } });
  } catch (e) { res.json({ ok: false, error: e.message }); }
});

// ─── Client Cabinet ───────────────────────────────────────────────────────────
// Rate-limit store: { ip: [timestamps] }
const _clientRateLimits = new Map();

function clientRateLimit(req, res, next) {
  const ip = req.ip || req.connection?.remoteAddress || 'unknown';
  const now = Date.now();
  const windowMs = 60 * 60 * 1000; // 1 hour
  const maxReqs = 10;
  const timestamps = (_clientRateLimits.get(ip) || []).filter(t => now - t < windowMs);
  if (timestamps.length >= maxReqs) {
    return res.status(429).json({ error: 'Слишком много запросов. Попробуйте через час.' });
  }
  timestamps.push(now);
  _clientRateLimits.set(ip, timestamps);
  next();
}

function normalizePhone(raw) {
  if (!raw || typeof raw !== 'string') return null;
  const digits = raw.replace(/\D/g, '');
  // Remove leading 7 or 8 (Russia) to get 10 digits
  if (digits.length === 11 && (digits[0] === '7' || digits[0] === '8')) {
    return digits.slice(1);
  }
  if (digits.length === 10) return digits;
  return null;
}

// ─── Client OTP auth ──────────────────────────────────────────────────────────
const clientOtpLimiter = (() => {
  try {
    const rateLimit = require('express-rate-limit');
    return rateLimit({ windowMs: 60 * 1000, max: 3, standardHeaders: true, legacyHeaders: false,
      message: { error: 'Слишком много запросов. Повторите через минуту.' } });
  } catch { return (req, res, next) => next(); }
})();

// POST /api/client/request-code — send OTP to phone
router.post('/client/request-code', clientOtpLimiter, async (req, res, next) => {
  try {
    const rawPhone = (req.body.phone || '').trim();
    const phone10 = normalizePhone(rawPhone);
    if (!phone10) return res.status(400).json({ error: 'Укажите корректный номер телефона' });

    // Check if phone has any orders in the system
    const patterns = [phone10, '7' + phone10, '+7' + phone10, '8' + phone10];
    const ph = patterns.map(() => '?').join(',');
    const order = await get(`SELECT id FROM orders WHERE client_phone IN (${ph}) LIMIT 1`, patterns);
    if (!order) return res.status(404).json({ error: 'Заявки с этим номером не найдены' });

    // Generate 6-digit code
    const code = String(Math.floor(100000 + Math.random() * 900000));
    // Expire old codes for this phone
    await run("DELETE FROM client_otp WHERE phone=?", [phone10]).catch(() => {});
    await run(
      "INSERT INTO client_otp (phone, code, expires_at) VALUES (?, ?, datetime('now', '+10 minutes'))",
      [phone10, code]
    );

    // Try to send SMS
    const sms = require('../services/sms');
    const phoneE164 = '+7' + phone10;
    await sms.sendSMS(phoneE164, `Ваш код для входа в личный кабинет Nevesty Models: ${code}. Действует 10 минут.`)
      .catch(e => console.log('[OTP] SMS send skipped:', e.message));

    // In dev/test — return code (only if no SMS API configured)
    const isDev = !process.env.SMS_RU_API_ID;
    res.json({ ok: true, ...(isDev ? { code_debug: code } : {}) });
  } catch (e) { next(e); }
});

// POST /api/client/verify — verify OTP and get session token
router.post('/client/verify', clientOtpLimiter, async (req, res, next) => {
  try {
    const rawPhone = (req.body.phone || '').trim();
    const code = (req.body.code || '').trim();
    const phone10 = normalizePhone(rawPhone);
    if (!phone10 || !code) return res.status(400).json({ error: 'Укажите телефон и код' });

    const otp = await get(
      "SELECT * FROM client_otp WHERE phone=? AND used=0 AND expires_at > CURRENT_TIMESTAMP ORDER BY created_at DESC LIMIT 1",
      [phone10]
    );
    if (!otp) return res.status(401).json({ error: 'Код не найден или истёк. Запросите новый.' });

    // Increment attempts
    await run("UPDATE client_otp SET attempts=attempts+1 WHERE id=?", [otp.id]);
    if (otp.attempts >= 5) {
      await run("UPDATE client_otp SET used=1 WHERE id=?", [otp.id]);
      return res.status(429).json({ error: 'Превышено число попыток. Запросите новый код.' });
    }

    if (otp.code !== code) return res.status(401).json({ error: 'Неверный код' });

    // Mark used
    await run("UPDATE client_otp SET used=1 WHERE id=?", [otp.id]);

    // Issue short-lived client JWT (1 hour)
    const token = jwt.sign(
      { phone: phone10, type: 'client' },
      process.env.JWT_SECRET || 'secret',
      { expiresIn: '1h' }
    );
    res.json({ ok: true, token, phone: '+7' + phone10 });
  } catch (e) { next(e); }
});

// Client auth middleware (reserved for future JWT-protected client routes)
function _clientAuth(req, res, next) {
  const header = req.headers.authorization || '';
  if (!header.startsWith('Bearer ')) return res.status(401).json({ error: 'Требуется авторизация' });
  try {
    const payload = jwt.verify(header.slice(7), process.env.JWT_SECRET || 'secret');
    if (payload.type !== 'client') return res.status(401).json({ error: 'Неверный тип токена' });
    req.clientPhone = payload.phone;
    next();
  } catch { return res.status(401).json({ error: 'Токен недействителен или истёк' }); }
}

// GET /api/client/orders?phone=79991234567
router.get('/client/orders', clientRateLimit, async (req, res, next) => {
  try {
    const rawPhone = (req.query.phone || '').trim();
    const phone10 = normalizePhone(rawPhone);
    if (!phone10) {
      return res.status(400).json({ error: 'Укажите корректный номер телефона (10 цифр)' });
    }

    // Match stored phone against all common formats
    // Stored phone could be: +79991234567 / 89991234567 / 9991234567 / 79991234567
    const patterns = [
      phone10,           // 9991234567
      '7' + phone10,     // 79991234567
      '+7' + phone10,    // +79991234567
      '8' + phone10,     // 89991234567
    ];
    const placeholders = patterns.map(() => '?').join(',');

    const EVENT_RU = {
      fashion_show: 'Показ мод',
      photo_shoot: 'Фотосессия',
      event: 'Мероприятие',
      commercial: 'Коммерческая съёмка',
      runway: 'Подиум',
      other: 'Другое'
    };

    const orders = await query(
      `SELECT o.id, o.order_number, o.created_at, o.event_type, o.event_date,
              o.budget, o.status, o.model_id, o.comments, o.location,
              m.name as model_name, m.photo_main as model_photo
       FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       WHERE REPLACE(REPLACE(REPLACE(REPLACE(o.client_phone, '+', ''), '-', ''), ' ', ''), '(', '') IN (${placeholders})
          OR REPLACE(REPLACE(REPLACE(REPLACE(o.client_phone, ')', ''), '-', ''), ' ', ''), '(', '') IN (${placeholders})
       GROUP BY o.id
       ORDER BY o.created_at DESC`,
      [...patterns, ...patterns]
    );

    if (!orders.length) {
      return res.status(404).json({ error: 'Заявки не найдены. Проверьте номер телефона.' });
    }

    // Add human-readable event type
    const result = orders.map(o => ({
      ...o,
      event_type_ru: EVENT_RU[o.event_type] || o.event_type,
    }));

    res.json({ orders: result, total: result.length });
  } catch (e) { next(e); }
});

// POST /api/client/review
router.post('/client/review', clientRateLimit, async (req, res, next) => {
  try {
    const { order_id, phone, rating, text } = req.body;

    // Validate inputs
    if (!order_id || !Number.isInteger(parseInt(order_id, 10))) {
      return res.status(400).json({ error: 'Укажите ID заявки' });
    }
    const rawPhone = (phone || '').trim();
    const phone10 = normalizePhone(rawPhone);
    if (!phone10) {
      return res.status(400).json({ error: 'Укажите корректный номер телефона' });
    }
    const ratingNum = parseInt(rating, 10);
    if (!ratingNum || ratingNum < 1 || ratingNum > 5) {
      return res.status(400).json({ error: 'Оценка должна быть от 1 до 5' });
    }
    const reviewText = sanitize(text, 2000);
    if (!reviewText || reviewText.length < 10) {
      return res.status(400).json({ error: 'Отзыв должен содержать минимум 10 символов' });
    }

    const patterns = [phone10, '7' + phone10, '+7' + phone10, '8' + phone10];
    const placeholders = patterns.map(() => '?').join(',');

    // Fetch the order and verify ownership
    const orderId = parseInt(order_id, 10);
    const order = await get(
      `SELECT o.id, o.status, o.client_name, o.client_phone, o.model_id, o.order_number
       FROM orders o
       WHERE o.id = ?
         AND (REPLACE(REPLACE(REPLACE(REPLACE(o.client_phone, '+', ''), '-', ''), ' ', ''), '(', '') IN (${placeholders})
           OR REPLACE(REPLACE(REPLACE(REPLACE(o.client_phone, ')', ''), '-', ''), ' ', ''), '(', '') IN (${placeholders}))`,
      [orderId, ...patterns, ...patterns]
    );

    if (!order) {
      return res.status(403).json({ error: 'Заявка не найдена или не принадлежит этому телефону' });
    }
    if (order.status !== 'completed') {
      return res.status(400).json({ error: 'Отзыв можно оставить только для завершённых заявок' });
    }

    // Check for duplicate review on same order
    const existing = await get('SELECT id FROM reviews WHERE order_id = ?', [orderId]).catch(() => null);
    if (existing) {
      return res.status(409).json({ error: 'Вы уже оставили отзыв на эту заявку' });
    }

    // Insert review
    const result = await run(
      `INSERT INTO reviews (client_name, rating, text, model_id, approved, order_id)
       VALUES (?, ?, ?, ?, 0, ?)`,
      [order.client_name, ratingNum, reviewText, order.model_id || null, orderId]
    );

    // Notify admin via bot
    if (botInstance?.notifyAdmin) {
      const stars = '⭐'.repeat(ratingNum);
      botInstance.notifyAdmin(
        `💬 *Новый отзыв* \\(заявка ${escMd(order.order_number)}\\)\n${stars}\n_${escMd(reviewText.slice(0, 200))}_`,
        { parse_mode: 'MarkdownV2' }
      ).catch(() => {});
    }

    res.json({ ok: true, id: result.id });
  } catch (e) { next(e); }
});

// ─── Contact form ─────────────────────────────────────────────────────────────
// POST /api/contact — {name, phone, message, email?}
// Rate limit: 3 requests per hour per IP
router.post('/contact', contactRateLimit, async (req, res, next) => {
  try {
    const { name, phone, message, email } = req.body;
    if (!sanitize(name, 100)) return res.status(400).json({ error: 'Укажите ваше имя' });
    if (!phone || !validatePhone(phone)) return res.status(400).json({ error: 'Укажите корректный номер телефона' });
    if (email && !validateEmail(email)) return res.status(400).json({ error: 'Некорректный email' });
    if (!sanitize(message, 2000)) return res.status(400).json({ error: 'Укажите сообщение' });

    const order_number = generateOrderNumber();
    const s = {
      client_name:  sanitize(name, 100),
      client_phone: phone.trim().slice(0, 20),
      client_email: sanitize(email, 100),
      comments:     sanitize(message, 2000),
    };

    const result = await run(
      `INSERT INTO orders (order_number, client_name, client_phone, client_email, event_type, comments, utm_source)
       VALUES (?, ?, ?, ?, 'other', ?, 'contact_form')`,
      [order_number, s.client_name, s.client_phone, s.client_email, s.comments]
    );

    const formData = { name: s.client_name, phone: s.client_phone, email: s.client_email, message: s.comments };

    if (botInstance) {
      const orderForBot = { id: result.id, order_number, ...s, event_type: 'other', utm_source: 'contact_form' };
      botInstance.notifyNewOrder(orderForBot).catch(e => console.error('Bot notify contact form:', e.message));
    }

    // Also send direct Telegram notification to admin IDs (if bot token configured and botInstance unavailable)
    if (!botInstance) {
      const tgToken = process.env.TELEGRAM_BOT_TOKEN;
      const adminIds = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(x => x.trim()).filter(Boolean);
      if (tgToken && adminIds.length) {
        const tgText = `📬 Новый контакт!\nИмя: ${s.client_name}\nТелефон: ${s.client_phone}\nСообщение: ${s.comments}`;
        for (const adminId of adminIds) {
          fetch(`https://api.telegram.org/bot${tgToken}/sendMessage`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chat_id: adminId, text: tgText }),
          }).catch(e => console.error('[contact] tg direct notify:', e.message));
        }
      }
    }

    const adminEmails = mailer.getAdminEmails();
    for (const adminEmail of adminEmails) {
      mailer.sendContactFormEmail(adminEmail, formData).catch(e => console.error('[mailer] contact form error:', e.message));
    }

    res.json({ ok: true, message: 'Сообщение отправлено! Мы свяжемся в течение рабочего дня.', order_number, id: result.id });
  } catch (e) { next(e); }
});

// ─── Email test endpoints (admin) ─────────────────────────────────────────────
router.get('/admin/email/test', auth, async (req, res, next) => {
  try {
    const configured = !!(process.env.SMTP_HOST && process.env.SMTP_USER && process.env.SMTP_PASS);
    res.json({
      configured,
      smtp_host: process.env.SMTP_HOST || null,
      smtp_port: process.env.SMTP_PORT || '587',
      smtp_user: process.env.SMTP_USER ? process.env.SMTP_USER.replace(/(.{2})(.*)(@.*)/, '***') : null,
      smtp_from: process.env.SMTP_FROM || null,
      admin_emails: mailer.getAdminEmails().map(e => e.replace(/(.{2})(.*)(@.*)/, '***')),
    });
  } catch (e) { next(e); }
});

router.post('/admin/email/test', auth, async (req, res, next) => {
  try {
    const admin = await get('SELECT email FROM admins WHERE id = ?', [req.admin.id]);
    const toEmail = req.body.email || admin?.email;
    if (!toEmail) return res.status(400).json({ error: 'Укажите email или добавьте email в профиль' });
    const result = await mailer.sendTestEmail(toEmail);
    if (result.ok) {
      res.json({ ok: true, message: `Тестовое письмо отправлено на ${toEmail}` });
    } else {
      res.status(500).json({ ok: false, error: result.error || 'Ошибка отправки' });
    }
  } catch (e) { next(e); }
});


// ─────────────────────────────────────────────────────────────────────────────
// PAYMENT ROUTES
// ─────────────────────────────────────────────────────────────────────────────

// Helper: look up payment_provider from bot_settings
async function getPaymentProvider() {
  const row = await get('SELECT value FROM bot_settings WHERE key=?', ['payment_provider']).catch(() => null);
  return row?.value || 'disabled';
}

// POST /api/orders/:id/pay — create payment for an order
// Auth: admin JWT  OR  client phone token
router.post('/orders/:id/pay', async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid order ID' });

    let authorized = false;
    const authHeader = req.headers.authorization || '';
    if (authHeader.startsWith('Bearer ')) {
      try {
        jwt.verify(authHeader.slice(7), process.env.JWT_SECRET || 'secret');
        authorized = true;
      } catch {}
    }

    const { phone, return_url } = req.body;
    const order = await get('SELECT * FROM orders WHERE id=?', [id]);
    if (!order) return res.status(404).json({ error: 'Заявка не найдена' });

    if (!authorized && phone) {
      const clean = (p) => p.replace(/\D/g, '').replace(/^[78]/, '');
      const clientClean = clean(String(phone));
      const orderClean  = clean(order.client_phone || '');
      if (clientClean.length >= 7 && clientClean === orderClean) authorized = true;
    }

    if (!authorized) return res.status(401).json({ error: 'Необходима авторизация' });

    const provider = await getPaymentProvider();
    if (provider === 'disabled' || !provider) {
      return res.status(400).json({ error: 'Оплата не настроена' });
    }

    const siteUrl     = process.env.SITE_URL || 'http://localhost:3000';
    const returnUrl   = return_url || `${siteUrl}/order-status.html?id=${order.id}`;
    const description = `Оплата заявки ${order.order_number}`;
    const amountStr   = (order.budget || '').replace(/[^\d]/g, '');
    const amount      = amountStr ? Math.max(1, parseInt(amountStr, 10)) : 1000;

    let result;
    if (provider === 'yookassa') {
      result = await payment.createYooKassaPayment(order.id, amount, description, returnUrl);
    } else if (provider === 'stripe') {
      result = await payment.createStripePayment(order.id, amount, description, 'rub');
    } else {
      return res.status(400).json({ error: `Неизвестный провайдер: ${provider}` });
    }

    if (result.error) return res.status(502).json({ error: result.error });

    await run(
      'UPDATE orders SET payment_id=?, payment_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
      [result.payment_id, 'pending', id]
    );

    res.json({
      payment_url:   result.payment_url || null,
      payment_id:    result.payment_id,
      client_secret: result.client_secret || null,
      provider,
    });
  } catch (e) { next(e); }
});

// POST /api/webhooks/yookassa
router.post('/webhooks/yookassa', async (req, res, next) => {
  try {
    const body  = req.body;
    const event = Buffer.isBuffer(body) ? JSON.parse(body.toString('utf8')) : body;

    if (event?.event === 'payment.succeeded') {
      const paymentId   = event?.object?.id;
      const metaOrderId = event?.object?.metadata?.order_id;
      if (paymentId) {
        const ord = metaOrderId
          ? await get('SELECT * FROM orders WHERE id=?', [parseInt(metaOrderId)]).catch(() => null)
          : await get('SELECT * FROM orders WHERE payment_id=?', [paymentId]).catch(() => null);
        if (ord) {
          await run(
            `UPDATE orders SET payment_status='paid', paid_at=CURRENT_TIMESTAMP,
             status='confirmed', updated_at=CURRENT_TIMESTAMP WHERE id=?`,
            [ord.id]
          );
          await run(
            'INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, notes) VALUES (?,?,?,?,?)',
            [ord.id, ord.status, 'confirmed', 'yookassa_webhook', `Payment ID: ${paymentId}`]
          ).catch(() => {});
          if (botInstance && ord.client_chat_id && typeof botInstance.notifyPaymentSuccess === 'function') {
            botInstance.notifyPaymentSuccess(ord.client_chat_id, ord.order_number).catch(() => {});
          }
          if (botInstance?.notifyAdmin) {
            botInstance.notifyAdmin(
              `💳 *Оплата получена\\!* Заявка ${escMd(ord.order_number)}\nКлиент: ${escMd(ord.client_name)}`,
              { parse_mode: 'MarkdownV2' }
            ).catch(() => {});
          }
        }
      }
    }

    if (event?.event === 'payment.canceled') {
      const paymentId   = event?.object?.id;
      const metaOrderId = event?.object?.metadata?.order_id;
      if (paymentId) {
        const ord = metaOrderId
          ? await get('SELECT * FROM orders WHERE id=?', [parseInt(metaOrderId)]).catch(() => null)
          : await get('SELECT * FROM orders WHERE payment_id=?', [paymentId]).catch(() => null);
        if (ord) {
          await run(
            `UPDATE orders SET payment_status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?`,
            [ord.id]
          );
        }
      }
    }

    res.json({ ok: true });
  } catch (e) { next(e); }
});

// POST /api/webhooks/stripe
router.post('/webhooks/stripe', async (req, res, next) => {
  try {
    const sig  = req.headers['stripe-signature'] || '';
    const body = req.body;

    if (process.env.STRIPE_WEBHOOK_SECRET && sig) {
      const rawBody = Buffer.isBuffer(body) ? body : Buffer.from(JSON.stringify(body));
      if (!payment.verifyStripeWebhook(rawBody, sig)) {
        return res.status(403).json({ error: 'Invalid Stripe signature' });
      }
    }

    const event = Buffer.isBuffer(body) ? JSON.parse(body.toString('utf8')) : body;

    // Helper: mark order as paid and notify
    const markPaid = async (ord, ref) => {
      await run(
        `UPDATE orders SET payment_status='paid', paid_at=CURRENT_TIMESTAMP,
         status='confirmed', updated_at=CURRENT_TIMESTAMP WHERE id=?`,
        [ord.id]
      );
      await run(
        'INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, notes) VALUES (?,?,?,?,?)',
        [ord.id, ord.status, 'confirmed', 'stripe_webhook', `Ref: ${ref}`]
      ).catch(() => {});
      if (botInstance && ord.client_chat_id && typeof botInstance.notifyPaymentSuccess === 'function') {
        botInstance.notifyPaymentSuccess(ord.client_chat_id, ord.order_number).catch(() => {});
      }
      if (botInstance?.notifyAdmin) {
        botInstance.notifyAdmin(
          `💳 *Оплата Stripe получена\\!* Заявка ${escMd(ord.order_number)}\nКлиент: ${escMd(ord.client_name)}`,
          { parse_mode: 'MarkdownV2' }
        ).catch(() => {});
      }
    };

    if (event?.type === 'payment_intent.succeeded') {
      const pi          = event?.data?.object;
      const paymentId   = pi?.id;
      const metaOrderId = pi?.metadata?.order_id;
      if (paymentId) {
        const ord = metaOrderId
          ? await get('SELECT * FROM orders WHERE id=?', [parseInt(metaOrderId)]).catch(() => null)
          : await get('SELECT * FROM orders WHERE payment_id=?', [paymentId]).catch(() => null);
        if (ord) await markPaid(ord, paymentId);
      }
    }

    if (event?.type === 'checkout.session.completed') {
      const session   = event?.data?.object;
      const sessionId = session?.id;
      if (sessionId) {
        const ord = await get('SELECT * FROM orders WHERE payment_id=?', [sessionId]).catch(() => null);
        if (ord) await markPaid(ord, sessionId);
      }
    }

    if (event?.type === 'payment_intent.payment_failed') {
      const pi        = event?.data?.object;
      const paymentId = pi?.id;
      if (paymentId) {
        const ord = await get('SELECT * FROM orders WHERE payment_id=?', [paymentId]).catch(() => null);
        if (ord) {
          await run("UPDATE orders SET payment_status='failed', updated_at=CURRENT_TIMESTAMP WHERE id=?", [ord.id]);
        }
      }
    }

    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── Admin FAQ CRUD ───────────────────────────────────────────────────────────

// GET /admin/faq — list all FAQ items
router.get('/admin/faq', auth, async (req, res, next) => {
  try {
    const items = await query('SELECT * FROM faq ORDER BY sort_order ASC, id ASC');
    res.json(items);
  } catch (e) { next(e); }
});

// POST /admin/faq — create FAQ item
router.post('/admin/faq', auth, async (req, res, next) => {
  try {
    const { question, answer, sort_order = 0 } = req.body;
    if (!question || !answer) return res.status(400).json({ error: 'question and answer required' });
    const result = await run(
      'INSERT INTO faq (question, answer, sort_order) VALUES (?, ?, ?)',
      [question.slice(0, 500), answer.slice(0, 2000), parseInt(sort_order) || 0]
    );
    res.json({ ok: true, id: result.lastID });
  } catch (e) { next(e); }
});

// PUT /admin/faq/:id — update FAQ item (supports partial updates)
router.put('/admin/faq/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid id' });
    const existing = await get('SELECT * FROM faq WHERE id=?', [id]);
    if (!existing) return res.status(404).json({ error: 'Not found' });
    const { question, answer, sort_order, active } = req.body;
    await run(
      'UPDATE faq SET question=?, answer=?, sort_order=?, active=? WHERE id=?',
      [
        question !== undefined ? question.slice(0, 500) : existing.question,
        answer !== undefined ? answer.slice(0, 2000) : existing.answer,
        sort_order !== undefined ? (parseInt(sort_order) || 0) : existing.sort_order,
        active !== undefined ? (active ? 1 : 0) : existing.active,
        id
      ]
    );
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// DELETE /admin/faq/:id — delete FAQ item
router.delete('/admin/faq/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!Number.isInteger(id) || id <= 0) return res.status(400).json({ error: 'Invalid id' });
    await run('DELETE FROM faq WHERE id=?', [id]);
    res.json({ ok: true });
  } catch (e) { next(e); }
});

// ─── AI: FAQ generation (admin) ───────────────────────────────────────────────
// POST /api/admin/faq/generate — generate FAQ items using Claude AI
router.post('/admin/faq/generate', auth, async (req, res) => {
  const { topic } = req.body;
  if (!topic) return res.json({ ok: false, error: 'topic required' });
  if (typeof topic !== 'string' || topic.length > 200) return res.json({ ok: false, error: 'topic must be a string under 200 characters' });

  const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
  if (!ANTHROPIC_API_KEY) return res.json({ ok: false, error: 'ANTHROPIC_API_KEY not set' });

  try {
    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 1024,
        messages: [{
          role: 'user',
          content: `Создай 3 вопроса и ответа для FAQ агентства моделей Nevesty Models на тему: "${topic}"\n\nФормат JSON: [{"question": "...", "answer": "..."}]\nТолько JSON, без других слов. Язык: русский.`
        }]
      })
    });

    const data = await response.json();
    const text = data.content?.[0]?.text || '[]';
    const match = text.match(/\[[\s\S]*\]/);
    const items = match ? JSON.parse(match[0]) : [];

    res.json({ ok: true, items });
  } catch (e) {
    res.json({ ok: false, error: e.message });
  }
});

// ─── AI: model matching by description (client) ───────────────────────────────
// POST /api/client/ai-match — match models to a natural language description
router.post('/client/ai-match', aiMatchLimiter, async (req, res) => {
  const { description } = req.body;
  if (!description || description.length < 10) return res.json({ ok: false, error: 'Describe your request in more detail' });
  if (description.length > 500) return res.json({ ok: false, error: 'Description too long (max 500 characters)' });

  let models = [];
  try {
    models = await query(
      `SELECT id, name, age, height, category, bio, params, city, instagram
       FROM models WHERE available=1 AND COALESCE(archived,0)=0 ORDER BY featured DESC LIMIT 20`
    );

    if (!models.length) return res.json({ ok: true, models: [] });

    const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
    if (!ANTHROPIC_API_KEY) return res.json({ ok: true, models: models.slice(0, 3) });

    const modelList = models.map((m, i) =>
      `${i+1}. ${m.name} (${m.age} лет, рост ${m.height} см, ${m.category}, ${m.city || 'Москва'}): ${m.bio || 'Профессиональная модель'}`
    ).join('\n');

    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 512,
        messages: [{
          role: 'user',
          content: `Клиент ищет модель для мероприятия: "${description}"\n\nДоступные модели:\n${modelList}\n\nВерни JSON: {"top3": [номер1, номер2, номер3], "reason": "краткое объяснение"}\nТолько JSON, без других слов.`
        }]
      })
    });

    const data = await response.json();
    const text = data.content?.[0]?.text || '';
    const match = text.match(/\{[\s\S]*\}/);
    if (!match) return res.json({ ok: true, models: models.slice(0, 3) });

    const parsed = JSON.parse(match[0]);
    const topIndices = (parsed.top3 || [1, 2, 3]).map(n => parseInt(n) - 1).filter(i => i >= 0 && i < models.length);
    const topModels = topIndices.map(i => models[i]).filter(Boolean);

    res.json({ ok: true, models: topModels, reason: parsed.reason || '' });
  } catch (e) {
    console.error('[AI Match] Error:', e.message);
    res.json({ ok: true, models: models.slice(0, 3) }); // fallback
  }
});

// ─── FAQ (public) ─────────────────────────────────────────────────────────────
// GET /api/faq — returns active FAQ items from the faq table (managed via admin CRUD)
// Response shape: [{id, q, a}] — maps question/answer to q/a for faq.html compatibility
router.get('/faq', async (req, res) => {
  try {
    const rows = await query(
      'SELECT id, question AS q, answer AS a FROM faq WHERE active=1 ORDER BY sort_order ASC, id ASC'
    );
    res.json(rows);
  } catch (e) {
    res.json([]);
  }
});

module.exports = router;
module.exports.setBot = setBot;
