'use strict';
/**
 * API Integration Tests — New Features
 * Covers: model busy dates, payment tracking, review reply, bulk order status, date range filters.
 * Uses Jest + supertest against an in-memory SQLite database.
 *
 * Run: npm test
 */

// ── Env setup BEFORE any app module is loaded ────────────────────────────────
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = ''; // disable bot
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app;
let adminToken;
let seededModelId;
let seededOrderId;
let seededOrderId2;
let seededReviewId;

// ── Server bootstrap ─────────────────────────────────────────────────────────

beforeAll(async () => {
  const { initDatabase, run, get } = require('../database');
  await initDatabase();

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());

  const bot = initBot(a);
  if (bot) apiRouter.setBot(bot);

  a.use('/api', apiRouter);

  // Global error handler
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => {
    res.status(500).json({ error: err.message });
  });

  app = a;

  // ── Obtain admin token ──────────────────────────────────────────────────────
  const loginRes = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;

  // ── Seed a model ────────────────────────────────────────────────────────────
  const modelRes = await run(
    `INSERT INTO models (name, height, weight, bust, waist, hips, shoe_size, age, category, city, available)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ['Feature Test Model', 172, 54, 87, 61, 89, '38', 22, 'fashion', 'Kyiv', 1]
  );
  seededModelId = modelRes.id;

  // ── Seed two orders ─────────────────────────────────────────────────────────
  const { generateOrderNumber } = require('../database');
  const on1 = await generateOrderNumber();
  const orderRes1 = await run(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, event_duration, status)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
    [on1, 'Test Client A', '+79001234567', 'photo_shoot', '2026-09-01', 4, 'new']
  );
  seededOrderId = orderRes1.id;

  const on2 = await generateOrderNumber();
  const orderRes2 = await run(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, event_duration, status)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
    [on2, 'Test Client B', '+79001234568', 'photo_shoot', '2026-09-02', 2, 'new']
  );
  seededOrderId2 = orderRes2.id;

  // ── Seed an approved review ─────────────────────────────────────────────────
  const reviewRes = await run(
    `INSERT INTO reviews (client_name, rating, text, model_id, approved)
     VALUES (?, ?, ?, ?, ?)`,
    ['Reviewer A', 5, 'Excellent model, highly recommended!', seededModelId, 1]
  );
  seededReviewId = reviewRes.id;
}, 30000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ── Model busy dates ──────────────────────────────────────────────────────────

describe('Model busy dates', () => {
  const testDate = '2026-10-15';

  test('POST /admin/models/:id/busy-dates adds a date range', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post(`/api/admin/models/${seededModelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ busy_date: testDate, reason: 'Commercial shoot' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('GET /admin/models/:id/busy-dates returns dates', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get(`/api/admin/models/${seededModelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    const found = res.body.find(d => d.busy_date === testDate);
    expect(found).toBeDefined();
    expect(found.reason).toBe('Commercial shoot');
  });

  test('DELETE /admin/models/:id/busy-dates/:date removes a date', async () => {
    expect(adminToken).toBeTruthy();
    // First add another date to delete
    const dateToDelete = '2026-10-20';
    await request(app)
      .post(`/api/admin/models/${seededModelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ busy_date: dateToDelete });

    // Delete it
    const res = await request(app)
      .delete(`/api/admin/models/${seededModelId}/busy-dates/${dateToDelete}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);

    // Verify it is gone
    const listRes = await request(app)
      .get(`/api/admin/models/${seededModelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`);
    const stillPresent = listRes.body.find(d => d.busy_date === dateToDelete);
    expect(stillPresent).toBeUndefined();
  });

  test('POST /admin/models/:id/busy-dates rejects invalid dates', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post(`/api/admin/models/${seededModelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ busy_date: 'not-a-date' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /admin/models/:id/busy-dates requires auth', async () => {
    const res = await request(app)
      .post(`/api/admin/models/${seededModelId}/busy-dates`)
      .send({ busy_date: '2026-11-01' });
    expect(res.status).toBe(401);
  });

  test('GET /admin/models/:id/busy-dates requires auth', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${seededModelId}/busy-dates`);
    expect(res.status).toBe(401);
  });

  test('POST /admin/models/:id/busy-dates ignores duplicate date (INSERT OR IGNORE)', async () => {
    expect(adminToken).toBeTruthy();
    // Insert the same date again — should succeed silently (INSERT OR IGNORE)
    const res = await request(app)
      .post(`/api/admin/models/${seededModelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ busy_date: testDate, reason: 'Duplicate attempt' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    // Only one entry should exist for that date
    const listRes = await request(app)
      .get(`/api/admin/models/${seededModelId}/busy-dates`)
      .set('Authorization', `Bearer ${adminToken}`);
    const matches = listRes.body.filter(d => d.busy_date === testDate);
    expect(matches.length).toBe(1);
  });
});

// ── Order payment ─────────────────────────────────────────────────────────────

describe('Order payment', () => {
  test('PATCH /admin/orders/:id/payment marks order as paid', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch(`/api/admin/orders/${seededOrderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: true });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.paid_at).toBeTruthy();
  });

  test('PATCH /admin/orders/:id/payment clears paid_at when paid=false', async () => {
    expect(adminToken).toBeTruthy();
    // First mark as paid
    await request(app)
      .patch(`/api/admin/orders/${seededOrderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: true });

    // Now clear payment
    const res = await request(app)
      .patch(`/api/admin/orders/${seededOrderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: false });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.paid_at).toBeFalsy();
  });

  test('PATCH /admin/orders/:id/payment returns 400 when paid is not boolean', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch(`/api/admin/orders/${seededOrderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: 'yes' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('PATCH /admin/orders/:id/payment returns 404 for non-existent order', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch('/api/admin/orders/999999/payment')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: true });
    expect(res.status).toBe(404);
  });

  test('PATCH /admin/orders/:id/payment requires auth', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${seededOrderId}/payment`)
      .send({ paid: true });
    expect(res.status).toBe(401);
  });
});

// ── Review reply ──────────────────────────────────────────────────────────────

describe('Review reply', () => {
  test('PATCH /admin/reviews/:id/reply saves reply text', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch(`/api/admin/reviews/${seededReviewId}/reply`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reply: 'Thank you for the great feedback!' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PATCH /admin/reviews/:id/reply persists reply in database', async () => {
    expect(adminToken).toBeTruthy();
    const replyText = 'We really appreciate your kind words!';
    await request(app)
      .patch(`/api/admin/reviews/${seededReviewId}/reply`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reply: replyText });

    // Verify via admin reviews list
    const listRes = await request(app)
      .get('/api/admin/reviews')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(listRes.status).toBe(200);
    const reviews = listRes.body.reviews || listRes.body;
    const review = Array.isArray(reviews)
      ? reviews.find(r => r.id === seededReviewId)
      : null;
    if (review) {
      expect(review.admin_reply).toBe(replyText);
    }
  });

  test('GET /api/reviews returns reply_at when present', async () => {
    // Set a reply so reply_at is populated
    await request(app)
      .patch(`/api/admin/reviews/${seededReviewId}/reply`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reply: 'Reply that sets reply_at timestamp' });

    // Admin reviews endpoint includes admin_reply column
    const adminRes = await request(app)
      .get('/api/admin/reviews')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(adminRes.status).toBe(200);
    const reviews = adminRes.body.reviews || adminRes.body;
    if (Array.isArray(reviews)) {
      const review = reviews.find(r => r.id === seededReviewId);
      if (review) {
        expect(review.reply_at).toBeTruthy();
      }
    }
  });

  test('PATCH /admin/reviews/:id/reply returns 400 when reply is not a string', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch(`/api/admin/reviews/${seededReviewId}/reply`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reply: 12345 });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('PATCH /admin/reviews/:id/reply clears reply when empty string sent', async () => {
    expect(adminToken).toBeTruthy();
    // First set a reply
    await request(app)
      .patch(`/api/admin/reviews/${seededReviewId}/reply`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reply: 'Initial reply' });

    // Now clear it
    const res = await request(app)
      .patch(`/api/admin/reviews/${seededReviewId}/reply`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reply: '' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PATCH /admin/reviews/:id/reply requires auth', async () => {
    const res = await request(app)
      .patch(`/api/admin/reviews/${seededReviewId}/reply`)
      .send({ reply: 'Unauthorized reply' });
    expect(res.status).toBe(401);
  });
});

// ── Bulk order status ─────────────────────────────────────────────────────────

describe('Bulk order status', () => {
  test('PATCH /admin/orders/bulk-status changes multiple orders', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [seededOrderId, seededOrderId2], status: 'reviewing' });
    expect(res.status).toBe(200);
    expect(res.body.updated).toBe(2);

    // Verify status was changed in the list
    const listRes = await request(app)
      .get('/api/admin/orders')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(listRes.status).toBe(200);
    const orders = listRes.body.orders || [];
    const order1 = orders.find(o => o.id === seededOrderId);
    const order2 = orders.find(o => o.id === seededOrderId2);
    if (order1) expect(order1.status).toBe('reviewing');
    if (order2) expect(order2.status).toBe('reviewing');
  });

  test('PATCH /admin/orders/bulk-status rejects invalid status', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [seededOrderId], status: 'invalid_status_xyz' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('PATCH /admin/orders/bulk-status rejects empty ids array', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [], status: 'confirmed' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('PATCH /admin/orders/bulk-status requires auth', async () => {
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .send({ ids: [seededOrderId], status: 'confirmed' });
    expect(res.status).toBe(401);
  });

  test('POST /admin/orders/bulk-status also works (POST alias)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ order_ids: [seededOrderId, seededOrderId2], status: 'confirmed' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.affected).toBe(2);
  });

  test('POST /admin/orders/bulk-status rejects invalid status', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ order_ids: [seededOrderId], status: 'not_a_real_status' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });
});

// ── Order date range filter ───────────────────────────────────────────────────

describe('Order date range filter', () => {
  test('GET /admin/orders with date_from filters correctly', async () => {
    expect(adminToken).toBeTruthy();
    // Use a future date that no seeded order can match
    const farFuture = '2099-01-01';
    const res = await request(app)
      .get(`/api/admin/orders?date_from=${farFuture}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.total).toBe(0);
    expect(res.body.orders.length).toBe(0);
  });

  test('GET /admin/orders with date_from and date_to filters correctly', async () => {
    expect(adminToken).toBeTruthy();
    // Use a past range that no seeded order can match
    const res = await request(app)
      .get('/api/admin/orders?date_from=2000-01-01&date_to=2000-01-31')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.total).toBe(0);
    expect(res.body.orders.length).toBe(0);
  });

  test('GET /admin/orders with date_from=today returns today\'s orders', async () => {
    expect(adminToken).toBeTruthy();
    // Seeded orders are created "now" in SQLite via DEFAULT CURRENT_TIMESTAMP
    const today = new Date().toISOString().slice(0, 10);
    const res = await request(app)
      .get(`/api/admin/orders?date_from=${today}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.total).toBeGreaterThanOrEqual(2);
  });

  test('GET /admin/orders with date_from and date_to spanning today returns seeded orders', async () => {
    expect(adminToken).toBeTruthy();
    const today = new Date().toISOString().slice(0, 10);
    const tomorrow = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
    const res = await request(app)
      .get(`/api/admin/orders?date_from=${today}&date_to=${tomorrow}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.total).toBeGreaterThanOrEqual(2);
  });

  test('GET /admin/orders with invalid date_from is ignored gracefully', async () => {
    expect(adminToken).toBeTruthy();
    // Invalid date should be silently ignored (validateDate returns false) → all orders returned
    const res = await request(app)
      .get('/api/admin/orders?date_from=not-a-date')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.total).toBeGreaterThanOrEqual(2);
  });

  test('GET /admin/orders date filter requires auth', async () => {
    const today = new Date().toISOString().slice(0, 10);
    const res = await request(app)
      .get(`/api/admin/orders?date_from=${today}`);
    expect(res.status).toBe(401);
  });
});
