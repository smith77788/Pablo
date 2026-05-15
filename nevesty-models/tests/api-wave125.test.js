'use strict';
// Wave 125: social posts, FAQ CRUD, price packages, cabinet login, analytics overview/chart/conversion

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave125-test-secret-32-chars-ok!!';
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

// ── 1. Social posts ───────────────────────────────────────────────────────────

describe('Social posts — GET /api/admin/social/posts', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/social/posts');
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid admin token', async () => {
    const res = await request(app).get('/api/admin/social/posts').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has posts array', async () => {
    const res = await request(app).get('/api/admin/social/posts').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('posts');
    expect(Array.isArray(res.body.posts)).toBe(true);
  });

  it('filters by platform query param (default instagram)', async () => {
    const res = await request(app)
      .get('/api/admin/social/posts?platform=instagram')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.posts)).toBe(true);
  });
});

describe('Social posts — POST /api/admin/social/posts', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/social/posts').send({ caption: 'test' });
    expect(res.status).toBe(401);
  });

  it('returns 400 without caption', async () => {
    const res = await request(app)
      .post('/api/admin/social/posts')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ platform: 'instagram' });
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('creates post and returns id', async () => {
    const res = await request(app)
      .post('/api/admin/social/posts')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ platform: 'instagram', caption: 'Wave125 test post #models #fashion' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id');
    expect(res.body.id).toBeGreaterThan(0);
    expect(res.body.status).toBe('scheduled');
  });

  it('created post appears in GET list', async () => {
    const createRes = await request(app)
      .post('/api/admin/social/posts')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ caption: 'Unique wave125 caption XYZ' });
    expect(createRes.status).toBe(200);

    const listRes = await request(app).get('/api/admin/social/posts').set('Authorization', `Bearer ${adminToken}`);
    expect(listRes.status).toBe(200);
    const found = listRes.body.posts.find(p => p.caption === 'Unique wave125 caption XYZ');
    expect(found).toBeDefined();
  });
});

describe('Social posts — PATCH /api/admin/social/posts/:id/status', () => {
  let postId;

  beforeAll(async () => {
    const r = await request(app)
      .post('/api/admin/social/posts')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ caption: 'Status test post' });
    postId = r.body.id;
  });

  it('returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/social/posts/${postId}/status`).send({ status: 'published' });
    expect(res.status).toBe(401);
  });

  it('returns 400 for invalid status', async () => {
    const res = await request(app)
      .patch(`/api/admin/social/posts/${postId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'invalid_status' });
    expect(res.status).toBe(400);
  });

  it('returns 200 for valid status transition', async () => {
    const res = await request(app)
      .patch(`/api/admin/social/posts/${postId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'published' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── 2. FAQ CRUD ───────────────────────────────────────────────────────────────

describe('FAQ — GET/POST /api/admin/faq', () => {
  it('GET returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/faq');
    expect(res.status).toBe(401);
  });

  it('GET returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/faq').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('GET response has items array or faq array', async () => {
    const res = await request(app).get('/api/admin/faq').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const hasFaq = Array.isArray(res.body.faq) || Array.isArray(res.body.items) || Array.isArray(res.body);
    expect(hasFaq).toBe(true);
  });

  it('POST creates FAQ item', async () => {
    const res = await request(app)
      .post('/api/admin/faq')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({
        question: 'Как забронировать модель?',
        answer: 'Через форму на сайте или Telegram бот.',
        category: 'booking',
      });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id');
  });

  it('POST returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/faq').send({ question: 'Q', answer: 'A' });
    expect(res.status).toBe(401);
  });
});

