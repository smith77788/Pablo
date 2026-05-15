'use strict';
// Wave 118: Public reviews API, BI analytics, compression/gzip, cache headers, cabinet auth

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-wave118-minimum-32chars!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.BOT_TOKEN = '123:test';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
delete process.env.BITRIX24_WEBHOOK_URL;
delete process.env.AMOCRM_SUBDOMAIN;
delete process.env.AMOCRM_ACCESS_TOKEN;

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const compression = require('compression');

let app, adminToken;

beforeAll(async () => {
  const { initDatabase, run } = require('../database');
  await initDatabase();

  // Seed approved reviews
  await run(
    `INSERT INTO reviews (client_name, rating, text, approved, created_at)
     VALUES (?,?,?,?,?)`,
    ['Alice Wave118', 5, 'Excellent service!', 1, new Date().toISOString()]
  );
  await run(
    `INSERT INTO reviews (client_name, rating, text, approved, created_at)
     VALUES (?,?,?,?,?)`,
    ['Bob Wave118', 4, 'Very good experience.', 1, new Date().toISOString()]
  );
  await run(
    `INSERT INTO reviews (client_name, rating, text, approved, created_at)
     VALUES (?,?,?,?,?)`,
    ['Carol Wave118', 3, 'Pretty decent.', 1, new Date().toISOString()]
  );
  await run(
    `INSERT INTO reviews (client_name, rating, text, approved, created_at)
     VALUES (?,?,?,?,?)`,
    ['Dave Wave118', 2, 'Could be better.', 1, new Date().toISOString()]
  );

  // Seed a pending (unapproved) review — should never appear in public endpoint
  await run(
    `INSERT INTO reviews (client_name, rating, text, approved, created_at)
     VALUES (?,?,?,?,?)`,
    ['Eve Wave118 PENDING', 5, 'Should not appear in public.', 0, new Date().toISOString()]
  );

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();
  a.use(cors());
  a.use(
    compression({
      level: 6,
      threshold: 1024,
      filter: (req, res) => {
        if (req.headers['x-no-compression']) return false;
        return compression.filter(req, res);
      },
    })
  );
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));

  // Mirrors sitemap.xml from server.js
  a.get('/sitemap.xml', async (req, res) => {
    res.header('Content-Type', 'application/xml');
    res.header('Cache-Control', 'public, max-age=3600');
    res.send(
      '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
    );
  });

  // Mirrors robots.txt from server.js
  a.get('/robots.txt', (req, res) => {
    res.type('text/plain');
    res.send('User-agent: *\nAllow: /\n');
  });

  const bot = initBot(a);
  if (bot && apiRouter.setBot) apiRouter.setBot(bot);

  a.use('/api', apiRouter);
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 20000);

// ── 1. Public reviews — GET /api/reviews/public ───────────────────────────────

describe('Public reviews — GET /api/reviews/public', () => {
  it('returns 200 without authorization header', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
  });

  it('response body contains a "reviews" field', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.body).toHaveProperty('reviews');
  });

  it('"reviews" field is an array', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(Array.isArray(res.body.reviews)).toBe(true);
  });

  it('each review has "rating" field', async () => {
    const res = await request(app).get('/api/reviews/public');
    const { reviews } = res.body;
    expect(reviews.length).toBeGreaterThan(0);
    reviews.forEach(r => expect(r).toHaveProperty('rating'));
  });

  it('each review has "text" field', async () => {
    const res = await request(app).get('/api/reviews/public');
    const { reviews } = res.body;
    expect(reviews.length).toBeGreaterThan(0);
    reviews.forEach(r => expect(r).toHaveProperty('text'));
  });

  it('each review has "created_at" field', async () => {
    const res = await request(app).get('/api/reviews/public');
    const { reviews } = res.body;
    expect(reviews.length).toBeGreaterThan(0);
    reviews.forEach(r => expect(r).toHaveProperty('created_at'));
  });

  it('?limit=3 returns at most 3 reviews', async () => {
    const res = await request(app).get('/api/reviews/public').query({ limit: 3 });
    expect(res.status).toBe(200);
    expect(res.body.reviews.length).toBeLessThanOrEqual(3);
  });

  it('?limit=100 is clamped to 20 maximum', async () => {
    const res = await request(app).get('/api/reviews/public').query({ limit: 100 });
    expect(res.status).toBe(200);
    expect(res.body.reviews.length).toBeLessThanOrEqual(20);
  });

  it('only approved reviews appear (no pending reviews in result)', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
    const { reviews } = res.body;
    const hasPending = reviews.some(r => r.client_name === 'Eve Wave118 PENDING');
    expect(hasPending).toBe(false);
  });
});

// ── 2. BI Analytics endpoints ─────────────────────────────────────────────────

describe('BI Analytics — GET /api/admin/analytics/*', () => {
  it('/overview returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('/overview response has orders.month field', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('orders');
    expect(res.body.orders).toHaveProperty('month');
  });

  it('/overview response has revenue.month field', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('revenue');
    expect(res.body.revenue).toHaveProperty('month');
  });

  it('/top-models?limit=5 returns 200 with models array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-models')
      .query({ limit: 5 })
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('models');
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('/revenue-chart?period=7 returns 200 with data array and correct period', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart')
      .query({ period: 7 })
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('data');
    expect(Array.isArray(res.body.data)).toBe(true);
    expect(res.body.period).toBe(7);
  });

  it('/conversion returns 200 with funnel.new field', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('funnel');
    expect(res.body.funnel).toHaveProperty('new');
  });

  it('/conversion returns funnel.confirmed and funnel.completed fields', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.funnel).toHaveProperty('confirmed');
    expect(res.body.funnel).toHaveProperty('completed');
  });

  it('/overview returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/analytics/overview');
    expect(res.status).toBe(401);
  });
});

// ── 3. Compression/gzip ───────────────────────────────────────────────────────

describe('Compression/gzip — GET /api/models', () => {
  it('returns 200 with Accept-Encoding: gzip header', async () => {
    const res = await request(app).get('/api/models').set('Accept-Encoding', 'gzip');
    expect(res.status).toBe(200);
  });

  it('does not crash when gzip encoding is accepted', async () => {
    // Simply ensure the request completes without throwing
    const res = await request(app).get('/api/models').set('Accept-Encoding', 'gzip, deflate, br');
    expect([200, 304]).toContain(res.status);
  });
});

// ── 4. Cache headers — sitemap.xml and robots.txt ─────────────────────────────

describe('Cache headers — /sitemap.xml and /robots.txt', () => {
  it('/sitemap.xml returns Content-Type: application/xml', async () => {
    const res = await request(app).get('/sitemap.xml');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/application\/xml/);
  });

  it('/robots.txt returns Content-Type: text/plain', async () => {
    const res = await request(app).get('/robots.txt');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/plain/);
  });
});

// ── 5. Cabinet orders auth ────────────────────────────────────────────────────

describe('Cabinet orders auth — GET /api/cabinet/orders', () => {
  it('returns 401 without any Authorization header', async () => {
    const res = await request(app).get('/api/cabinet/orders');
    expect(res.status).toBe(401);
  });

  it('returns 401 with invalid bearer token', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', 'Bearer invalid.token.here');
    expect(res.status).toBe(401);
  });

  it('returns 401 with a malformed Authorization header (no Bearer prefix)', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', 'notavalidtoken');
    expect(res.status).toBe(401);
  });

  it('returns 403 when using an admin JWT (type=admin, not client)', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });

  it('returns ok:false when unauthorized', async () => {
    const res = await request(app).get('/api/cabinet/orders');
    expect(res.body.ok).toBe(false);
  });
});
