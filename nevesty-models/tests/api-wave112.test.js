'use strict';
// Wave 112: AI Budget API, BI Dashboard Analytics, Pricing Packages
// Tests live HTTP endpoints via supertest against an in-memory SQLite DB.

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-wave112-minimum-32chars!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.BOT_TOKEN = '123:test';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
// No ANTHROPIC_API_KEY → DEV / fallback mode for AI Budget endpoint

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken;

beforeAll(async () => {
  const { initDatabase, run, generateOrderNumber } = require('../database');
  await initDatabase();

  // Seed a completed order so revenue/conversion queries return meaningful data
  const orderNum = await generateOrderNumber();
  await run(
    'INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, status, budget) VALUES (?,?,?,?,?,?,?)',
    [orderNum, 'Wave112 Client', '+79991112233', 'корпоратив', '2025-12-31', 'completed', '25000']
  );

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

  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 20000);

// ── 1. AI Budget API ──────────────────────────────────────────────────────────

describe('AI Budget API — POST /api/client/ai-budget', () => {
  // Validation tests: these all get rejected before the rate limiter applies
  // (description is missing/too short → 200 with ok:false immediately)
  it('returns 200 with ok:false when description is missing', async () => {
    const res = await request(app).post('/api/client/ai-budget').send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
    expect(res.body.error).toBeTruthy();
  });

  it('returns ok:false when description is empty string', async () => {
    const res = await request(app).post('/api/client/ai-budget').send({ description: '' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
  });

  it('returns ok:false when description is shorter than 10 chars', async () => {
    const res = await request(app).post('/api/client/ai-budget').send({ description: 'short' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
    expect(res.body.error).toMatch(/минимум 10/i);
  });

  it('returns ok:false when description is exactly 9 chars', async () => {
    const res = await request(app).post('/api/client/ai-budget').send({ description: '123456789' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
  });

  // For valid descriptions we make ONE request and run all assertions on the same response
  // to avoid triggering the rate limiter multiple times.
  let fallbackRes;
  beforeAll(async () => {
    fallbackRes = await request(app)
      .post('/api/client/ai-budget')
      .send({ description: 'Корпоратив на 50 человек, нужны 3 модели для подиума, 4 часа' });
  });

  it('returns ok:true with fallback estimate when ANTHROPIC_API_KEY is absent (DEV mode)', () => {
    expect(fallbackRes.status).toBe(200);
    expect(fallbackRes.body.ok).toBe(true);
    expect(fallbackRes.body.ai).toBe(false);
  });

  it('fallback response has estimate.min as a number', () => {
    expect(fallbackRes.status).toBe(200);
    expect(typeof fallbackRes.body.estimate.min).toBe('number');
  });

  it('fallback response has estimate.max as a number', () => {
    expect(fallbackRes.status).toBe(200);
    expect(typeof fallbackRes.body.estimate.max).toBe('number');
  });

  it('fallback estimate.min is less than or equal to estimate.max', () => {
    expect(fallbackRes.status).toBe(200);
    expect(fallbackRes.body.estimate.min).toBeLessThanOrEqual(fallbackRes.body.estimate.max);
  });

  it('fallback response includes notes field', () => {
    expect(fallbackRes.status).toBe(200);
    expect(fallbackRes.body.notes).toBeTruthy();
  });
});

// ── 2. BI Dashboard Analytics ─────────────────────────────────────────────────

describe('BI Dashboard — GET /api/admin/analytics/overview', () => {
  it('returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/analytics/overview');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response body has orders object', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('orders');
    expect(typeof res.body.orders).toBe('object');
  });

  it('response body has revenue object', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('revenue');
    expect(typeof res.body.revenue).toBe('object');
  });

  it('response body has models and clients fields', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('models');
    expect(res.body).toHaveProperty('clients');
  });
});

describe('BI Dashboard — GET /api/admin/analytics/top-models', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models');
    expect(res.status).toBe(401);
  });

  it('returns 200 and models array with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });
});

describe('BI Dashboard — GET /api/admin/analytics/revenue-chart', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue-chart?period=30');
    expect(res.status).toBe(401);
  });

  it('returns 200 and data array with auth', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart?period=30')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.data)).toBe(true);
  });

  it('response includes period field', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart?period=30')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.period).toBe(30);
  });
});

describe('BI Dashboard — GET /api/admin/analytics/conversion', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has conversion_rate as number', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.conversion_rate).toBe('number');
  });

  it('response has total field', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.total).toBe('number');
  });

  it('response has funnel object with new/confirmed/completed/cancelled', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('funnel');
    expect(res.body.funnel).toHaveProperty('new');
    expect(res.body.funnel).toHaveProperty('confirmed');
    expect(res.body.funnel).toHaveProperty('completed');
    expect(res.body.funnel).toHaveProperty('cancelled');
  });

  it('conversion_rate is between 0 and 100', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.conversion_rate).toBeGreaterThanOrEqual(0);
    expect(res.body.conversion_rate).toBeLessThanOrEqual(100);
  });
});

// ── 3. Pricing Packages ───────────────────────────────────────────────────────

describe('Pricing — GET /api/pricing (public)', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/pricing');
    expect(res.status).toBe(200);
  });

  it('response is an array', async () => {
    const res = await request(app).get('/api/pricing');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('array is non-empty (at least 1 package)', async () => {
    const res = await request(app).get('/api/pricing');
    expect(res.status).toBe(200);
    expect(res.body.length).toBeGreaterThan(0);
  });

  it('each package has a name field', async () => {
    const res = await request(app).get('/api/pricing');
    expect(res.status).toBe(200);
    res.body.forEach(pkg => expect(pkg).toHaveProperty('name'));
  });

  it('each package has a description field', async () => {
    const res = await request(app).get('/api/pricing');
    expect(res.status).toBe(200);
    res.body.forEach(pkg => expect(pkg).toHaveProperty('description'));
  });

  it('each package has a price_from field', async () => {
    const res = await request(app).get('/api/pricing');
    expect(res.status).toBe(200);
    res.body.forEach(pkg => expect(pkg).toHaveProperty('price_from'));
  });

  it('each package has a price_to field (may be null)', async () => {
    const res = await request(app).get('/api/pricing');
    expect(res.status).toBe(200);
    res.body.forEach(pkg => expect(Object.prototype.hasOwnProperty.call(pkg, 'price_to')).toBe(true));
  });
});

describe('Pricing — GET /api/admin/price-packages (admin)', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/price-packages');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response includes packages array', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // packages may be returned directly as array or nested under .packages
    const packages = Array.isArray(res.body) ? res.body : res.body.packages;
    expect(Array.isArray(packages)).toBe(true);
  });
});