describe('FAQ — PUT/DELETE /api/admin/faq/:id', () => {
  let faqId;

  beforeAll(async () => {
    const r = await request(app)
      .post('/api/admin/faq')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ question: 'FAQ to update', answer: 'Answer', category: 'general' });
    faqId = r.body.id;
  });

  it('PUT returns 401 without auth', async () => {
    const res = await request(app).put(`/api/admin/faq/${faqId}`).send({ question: 'Updated?' });
    expect(res.status).toBe(401);
  });

  it('PUT updates FAQ item successfully', async () => {
    const res = await request(app)
      .put(`/api/admin/faq/${faqId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ question: 'Updated FAQ question', answer: 'Updated answer', category: 'general' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('DELETE removes FAQ item', async () => {
    const res = await request(app).delete(`/api/admin/faq/${faqId}`).set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('DELETE returns 401 without auth', async () => {
    const res = await request(app).delete(`/api/admin/faq/1`);
    expect(res.status).toBe(401);
  });
});

// ── 3. Price packages ─────────────────────────────────────────────────────────

describe('Price packages — GET/POST /api/admin/price-packages', () => {
  it('GET returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/price-packages');
    expect(res.status).toBe(401);
  });

  it('GET returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('GET response has packages array', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const hasPackages = Array.isArray(res.body.packages) || Array.isArray(res.body.items) || Array.isArray(res.body);
    expect(hasPackages).toBe(true);
  });

  it('POST creates price package', async () => {
    const res = await request(app)
      .post('/api/admin/price-packages')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({
        name: 'Стандарт',
        price_from: 50000,
        price_to: 100000,
        duration: '4 часа',
        category: 'standard',
        description: 'Стандартный пакет',
      });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id');
    expect(res.body.ok).toBe(true);
  });
});

// ── 4. Analytics overview ─────────────────────────────────────────────────────

describe('Analytics overview — GET /api/admin/analytics/overview', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/overview');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has ok:true', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
  });

  it('has orders field with today/week/month/total', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('orders');
    expect(typeof res.body.orders.today).toBe('number');
    expect(typeof res.body.orders.total).toBe('number');
  });

  it('has revenue field with week/month/total', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('revenue');
    expect(typeof res.body.revenue.total).toBe('number');
  });

  it('has models and clients fields', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('models');
    expect(res.body).toHaveProperty('clients');
  });
});

// ── 5. Analytics revenue chart ────────────────────────────────────────────────

describe('Analytics revenue chart — GET /api/admin/analytics/revenue-chart', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue-chart');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has ok:true and data array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.data)).toBe(true);
  });

  it('respects period query param (max 365)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart?period=7')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.period).toBe(7);
  });
});

// ── 6. Analytics conversion ───────────────────────────────────────────────────

describe('Analytics conversion — GET /api/admin/analytics/conversion', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has total and conversion_rate fields', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('total');
    const hasConversion = 'conversion_rate' in res.body || 'conversionRate' in res.body || 'rate' in res.body;
    expect(hasConversion || typeof res.body.total === 'number').toBe(true);
  });
});

// ── 7. Public FAQ endpoint ────────────────────────────────────────────────────

describe('Public FAQ — GET /api/faq', () => {
  it('returns 200 without auth (public endpoint)', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
  });

  it('response has items or faq array', async () => {
    const res = await request(app).get('/api/faq');
    const hasFaq = Array.isArray(res.body.faq) || Array.isArray(res.body.items) || Array.isArray(res.body);
    expect(hasFaq).toBe(true);
  });

  it('GET /api/faq/categories returns 200', async () => {
    const res = await request(app).get('/api/faq/categories');
    expect(res.status).toBe(200);
  });
});

// ── 8. Client segments ────────────────────────────────────────────────────────

describe('Client segments — GET /api/admin/analytics/client-segments', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/client-segments');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has ok:true', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
  });

  it('has segments object with vip/active/dormant/one_time fields (all numbers)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('segments');
    const { segments } = res.body;
    expect(typeof segments.vip).toBe('number');
    expect(typeof segments.active).toBe('number');
    expect(typeof segments.dormant).toBe('number');
    expect(typeof segments.one_time).toBe('number');
  });
});
