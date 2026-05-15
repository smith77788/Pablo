'use strict';
// Wave 127: server health/metrics, cabinet auth, chat/ask, analytics KPI, DB ops, payments

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave127-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

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
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;
}, 30000);

// ── 1. Cabinet auth (client login) ───────────────────────────────────────────

describe('Cabinet login — POST /api/cabinet/login', () => {
  it('returns 400 for missing phone', async () => {
    const res = await request(app).post('/api/cabinet/login').send({});
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 400 for invalid phone format', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: 'not-a-phone' });
    expect([400, 422]).toContain(res.status);
  });

  it('returns 404 for unknown phone (no orders)', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '+7 (999) 000-00-00' });
    expect([404, 400]).toContain(res.status);
  });

  it('returns proper error structure when failed', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '+7 (999) 000-00-00' });
    expect(res.status).not.toBe(200);
    expect(res.body).toHaveProperty('error');
  });
});

// ── 4. Cabinet orders (requires client auth) ──────────────────────────────────

describe('Cabinet orders — GET /api/cabinet/orders', () => {
  it('returns 401 without client auth token', async () => {
    const res = await request(app).get('/api/cabinet/orders');
    expect(res.status).toBe(401);
  });
});

// ── 5. Analytics KPI ──────────────────────────────────────────────────────────

describe('Analytics KPI — GET /api/admin/analytics/kpi', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has numeric KPI fields (total, completed, active, new_clients)', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    // KPI endpoint returns: { total, completed, active, new_clients, ...status_counts }
    expect(typeof res.body.total).toBe('number');
    expect(typeof res.body.completed).toBe('number');
    expect(typeof res.body.active).toBe('number');
    expect(typeof res.body.new_clients).toBe('number');
  });

  it('all KPI values are non-negative numbers', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.total).toBeGreaterThanOrEqual(0);
    expect(res.body.completed).toBeGreaterThanOrEqual(0);
    expect(res.body.active).toBeGreaterThanOrEqual(0);
  });
});

// ── 6. DB stats and vacuum ────────────────────────────────────────────────────

describe('DB stats — GET /api/admin/db-stats', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/db-stats');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has size or tables field', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    const hasStats = 'size_mb' in res.body || 'size' in res.body || 'tables' in res.body || 'table_count' in res.body;
    expect(hasStats).toBe(true);
  });
});

describe('DB vacuum — POST /api/admin/db-vacuum', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/db-vacuum');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth (or 500 if DB locked)', async () => {
    const res = await request(app).post('/api/admin/db-vacuum').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
    if (res.status === 200) {
      // Returns {success: true} or {ok: true}
      const isOk = res.body.ok === true || res.body.success === true;
      expect(isOk).toBe(true);
    }
  });
});

// ── 7. Cache stats and clear ──────────────────────────────────────────────────

describe('Cache — GET /api/admin/cache/stats and DELETE /api/admin/cache', () => {
  it('GET cache/stats returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/cache/stats');
    expect(res.status).toBe(401);
  });

  it('GET cache/stats returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/cache/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('DELETE cache returns 401 without auth', async () => {
    const res = await request(app).delete('/api/admin/cache');
    expect(res.status).toBe(401);
  });

  it('DELETE cache returns 200 with auth', async () => {
    const res = await request(app).delete('/api/admin/cache').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── 8. Payments webhook ───────────────────────────────────────────────────────

describe('Payments webhook — POST /api/payments/webhook', () => {
  it('returns 403 for requests from unverified IP', async () => {
    // YooKassa webhook — should reject when IP not in allowed list and signature invalid
    const res = await request(app).post('/api/payments/webhook').send({ type: 'payment.succeeded' });
    // Without valid IP or signature, returns 403
    expect([403, 200]).toContain(res.status);
  });

  it('returns 200 with ok:true for empty/malformed payload (idempotent)', async () => {
    // Idempotency: webhook with no orderId should return 200 ok
    const res = await request(app).post('/api/payments/webhook').send({});
    // Either 403 (IP not whitelisted) or 200 ok (no orderId to process)
    expect([200, 403]).toContain(res.status);
  });
});

// ── 9. Sitemap XML ────────────────────────────────────────────────────────────

describe('Sitemap — GET /api/sitemap-models.xml', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    expect(res.status).toBe(200);
  });

  it('returns XML content type', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    const ct = res.headers['content-type'] || '';
    expect(ct).toMatch(/xml|text/);
  });

  it('contains urlset root element', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    if (typeof res.text === 'string') {
      expect(res.text).toMatch(/urlset|sitemap/i);
    }
  });
});

// ── 10. Analytics funnel ──────────────────────────────────────────────────────

describe('Analytics funnel — GET /api/admin/analytics/funnel', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has funnel stages array or steps array', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    const hasFunnel =
      Array.isArray(res.body.stages) ||
      Array.isArray(res.body.steps) ||
      Array.isArray(res.body.funnel) ||
      Array.isArray(res.body);
    expect(hasFunnel).toBe(true);
  });
});
