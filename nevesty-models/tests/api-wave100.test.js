'use strict';
/**
 * Wave 21-25 integration tests:
 *  1. OTP login flow (source-code + HTTP)
 *  2. Security — client_chat_id not accepted from form body
 *  3. Broadcast concurrency guard in services/scheduler.js
 *  4. Settings stub features applied in bot.js (БЛОК 1)
 *  5. Catalog URL sync (catalog.html)
 *  6. keyboards/constants.js exports
 */

// ─── Env for in-memory DB test server ────────────────────────────────────────
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

const API_JS = path.join(__dirname, '../routes/api.js');
const BOT_JS = path.join(__dirname, '../bot.js');
const SCHEDULER_JS = path.join(__dirname, '../services/scheduler.js');
const CATALOG_HTML = path.join(__dirname, '../public/catalog.html');

let app, adminToken;

beforeAll(async () => {
  const { initDatabase } = require('../database');
  await initDatabase();
  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');
  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());
  const bot = initBot(a);
  if (bot && apiRouter.setBot) apiRouter.setBot(bot);
  a.use('/api', apiRouter);
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const res = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = res.body?.token || res.body?.accessToken || null;
}, 60000);

afterAll(async () => {
  await new Promise(r => setTimeout(r, 300));
});

// ─── 1. OTP login flow ────────────────────────────────────────────────────────

describe('OTP login flow', () => {
  it('POST /api/client/request-code requires phone', async () => {
    const res = await request(app).post('/api/client/request-code').send({});
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('POST /api/client/request-code returns 404 for unknown phone', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '+70000000000' });
    expect(res.status).toBe(404);
  });

  it('POST /api/client/verify rejects wrong code', async () => {
    // We need a phone that exists in orders; since DB is empty we expect 400/401
    const res = await request(app).post('/api/client/verify').send({ phone: '+70000000000', code: '000000' });
    // Either 400 (bad phone format) or 401 (code not found) — both are non-success
    expect([400, 401]).toContain(res.status);
  });

  it('POST /api/client/verify returns 400 or 429 when phone or code is missing', async () => {
    const res = await request(app).post('/api/client/verify').send({ phone: '+79991234567' });
    // Either 400 (missing code field) or 429 (rate limited from previous test calls)
    expect([400, 429]).toContain(res.status);
    expect(res.body).toHaveProperty('error');
  });

  it('OTP uses crypto.randomInt not Math.random (source check)', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    // The OTP generation uses require('crypto').randomInt inline
    expect(src).toMatch(/require\(['"]crypto['"]\)\.randomInt/);
    // Math.random should not be used for OTP generation (6-digit range 100000–1000000)
    expect(src).not.toMatch(/Math\.random.*1000000|1000000.*Math\.random/);
  });

  it('api.js defines /client/request-code route', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    expect(src).toContain('/client/request-code');
  });

  it('api.js defines /client/verify route', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    expect(src).toContain('/client/verify');
  });

  it('OTP is 6-digit (randomInt range 100000–1000000)', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    expect(src).toMatch(/randomInt\s*\(\s*100000\s*,\s*1000000\s*\)/);
  });

  it('OTP stored in client_otp table with expires_at', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    expect(src).toContain('client_otp');
    expect(src).toContain('expires_at');
  });

  it('OTP verify uses timingSafeEqual (timing-safe comparison)', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    expect(src).toContain('timingSafeEqual');
  });
});

// ─── 2. Security — client_chat_id not accepted from form ─────────────────────

describe('Security — client_chat_id not accepted from form body', () => {
  it('POST /api/orders ignores client_chat_id from request body (hardcoded null)', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    // Must hardcode client_chat_id as null in the orders insert
    expect(src).toContain('client_chat_id: null');
  });

  it('api.js comment documents that client_chat_id is set only by bot', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    // Comment clarifying intent
    expect(src).toMatch(/client_chat_id.*Only set by bot|Only set by bot.*client_chat_id/s);
  });

  it('api.js has comment saying client_chat_id is NOT accepted from booking form', () => {
    const src = fs.readFileSync(API_JS, 'utf8');
    expect(src).toMatch(
      /client_chat_id.*NOT accepted from.*booking form|NOT accepted from.*booking form.*client_chat_id/s
    );
  });

  it('POST /api/orders endpoint exists and returns 4xx without required fields', async () => {
    const res = await request(app).post('/api/orders').send({});
    // Should fail validation (4xx) not crash (5xx)
    expect(res.status).toBeGreaterThanOrEqual(400);
    expect(res.status).toBeLessThan(500);
  });
});

// ─── 3. Broadcast concurrency guard ─────────────────────────────────────────

describe('Broadcast concurrency guard', () => {
  it('services/scheduler.js has _broadcastRunning guard variable', () => {
    const src = fs.readFileSync(SCHEDULER_JS, 'utf8');
    expect(src).toMatch(/_broadcastRunning/);
  });

  it('scheduler.js checks _broadcastRunning before starting broadcast', () => {
    const src = fs.readFileSync(SCHEDULER_JS, 'utf8');
    // Guard should check the flag and return early if already running
    expect(src).toMatch(/if\s*\(_broadcastRunning\)/);
  });

  it('scheduler.js sets _broadcastRunning = true when starting', () => {
    const src = fs.readFileSync(SCHEDULER_JS, 'utf8');
    expect(src).toMatch(/_broadcastRunning\s*=\s*true/);
  });

  it('scheduler.js resets _broadcastRunning = false after completion', () => {
    const src = fs.readFileSync(SCHEDULER_JS, 'utf8');
    expect(src).toMatch(/_broadcastRunning\s*=\s*false/);
  });
});

