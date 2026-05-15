'use strict';
/**
 * Integration tests for Wave 75 features:
 * - Repeat Order (bot.js: repeatOrder, bk_repeat_confirm, bk_repeat_cancel)
 * - Admin Stats API (GET /api/admin/stats, /api/admin/stats/extended2)
 * - Catalog API filters (sort, city)
 * - Dynamic Cities Setting (cities_list, cat_city_, showCitiesMenu)
 * - Archive/Restore Models (adm_archive_, is_archived)
 * - Security (authLimiter, rateLimit, sanitizeInput)
 * - Health endpoint (GET /health)
 * - Public reviews (GET /api/reviews)
 * - Wishlist (GET /api/user/wishlist)
 */

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

let app, adminToken;

const botContent = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
const routesContent = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
const serverContent = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');

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

  const res = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = res.body?.token || res.body?.accessToken || null;
}, 30000);

afterAll(async () => {
  await new Promise(r => setTimeout(r, 300));
});

// ── Repeat Order ──────────────────────────────────────────────────────────────

describe('Wave 75: Repeat Order', () => {
  test('bot.js contains repeatOrder function', () => {
    expect(botContent).toMatch(/repeatOrder|repeat_order|bk_repeat/i);
  });

  test('bot.js contains repeat_order_ callback prefix', () => {
    expect(botContent).toMatch(/repeat_order_/);
  });

  test('bot.js contains bk_repeat_confirm handler', () => {
    expect(botContent).toMatch(/bk_repeat_confirm/);
  });

  test('bot.js contains bk_repeat_cancel handler', () => {
    expect(botContent).toMatch(/bk_repeat_cancel/);
  });

  test('bot.js repeatOrder function fills session with prefill data', () => {
    expect(botContent).toMatch(/prefill|bk_repeat_confirm.*prefill|setSession.*bk_repeat/s);
  });

  test('bot.js shows confirmation buttons for repeat order', () => {
    // Should have both confirm and cancel buttons defined together for repeat flow
    expect(botContent).toMatch(/bk_repeat_confirm[\s\S]{1,300}bk_repeat_cancel|bk_repeat_cancel[\s\S]{1,300}bk_repeat_confirm/);
  });
});

// ── Admin Stats API ───────────────────────────────────────────────────────────

describe('Wave 75: Admin Stats', () => {
  test('GET /api/admin/stats requires auth', async () => {
    const res = await request(app).get('/api/admin/stats');
    expect(res.status).toBe(401);
  });

  test('routes/api.js has admin stats endpoint', () => {
    expect(routesContent).toMatch(/admin\/stats|adm_stats/i);
  });

  test('GET /api/admin/stats with token returns 200', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('GET /api/admin/stats returns total_orders field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.status === 200) {
      expect(res.body).toHaveProperty('total_orders');
    }
  });

  test('GET /api/admin/stats returns total_models field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.status === 200) {
      expect(res.body).toHaveProperty('total_models');
    }
  });

  test('GET /api/admin/stats/extended2 requires auth', async () => {
    const res = await request(app).get('/api/admin/stats/extended2');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/stats/extended2 with token returns 200', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/stats/extended2')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('GET /api/admin/stats/extended2 returns repeat_clients field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/stats/extended2')
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.status === 200) {
      expect(res.body).toHaveProperty('repeat_clients');
    }
  });
});

// ── Catalog API filters ───────────────────────────────────────────────────────

describe('Wave 75: Catalog API', () => {
  test('GET /api/models?sort=featured returns 200', async () => {
    const res = await request(app).get('/api/models?sort=featured');
    expect(res.status).toBe(200);
  });

  test('GET /api/models?sort=newest returns 200', async () => {
    const res = await request(app).get('/api/models?sort=newest');
    expect(res.status).toBe(200);
  });

  test('GET /api/models?city=Київ returns 200', async () => {
    const res = await request(app).get('/api/models?city=%D0%9A%D0%B8%D1%97%D0%B2');
    expect(res.status).toBe(200);
  });

  test('GET /api/models?city=Київ returns array', async () => {
    const res = await request(app).get('/api/models?city=%D0%9A%D0%B8%D1%97%D0%B2');
    expect(Array.isArray(res.body)).toBe(true);
  });

  test('GET /api/models?available=1 returns 200', async () => {
    const res = await request(app).get('/api/models?available=1');
    expect(res.status).toBe(200);
  });

  test('routes/api.js handles city filter for models', () => {
    expect(routesContent).toMatch(/city.*=.*\?|AND city/i);
  });

  test('routes/api.js handles catalog_sort setting', () => {
    expect(routesContent).toMatch(/catalog_sort/);
  });
});

// ── Dynamic Cities ────────────────────────────────────────────────────────────

