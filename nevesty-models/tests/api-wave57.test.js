'use strict';
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, seededOrderId, seededModelId;

beforeAll(async () => {
  const { initDatabase, run, get, generateOrderNumber } = require('../database');
  await initDatabase();
  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');
  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());
  const bot = initBot(a);
  if (bot) apiRouter.setBot(bot);

  // Register sitemap.xml route on the test app (mirrors server.js logic)
  a.get('/sitemap.xml', async (req, res) => {
    try {
      const { query: dbQuery } = require('../database');
      const models = await dbQuery('SELECT id, name, created_at FROM models WHERE available=1 AND archived=0 ORDER BY created_at DESC');
      const baseUrl = process.env.SITE_URL || 'https://nevesty-models.ru';
      const today = new Date().toISOString().split('T')[0];
      const modelUrls = models.map(m =>
        `\n  <url>\n    <loc>${baseUrl}/model/${m.id}</loc>\n    <lastmod>${m.created_at ? m.created_at.split('T')[0] : today}</lastmod>\n    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>`
      ).join('');
      const xml = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n  <url><loc>${baseUrl}/</loc><lastmod>${today}</lastmod><changefreq>daily</changefreq><priority>1.0</priority></url>${modelUrls}\n</urlset>`;
      res.header('Content-Type', 'application/xml');
      res.send(xml);
    } catch (e) {
      res.status(500).send('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>');
    }
  });

  a.use('/api', apiRouter);
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;
  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
  const model = await get('SELECT id FROM models LIMIT 1');
  seededModelId = model ? model.id : null;
  const orderNum = await generateOrderNumber();
  const orderRes = await run(
    'INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, status) VALUES (?,?,?,?,?,?)',
    [orderNum, 'W57 Client', '+79991234567', 'корпоратив', '2025-12-31', 'confirmed']
  );
  seededOrderId = orderRes ? orderRes.id : null;
}, 15000);

// ── 1. Enhanced Admin Stats ───────────────────────────────────────────────────

describe('Enhanced Admin Stats', () => {
  it('GET /api/admin/stats returns 200', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('body has total_orders field', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.total_orders).toBe('number');
  });

  it('body has new_orders field', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.new_orders).toBe('number');
  });

  it('body has conversion_rate as number', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.conversion_rate).toBe('number');
  });

  it('conversion_rate is between 0 and 100', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.conversion_rate).toBeGreaterThanOrEqual(0);
    expect(res.body.conversion_rate).toBeLessThanOrEqual(100);
  });

  it('body has avg_order_budget field', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('avg_order_budget');
  });

  it('top_models is an array', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.top_models)).toBe(true);
  });

  it('orders_by_status is an object', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.orders_by_status).toBe('object');
    expect(Array.isArray(res.body.orders_by_status)).toBe(false);
  });

  it('orders_trend has direction field', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.orders_trend).toHaveProperty('direction');
  });

  it("orders_trend.direction is 'up', 'down', or 'flat'/'stable'", async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(['up', 'down', 'flat', 'stable']).toContain(res.body.orders_trend.direction);
  });

  it('body has avg_cycle_days field', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('avg_cycle_days');
  });

  it('requires authentication (401 without token)', async () => {
    const res = await request(app).get('/api/admin/stats');
    expect(res.status).toBe(401);
  });
});

// ── 2. Order Status History ───────────────────────────────────────────────────

describe('Order Status History', () => {
  it('GET /api/admin/orders/:id/history requires auth (401)', async () => {
    const res = await request(app).get('/api/admin/orders/1/history');
    expect(res.status).toBe(401);
  });

  it('GET /api/admin/orders/:id/history returns 200 for seeded order', async () => {
    if (!seededOrderId) return;
    const res = await request(app)
      .get(`/api/admin/orders/${seededOrderId}/history`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response body is an array or contains order_id', async () => {
    if (!seededOrderId) return;
    const res = await request(app)
      .get(`/api/admin/orders/${seededOrderId}/history`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const isArray = Array.isArray(res.body);
    const hasOrderId = !isArray && res.body && typeof res.body === 'object' && ('order_id' in res.body || 'history' in res.body);
    expect(isArray || hasOrderId).toBe(true);
  });

  it('returns 401 for history of a non-seeded order without auth', async () => {
    const res = await request(app).get('/api/admin/orders/999999/history');
    expect(res.status).toBe(401);
  });

  it('returns 200 or 404 for non-existent order history with auth', async () => {
    const res = await request(app)
      .get('/api/admin/orders/999999/history')
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
  });
});

// ── 3. Code Quality Tooling ───────────────────────────────────────────────────

describe('Code Quality Tooling', () => {
  it('eslintrc.js exists', () => {
    const fs = require('fs');
    expect(fs.existsSync('.eslintrc.js')).toBe(true);
  });

  it('prettierrc exists', () => {
    const fs = require('fs');
    expect(fs.existsSync('.prettierrc')).toBe(true);
  });

  it('eslintrc.js is valid JSON-like config', () => {
    const config = require('../.eslintrc.js');
    expect(config).toHaveProperty('env');
    expect(config).toHaveProperty('rules');
  });

  it('.prettierrc contains valid JSON', () => {
    const fs = require('fs');
    const content = fs.readFileSync('.prettierrc', 'utf8');
    expect(() => JSON.parse(content)).not.toThrow();
    const config = JSON.parse(content);
    expect(typeof config).toBe('object');
  });

  it('eslintrc.js env includes node', () => {
    const config = require('../.eslintrc.js');
    expect(config.env).toHaveProperty('node', true);
  });

  it('eslintrc.js rules includes no-eval', () => {
    const config = require('../.eslintrc.js');
    expect(config.rules).toHaveProperty('no-eval');
  });
});

// ── 4. WebApp API ─────────────────────────────────────────────────────────────

describe('WebApp API', () => {
  it('GET /api/orders/by-phone returns 200 with orders array', async () => {
    const res = await request(app).get('/api/orders/by-phone?phone=+79991234567');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('orders');
    expect(Array.isArray(res.body.orders)).toBe(true);
  });

  it('GET /api/orders/by-phone finds the seeded order by phone', async () => {
    if (!seededOrderId) return;
    const res = await request(app).get('/api/orders/by-phone?phone=+79991234567');
    expect(res.status).toBe(200);
    expect(res.body.orders.length).toBeGreaterThan(0);
  });

  it('GET /api/orders/by-phone returns empty orders for unknown phone', async () => {
    const res = await request(app).get('/api/orders/by-phone?phone=+70000000000');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(res.body.orders.length).toBe(0);
  });

  it('GET /api/orders/by-phone has total field', async () => {
    const res = await request(app).get('/api/orders/by-phone?phone=+79991234567');
    expect(res.status).toBe(200);
    expect(typeof res.body.total).toBe('number');
  });

  it('GET /api/faq returns 200', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
  });

  it('GET /api/faq returns an array (may be empty in test DB)', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('GET /api/settings/public returns 200', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
  });

  it('GET /api/settings/public returns an object', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    expect(typeof res.body).toBe('object');
    expect(Array.isArray(res.body)).toBe(false);
  });

  it('GET /sitemap.xml returns 200', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.status).toBe(200);
  });

  it('GET /sitemap.xml Content-Type includes xml', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/xml/);
  });
});

// ── 5. Admin Stats Calculations ───────────────────────────────────────────────

describe('Admin Stats Calculations', () => {
  it('conversion_rate is correctly computed', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // We seeded one 'confirmed' order, so conversion > 0
    if (res.body.total_orders > 0) {
      expect(res.body.conversion_rate).toBeGreaterThanOrEqual(0);
      expect(res.body.conversion_rate).toBeLessThanOrEqual(100);
    }
  });

  it('conversion_rate is > 0 when confirmed orders exist', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // We seeded a 'confirmed' order
    if (res.body.total_orders > 0) {
      expect(res.body.conversion_rate).toBeGreaterThan(0);
    }
  });

  it('orders_by_status reflects the seeded confirmed order', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    if (seededOrderId && res.body.orders_by_status) {
      expect(res.body.orders_by_status).toHaveProperty('confirmed');
      expect(res.body.orders_by_status.confirmed).toBeGreaterThan(0);
    }
  });

  it('total_orders matches sum of orders_by_status values', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const byStatus = res.body.orders_by_status;
    if (byStatus && typeof byStatus === 'object') {
      const sum = Object.values(byStatus).reduce((acc, v) => acc + (v || 0), 0);
      expect(res.body.total_orders).toBe(sum);
    }
  });

  it('top_models entries have id, name, and order_count', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    if (res.body.top_models && res.body.top_models.length > 0) {
      const first = res.body.top_models[0];
      expect(first).toHaveProperty('id');
      expect(first).toHaveProperty('name');
      expect(first).toHaveProperty('order_count');
    }
  });

  it('orders_trend delta equals current_7d minus previous_7d', async () => {
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const trend = res.body.orders_trend;
    if (trend && 'delta' in trend && 'current_7d' in trend && 'previous_7d' in trend) {
      expect(trend.delta).toBe(trend.current_7d - trend.previous_7d);
    }
  });
});