// ─── 4. Settings stub features applied in bot.js (БЛОК 1) ────────────────────

describe('Bot settings applied in logic', () => {
  let botSrc;
  beforeAll(() => {
    botSrc = fs.readFileSync(BOT_JS, 'utf8');
  });

  it('bot.js checks model_max_photos setting', () => {
    expect(botSrc).toContain("getSetting('model_max_photos')");
  });

  it('bot.js checks booking_require_email setting', () => {
    expect(botSrc).toContain("getSetting('booking_require_email')");
  });

  it('bot.js checks wishlist_enabled setting for client keyboard', () => {
    expect(botSrc).toContain('wishlist_enabled');
  });

  it('bot.js checks loyalty_enabled setting', () => {
    expect(botSrc).toContain('loyalty_enabled');
  });

  it('bot.js reads model_max_photos with getSetting', () => {
    expect(botSrc).toMatch(/getSetting\(['"]model_max_photos['"]\)/);
  });

  it('bot.js reads booking_require_email with getSetting', () => {
    expect(botSrc).toMatch(/getSetting\(['"]booking_require_email['"]\)/);
  });

  it('bot.js reads wishlist_enabled with getSetting', () => {
    expect(botSrc).toMatch(/getSetting\(['"]wishlist_enabled['"]\)/);
  });

  it('bot.js reads loyalty_enabled with getSetting', () => {
    expect(botSrc).toMatch(/getSetting\(['"]loyalty_enabled['"]\)/);
  });
});

// ─── 5. Catalog URL sync ──────────────────────────────────────────────────────

describe('Catalog URL sync', () => {
  it('catalog.html has URL sync logic using history API', () => {
    const src = fs.readFileSync(CATALOG_HTML, 'utf8');
    expect(src).toMatch(/history\.(pushState|replaceState)/);
  });

  it('catalog.html uses URLSearchParams for query string building', () => {
    const src = fs.readFileSync(CATALOG_HTML, 'utf8');
    expect(src).toContain('URLSearchParams');
  });

  it('catalog.html reads URLSearchParams on load (initial state sync)', () => {
    const src = fs.readFileSync(CATALOG_HTML, 'utf8');
    // Should read params from location.search on page load
    expect(src).toMatch(/new URLSearchParams\(location\.search\)/);
  });

  it('catalog.html calls replaceState to update URL without reload', () => {
    const src = fs.readFileSync(CATALOG_HTML, 'utf8');
    expect(src).toContain('replaceState');
  });
});

// ─── 6. keyboards/constants.js exports ───────────────────────────────────────

describe('keyboards/constants.js exports', () => {
  let c;
  beforeAll(() => {
    c = require('../keyboards/constants');
  });

  it('exports MONTHS_RU with 12 month abbreviations', () => {
    expect(c.MONTHS_RU).toBeDefined();
    expect(c.MONTHS_RU).toHaveLength(12);
  });

  it('MONTHS_RU contains Russian month names', () => {
    expect(c.MONTHS_RU[0]).toBe('янв');
    expect(c.MONTHS_RU[11]).toBe('дек');
  });

  it('exports LOYALTY_LEVELS', () => {
    expect(c.LOYALTY_LEVELS).toBeDefined();
    expect(Array.isArray(c.LOYALTY_LEVELS)).toBe(true);
  });

  it('LOYALTY_LEVELS has at least 3 tiers', () => {
    expect(c.LOYALTY_LEVELS.length).toBeGreaterThanOrEqual(3);
  });

  it('each LOYALTY_LEVELS entry has key, label, minEarned, discount', () => {
    for (const lvl of c.LOYALTY_LEVELS) {
      expect(lvl).toHaveProperty('key');
      expect(lvl).toHaveProperty('label');
      expect(lvl).toHaveProperty('minEarned');
      expect(lvl).toHaveProperty('discount');
    }
  });

  it('exports ACHIEVEMENTS_LIST', () => {
    expect(c.ACHIEVEMENTS_LIST).toBeDefined();
    expect(Array.isArray(c.ACHIEVEMENTS_LIST)).toBe(true);
  });

  it('ACHIEVEMENTS_LIST is non-empty', () => {
    expect(c.ACHIEVEMENTS_LIST.length).toBeGreaterThan(0);
  });

  it('exports TEMPLATE_CATEGORIES', () => {
    expect(c.TEMPLATE_CATEGORIES).toBeDefined();
    expect(typeof c.TEMPLATE_CATEGORIES).toBe('object');
  });

  it('exports QUICK_REPLY_TEMPLATES as non-empty array', () => {
    expect(c.QUICK_REPLY_TEMPLATES).toBeDefined();
    expect(Array.isArray(c.QUICK_REPLY_TEMPLATES)).toBe(true);
    expect(c.QUICK_REPLY_TEMPLATES.length).toBeGreaterThan(0);
  });
});