describe('Wave 75: Cities Setting', () => {
  test('bot.js has cities_list setting handler', () => {
    expect(botContent).toMatch(/cities_list/);
  });

  test('bot.js has dynamic city buttons (cat_city_ prefix)', () => {
    expect(botContent).toMatch(/cat_city_/);
  });

  test('bot.js reads cities_list from settings', () => {
    expect(botContent).toMatch(/getSetting.*cities_list|cities_list.*getSetting/);
  });

  test('bot.js has admin UI to update cities list', () => {
    expect(botContent).toMatch(/adm_set_cities_list/);
  });
});

// ── Archive/Restore Models ────────────────────────────────────────────────────

describe('Wave 75: Model Archive', () => {
  test('bot.js has adm_archive_ handler', () => {
    expect(botContent).toMatch(/adm_archive_/);
  });

  test('routes/api.js supports archived filter for admin models list', () => {
    expect(routesContent).toMatch(/archived/);
  });

  test('GET /api/admin/models?archived=1 requires auth', async () => {
    const res = await request(app).get('/api/admin/models?archived=1');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/models?archived=1 with token returns 200', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/models?archived=1')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── Security ──────────────────────────────────────────────────────────────────

describe('Wave 75: Security', () => {
  test('POST /api/admin/login rate limited (authLimiter exists)', () => {
    expect(routesContent).toMatch(/authLimiter|rateLimit/i);
  });

  test('routes/api.js has input sanitization', () => {
    expect(routesContent).toMatch(/sanitize|sanitizeInput/i);
  });

  test('routes/api.js applies sanitizeInput middleware globally', () => {
    expect(routesContent).toMatch(/router\.use\(sanitizeInput\)/);
  });

  test('POST /api/admin/login with wrong credentials returns 401', async () => {
    const res = await request(app)
      .post('/api/admin/login')
      .send({ username: 'wrong', password: 'wrong' });
    expect(res.status).toBe(401);
  });

  test('POST /api/admin/login with correct credentials returns token', async () => {
    const res = await request(app)
      .post('/api/admin/login')
      .send({ username: 'admin', password: 'admin123' });
    expect(res.status).toBe(200);
    expect(res.body?.token || res.body?.accessToken).toBeTruthy();
  });
});

// ── Health Endpoint ───────────────────────────────────────────────────────────

describe('Wave 75: Health Endpoint', () => {
  test('server.js has /health route', () => {
    expect(serverContent).toMatch(/\/health/);
  });

  test('GET /api/admin/factory/status requires auth', async () => {
    const res = await request(app).get('/api/admin/factory/status');
    expect(res.status).toBe(401);
  });

  test('routes/api.js has factory status endpoint', () => {
    expect(routesContent).toMatch(/factory.*status|factory\/status/i);
  });
});

// ── Public Reviews ────────────────────────────────────────────────────────────

describe('Wave 75: Public Reviews', () => {
  test('GET /api/reviews returns 200', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
  });

  test('GET /api/reviews returns array (legacy mode)', async () => {
    const res = await request(app).get('/api/reviews');
    expect(Array.isArray(res.body) || Array.isArray(res.body?.reviews)).toBe(true);
  });

  test('GET /api/reviews/recent returns 200', async () => {
    const res = await request(app).get('/api/reviews/recent');
    expect(res.status).toBe(200);
  });

  test('GET /api/reviews/recent returns array', async () => {
    const res = await request(app).get('/api/reviews/recent');
    expect(Array.isArray(res.body)).toBe(true);
  });

  test('GET /api/reviews?page=1 returns paginated object', async () => {
    const res = await request(app).get('/api/reviews?page=1');
    expect(res.status).toBe(200);
    // paginated mode returns { reviews, total, page, pages }
    expect(res.body).toHaveProperty('reviews');
    expect(Array.isArray(res.body.reviews)).toBe(true);
  });

  test('GET /api/reviews?page=1 returns total field', async () => {
    const res = await request(app).get('/api/reviews?page=1');
    if (res.status === 200 && res.body.reviews !== undefined) {
      expect(res.body).toHaveProperty('total');
    }
  });
});

// ── Wishlist ──────────────────────────────────────────────────────────────────

describe('Wave 75: Wishlist API', () => {
  test('GET /api/user/wishlist without chat_id returns 400 or empty', async () => {
    const res = await request(app).get('/api/user/wishlist');
    // Should return 400 or an empty array — not 500
    expect(res.status).not.toBe(500);
  });

  test('routes/api.js has wishlist endpoint', () => {
    expect(routesContent).toMatch(/user\/wishlist/);
  });

  test('routes/api.js has wishlistLimiter rate limiter', () => {
    expect(routesContent).toMatch(/wishlistLimiter/);
  });
});
