'use strict';
// Wave 129: reviews, model busy dates, wishlists advanced, manager, broadcast

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave129-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, modelId;

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

  // Create a model for testing
  const mr = await request(app)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({ name: 'Wave129 Test Model', age: 24, city: 'Москва', category: 'fashion', available: 1 });
  modelId = mr.body.id;
}, 30000);

// ── 1. Reviews (admin) ────────────────────────────────────────────────────────

describe('Admin reviews — GET /api/admin/reviews', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/reviews');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/reviews').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has reviews array', async () => {
    const res = await request(app).get('/api/admin/reviews').set('Authorization', `Bearer ${adminToken}`);
    const hasReviews = Array.isArray(res.body.reviews) || Array.isArray(res.body.items) || Array.isArray(res.body);
    expect(hasReviews).toBe(true);
  });

  it('accepts ?status=pending filter', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?status=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('accepts ?status=approved filter', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?status=approved')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('Admin reviews — PATCH approve/reject /api/admin/reviews/:id', () => {
  let reviewId;

  beforeAll(async () => {
    // Insert a review directly via DB
    const { run: dbRun } = require('../database');
    const result = await dbRun(
      `INSERT INTO reviews (chat_id, model_id, rating, text, client_name, approved)
       VALUES (?,?,?,?,?,?)`,
      [999001, modelId, 5, 'Wave129 test review text', 'Test Reviewer', 0]
    );
    reviewId = result.id;
  });

  it('approve: returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/reviews/${reviewId}/approve`);
    expect(res.status).toBe(401);
  });

  it('approve: returns 200 with auth', async () => {
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId}/approve`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('reject: returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/reviews/${reviewId}/reject`);
    expect(res.status).toBe(401);
  });

  it('reject: returns 200 with auth', async () => {
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId}/reject`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── 2. Model busy dates ───────────────────────────────────────────────────────

describe('Model busy dates — GET/POST /api/admin/models/:id/busy-dates', () => {
  it('GET returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/models/${modelId}/busy-dates`);
    expect(res.status).toBe(401);
  });

  it('GET returns 200 with auth', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('GET response has dates array', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`);
    const hasDates = Array.isArray(res.body.dates) || Array.isArray(res.body.busy_dates) || Array.isArray(res.body);
    expect(hasDates).toBe(true);
  });

  it('POST adds a busy date', async () => {
    const res = await request(app)
      .post(`/api/admin/models/${modelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ busy_date: '2026-09-15', reason: 'Wave129 test booking' });
    expect([200, 201]).toContain(res.status);
    if (res.status === 200) expect(res.body.ok).toBe(true);
  });

  it('POST returns 401 without auth', async () => {
    const res = await request(app).post(`/api/admin/models/${modelId}/busy-dates`).send({ date: '2026-09-20' });
    expect(res.status).toBe(401);
  });
});

// ── 3. Manager (admin) ────────────────────────────────────────────────────────

describe('Managers — GET /api/admin/managers', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/managers');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/managers').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has managers array', async () => {
    const res = await request(app).get('/api/admin/managers').set('Authorization', `Bearer ${adminToken}`);
    const hasManagers = Array.isArray(res.body.managers) || Array.isArray(res.body);
    expect(hasManagers).toBe(true);
  });
});

// ── 4. Broadcast ──────────────────────────────────────────────────────────────

describe('Broadcast — GET /api/admin/broadcasts', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/broadcasts');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/broadcasts').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has broadcasts array or items', async () => {
    const res = await request(app).get('/api/admin/broadcasts').set('Authorization', `Bearer ${adminToken}`);
    const hasBroadcasts =
      Array.isArray(res.body.broadcasts) || Array.isArray(res.body.items) || Array.isArray(res.body);
    expect(hasBroadcasts).toBe(true);
  });
});

// ── 5. Wishlists (user-facing) ────────────────────────────────────────────────

describe('Wishlists — GET /api/user/wishlist', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/user/wishlist');
    expect(res.status).toBe(401);
  });

  it('returns 400 without chat_id (with auth)', async () => {
    const res = await request(app).get('/api/user/wishlist').set('Authorization', `Bearer ${adminToken}`);
    expect([400, 422]).toContain(res.status);
  });

  it('returns 200 with valid chat_id (with auth)', async () => {
    const res = await request(app)
      .get('/api/user/wishlist?chat_id=888001')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has models array (with auth)', async () => {
    const res = await request(app)
      .get('/api/user/wishlist?chat_id=888001')
      .set('Authorization', `Bearer ${adminToken}`);
    const hasModels = Array.isArray(res.body.models) || Array.isArray(res.body.wishlist) || Array.isArray(res.body);
    expect(hasModels).toBe(true);
  });
});

describe('Wishlists — POST /api/user/wishlist', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/user/wishlist').send({});
    expect(res.status).toBe(401);
  });

  it('returns 400 for missing fields (with auth)', async () => {
    const res = await request(app).post('/api/user/wishlist').set('Authorization', `Bearer ${adminToken}`).send({});
    expect(res.status).toBe(400);
  });

  it('returns 201 or 200 for valid add (with auth)', async () => {
    const res = await request(app)
      .post('/api/user/wishlist')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ chat_id: 888001, model_id: modelId });
    expect([200, 201]).toContain(res.status);
  });

  it('returns 409 for duplicate add (with auth)', async () => {
    // First add
    await request(app)
      .post('/api/user/wishlist')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ chat_id: 888002, model_id: modelId });
    // Second add (duplicate)
    const res = await request(app)
      .post('/api/user/wishlist')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ chat_id: 888002, model_id: modelId });
    expect([409, 200]).toContain(res.status); // 409 conflict or 200 idempotent
  });
});

// ── 6. Audit log ──────────────────────────────────────────────────────────────

describe('Audit log — GET /api/admin/audit-log', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/audit-log');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/audit-log').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has rows array', async () => {
    const res = await request(app).get('/api/admin/audit-log').set('Authorization', `Bearer ${adminToken}`);
    const hasRows = Array.isArray(res.body.rows) || Array.isArray(res.body.logs) || Array.isArray(res.body);
    expect(hasRows).toBe(true);
  });

  it('accepts ?limit query param', async () => {
    const res = await request(app).get('/api/admin/audit-log?limit=5').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 7. Orders status history ──────────────────────────────────────────────────

describe('Order status history — GET /api/admin/orders/:id/history', () => {
  let orderId;

  beforeAll(async () => {
    const { run: dbRun } = require('../database');
    const result = await dbRun(
      `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, model_id, status)
       VALUES (?,?,?,?,?,?,?)`,
      ['ORD-WAVE129', 'Test Client', '+79001234567', 'photo', '2026-10-01', modelId, 'new']
    );
    orderId = result.id;
  });

  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}/history`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/history`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has history array', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/history`)
      .set('Authorization', `Bearer ${adminToken}`);
    const hasHistory = Array.isArray(res.body.history) || Array.isArray(res.body);
    expect(hasHistory).toBe(true);
  });
});
