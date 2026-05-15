'use strict';
// Wave 126: price-packages PUT/DELETE, db-stats, db-vacuum, cache stats/clear,
//           analytics KPI, payments webhook, analytics revenue, sitemap-models.xml

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave126-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, createdPackageId;

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
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;

  // Pre-create a price package for PUT/DELETE tests
  const cr = await request(app)
    .post('/api/admin/price-packages')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({ name: 'Wave126 Package', price_from: 5000, category: 'standard' });
  createdPackageId = cr.body.id;
}, 30000);

// ── 1. Price packages — GET ───────────────────────────────────────────────────

describe('Price packages — GET /api/admin/price-packages', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/price-packages');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has ok:true and packages array', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.packages)).toBe(true);
  });
});

// ── 2. Price packages — PUT ───────────────────────────────────────────────────

describe('Price packages — PUT /api/admin/price-packages/:id', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app)
      .put(`/api/admin/price-packages/${createdPackageId}`)
      .send({ name: 'Updated Package' });
    expect(res.status).toBe(401);
  });

  it('returns 400 when name is missing', async () => {
    const res = await request(app)
      .put(`/api/admin/price-packages/${createdPackageId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ price_from: 6000 });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/name/i);
  });

  it('returns 400 for invalid (non-integer) id', async () => {
    const res = await request(app)
      .put('/api/admin/price-packages/abc')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ name: 'Updated Package' });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/invalid id/i);
  });

  it('successfully updates an existing package', async () => {
    const res = await request(app)
      .put(`/api/admin/price-packages/${createdPackageId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({
        name: 'Updated Wave126 Package',
        description: 'Updated desc',
        price_from: 7000,
        price_to: 15000,
        duration: '4h',
        category: 'premium',
        sort_order: 1,
        active: true,
      });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('updated name is reflected in GET list', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    const pkg = res.body.packages.find(p => p.id === createdPackageId);
    expect(pkg).toBeDefined();
    expect(pkg.name).toBe('Updated Wave126 Package');
  });
});

// ── 3. Price packages — DELETE ────────────────────────────────────────────────

describe('Price packages — DELETE /api/admin/price-packages/:id', () => {
  let pkgToDeleteId;

  beforeAll(async () => {
    const cr = await request(app)
      .post('/api/admin/price-packages')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ name: 'To Delete', price_from: 1000, category: 'standard' });
    pkgToDeleteId = cr.body.id;
  });

  it('returns 401 without auth', async () => {
    const res = await request(app).delete(`/api/admin/price-packages/${pkgToDeleteId}`);
    expect(res.status).toBe(401);
  });

  it('returns 400 for invalid id', async () => {
    const res = await request(app).delete('/api/admin/price-packages/xyz').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/invalid id/i);
  });

  it('successfully deletes a package', async () => {
    const res = await request(app)
      .delete(`/api/admin/price-packages/${pkgToDeleteId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('deleted package no longer appears in GET list', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    const pkg = res.body.packages.find(p => p.id === pkgToDeleteId);
    expect(pkg).toBeUndefined();
  });
});

// ── 4. DB Stats ───────────────────────────────────────────────────────────────

describe('DB stats — GET /api/admin/db-stats', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/db-stats');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has tables array', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.tables)).toBe(true);
  });

  it('response has size_bytes and size_mb fields', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('size_bytes');
    expect(res.body).toHaveProperty('size_mb');
  });

  it('response has schema_versions array', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.schema_versions)).toBe(true);
  });
});

// ── 5. DB Vacuum (both routes) ────────────────────────────────────────────────

describe('DB vacuum — POST /api/admin/db-vacuum', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/db-vacuum');
    expect(res.status).toBe(401);
  });

  it('returns 200 with success:true', async () => {
    const res = await request(app).post('/api/admin/db-vacuum').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
    expect(res.body.message).toMatch(/vacuum/i);
  });
});

describe('DB vacuum — POST /api/admin/db/vacuum', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/db/vacuum');
    expect(res.status).toBe(401);
  });

  it('returns 200 with ok:true', async () => {
    const res = await request(app).post('/api/admin/db/vacuum').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.message).toMatch(/vacuum/i);
  });
});

// ── 6. Cache stats & clear ────────────────────────────────────────────────────

describe('Cache stats — GET /api/admin/cache/stats', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/cache/stats');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    const res = await request(app).get('/api/admin/cache/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has keys, hits, misses, hit_rate, backend fields', async () => {
    const res = await request(app).get('/api/admin/cache/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('keys');
    expect(res.body).toHaveProperty('hits');
    expect(res.body).toHaveProperty('misses');
    expect(res.body).toHaveProperty('hit_rate');
    expect(res.body).toHaveProperty('backend');
  });

  it('backend is "memory" in test env (no Redis)', async () => {
    const res = await request(app).get('/api/admin/cache/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.backend).toBe('memory');
  });
});

describe('Cache clear — DELETE /api/admin/cache', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).delete('/api/admin/cache');
    expect(res.status).toBe(401);
  });

  it('returns 200 with ok:true and message', async () => {
    const res = await request(app).delete('/api/admin/cache').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.message).toMatch(/cache cleared/i);
  });

  it('cache keys drops to 0 after clear', async () => {
    const res = await request(app).get('/api/admin/cache/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.keys).toBe(0);
  });
});

// ── 7. Analytics KPI ─────────────────────────────────────────────────────────

describe('Analytics KPI — GET /api/admin/analytics/kpi', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has total, completed, active, new_clients fields', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('total');
    expect(res.body).toHaveProperty('completed');
    expect(res.body).toHaveProperty('active');
    expect(res.body).toHaveProperty('new_clients');
  });

  it('all numeric counts are >= 0', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.total).toBeGreaterThanOrEqual(0);
    expect(res.body.completed).toBeGreaterThanOrEqual(0);
    expect(res.body.active).toBeGreaterThanOrEqual(0);
    expect(res.body.new_clients).toBeGreaterThanOrEqual(0);
  });

  it('accepts ?days= query param', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi?days=7').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('total');
  });
});

// ── 8. Payments webhook ───────────────────────────────────────────────────────
// In DEV_MODE (no SHOP_ID/SECRET_KEY env vars), verifyWebhook() returns true,
// so the webhook always passes IP/signature checks and returns 200 ok:true.
// In production with valid credentials, an invalid IP would get 403.

describe('Payments webhook — POST /api/payments/webhook', () => {
  it('does not require auth header (public webhook endpoint)', async () => {
    // Webhook is public — no Bearer token should be required; must not return 401
    const res = await request(app)
      .post('/api/payments/webhook')
      .set('Content-Type', 'application/json')
      .send(JSON.stringify({}));
    expect(res.status).not.toBe(401);
  });

  it('returns 200 in DEV_MODE with empty body (no orderId)', async () => {
    const res = await request(app)
      .post('/api/payments/webhook')
      .set('Content-Type', 'application/json')
      .send(JSON.stringify({}));
    // DEV_MODE: verifyWebhook passes, no orderId → ok:true
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok');
  });

  it('returns 200 in DEV_MODE with payment.succeeded event (unknown order)', async () => {
    const res = await request(app)
      .post('/api/payments/webhook')
      .set('Content-Type', 'application/json')
      .send(
        JSON.stringify({
          event: 'payment.succeeded',
          object: { id: 'fake-payment-id', metadata: { orderId: '99999' } },
        })
      );
    // DEV_MODE: signature passes, order not found → ok:true (graceful)
    expect(res.status).toBe(200);
  });
});

// ── 9. Analytics revenue ──────────────────────────────────────────────────────

describe('Analytics revenue — GET /api/admin/analytics/revenue', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid token', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has ok:true, months array, and data array', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.months)).toBe(true);
    expect(Array.isArray(res.body.data)).toBe(true);
  });

  it('accepts ?months= query param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue?months=3')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('months array entries have month, revenue, order_count fields if non-empty', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue').set('Authorization', `Bearer ${adminToken}`);
    // Array may be empty in test DB, but if not entries must have proper shape
    if (res.body.months.length > 0) {
      expect(res.body.months[0]).toHaveProperty('month');
      expect(res.body.months[0]).toHaveProperty('revenue');
      expect(res.body.months[0]).toHaveProperty('order_count');
    }
  });
});

// ── 10. Sitemap — public XML ──────────────────────────────────────────────────

describe('Sitemap — GET /api/sitemap-models.xml', () => {
  it('returns 200 without auth (public endpoint)', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    expect(res.status).toBe(200);
  });

  it('Content-Type is application/xml', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    expect(res.headers['content-type']).toMatch(/application\/xml/);
  });

  it('body starts with XML declaration', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    expect(res.text).toMatch(/^<\?xml/);
  });

  it('body contains urlset element', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    expect(res.text).toContain('<urlset');
    expect(res.text).toContain('</urlset>');
  });

  it('sets Cache-Control header', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    expect(res.headers['cache-control']).toMatch(/max-age/i);
  });
});
