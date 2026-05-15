'use strict';
/**
 * API Integration Tests — Wave 47-48 Features
 * Covers: settings export/import/reset, public reviews API,
 *         model stats, view count tracking, wishlist DB helpers, admin messages.
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
let seededOrderNumber;

// ── Server bootstrap ─────────────────────────────────────────────────────────

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
    ['Wave48 Test Model', 175, 55, 88, 62, 90, '39', 24, 'fashion', 'Kyiv', 1]
  );
  seededModelId = modelRes.id;

  // ── Seed a completed order (for review submission) ──────────────────────────
  seededOrderNumber = await generateOrderNumber();
  const orderRes = await run(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, status, model_id)
     VALUES (?, ?, ?, ?, ?, ?)`,
    [seededOrderNumber, 'Wave48 Client', '+79001112233', 'photo_shoot', 'completed', seededModelId]
  );
  seededOrderId = orderRes.id;

  // ── Seed an approved review ─────────────────────────────────────────────────
  await run(
    `INSERT INTO reviews (client_name, rating, text, model_id, approved)
     VALUES (?, ?, ?, ?, ?)`,
    ['Approved Reviewer', 5, 'Great model, highly recommend!', seededModelId, 1]
  );

  // ── Seed a client message (for admin/messages/recent) ──────────────────────
  await run(
    `INSERT INTO messages (order_id, sender_type, sender_name, content)
     VALUES (?, ?, ?, ?)`,
    [seededOrderId, 'client', 'Wave48 Client', 'Hello, I have a question about my order.']
  );
}, 30000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ── Settings Export / Import / Reset ─────────────────────────────────────────

describe('Settings Export/Import/Reset', () => {
  test('GET /api/admin/settings/export requires auth → 401 without token', async () => {
    const res = await request(app).get('/api/admin/settings/export');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/settings/export returns JSON with Content-Disposition header', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/settings/export')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.headers['content-disposition']).toMatch(/attachment/);
    expect(res.headers['content-disposition']).toMatch(/settings\.json/);
    expect(typeof res.body).toBe('object');
    expect(Array.isArray(res.body)).toBe(false);
  });

  test('POST /api/admin/settings/import with valid JSON saves settings', async () => {
    expect(adminToken).toBeTruthy();
    const payload = { catalog_per_page: '12', catalog_sort: 'newest' };
    const res = await request(app)
      .post('/api/admin/settings/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send(payload);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.imported).toBe(2);
  });

  test('POST /api/admin/settings/import skips sensitive keys (admin_password, jwt_secret)', async () => {
    expect(adminToken).toBeTruthy();
    const payload = {
      admin_password: 'hacked',
      jwt_secret: 'stolen',
      catalog_per_page: '8',
    };
    const res = await request(app)
      .post('/api/admin/settings/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send(payload);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    // Only catalog_per_page should be imported (2 sensitive keys skipped)
    expect(res.body.imported).toBe(1);
  });

  test('POST /api/admin/settings/reset with valid key returns default value', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ key: 'greeting' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.value).toBe('string');
    expect(res.body.value.length).toBeGreaterThan(0);
  });

  test('POST /api/admin/settings/reset with unknown key returns 400', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ key: 'nonexistent_key_xyz' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });
});

// ── Public Reviews API ────────────────────────────────────────────────────────

describe('Public Reviews API', () => {
  test('GET /api/reviews returns array of approved reviews', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // All returned reviews must be approved (approved=1 is server-enforced, not in response)
    expect(res.body.length).toBeGreaterThan(0);
    // Verify expected fields
    const review = res.body[0];
    expect(review).toHaveProperty('id');
    expect(review).toHaveProperty('rating');
    expect(review).toHaveProperty('text');
  });

  test('GET /api/reviews?model_id=X filters by model', async () => {
    const res = await request(app).get(`/api/reviews?model_id=${seededModelId}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBeGreaterThan(0);
    // All returned reviews must belong to the filtered model
    for (const review of res.body) {
      expect(review.model_id).toBe(seededModelId);
    }
  });

  test('GET /api/reviews/recent returns at most limit items', async () => {
    const res = await request(app).get('/api/reviews/recent?limit=1');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBeLessThanOrEqual(1);
  });

  test('GET /api/reviews/recent without query returns array with model_name field', async () => {
    const res = await request(app).get('/api/reviews/recent');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('rating');
      expect(res.body[0]).toHaveProperty('text');
      // model_name column is joined from models table (may be null if unlinked)
      expect(Object.prototype.hasOwnProperty.call(res.body[0], 'model_name')).toBe(true);
    }
  });

  test('POST /api/client/review with valid order_number saves pending review', async () => {
    // The endpoint checks order ownership via phone, and requires status=completed
    const res = await request(app)
      .post('/api/client/review')
      .send({
        order_id: seededOrderId,
        phone: '+79001112233',
        rating: 4,
        text: 'Very professional, would book again for sure!',
      });
    // Should succeed (201 or 200) — review saved with approved=0
    expect([200, 201]).toContain(res.status);
    expect(res.body.ok).toBe(true);
    expect(res.body.id).toBeGreaterThan(0);
  });
});

// ── Model Stats ───────────────────────────────────────────────────────────────

describe('Model Stats', () => {
  test('GET /api/admin/models/:id/stats requires auth → 401 without token', async () => {
    const res = await request(app).get(`/api/admin/models/${seededModelId}/stats`);
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/models/:id/stats for valid model returns stats object with expected fields', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get(`/api/admin/models/${seededModelId}/stats`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('total_orders');
    expect(res.body).toHaveProperty('completed_orders');
    expect(res.body).toHaveProperty('active_orders');
    expect(res.body).toHaveProperty('review_count');
    expect(res.body).toHaveProperty('view_count');
    expect(res.body).toHaveProperty('revenue_total');
    // Seeded order is completed, so completed_orders >= 1
    expect(res.body.total_orders).toBeGreaterThanOrEqual(1);
    expect(res.body.completed_orders).toBeGreaterThanOrEqual(1);
  });

  test('GET /api/admin/models/:id/stats for unknown ID returns 404', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/models/999999/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });

  test('POST /api/models/:id/view increments view count (public endpoint)', async () => {
    // First call should succeed (rate limit is per IP:model, fresh in-memory)
    const res = await request(app).post(`/api/models/${seededModelId}/view`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── Wishlist DB-layer helpers ─────────────────────────────────────────────────

describe('Wishlist DB helpers', () => {
  let db;

  beforeAll(() => {
    db = require('../database');
  });

  test('can insert a wishlist entry for a chat_id', async () => {
    const r = await db.run(
      `INSERT INTO wishlists (chat_id, model_id) VALUES (?, ?)`,
      ['test_chat_1001', seededModelId]
    );
    expect(r.id).toBeGreaterThan(0);
  });

  test('can query wishlist entries by chat_id', async () => {
    const rows = await db.query(
      `SELECT w.chat_id, w.model_id, m.name
       FROM wishlists w JOIN models m ON m.id = w.model_id
       WHERE w.chat_id = ?`,
      ['test_chat_1001']
    );
    expect(Array.isArray(rows)).toBe(true);
    expect(rows.length).toBeGreaterThan(0);
    expect(rows[0].chat_id).toBe('test_chat_1001');
    expect(rows[0].model_id).toBe(seededModelId);
  });

  test('UNIQUE constraint prevents duplicate wishlist entries', async () => {
    await expect(
      db.run(
        `INSERT INTO wishlists (chat_id, model_id) VALUES (?, ?)`,
        ['test_chat_1001', seededModelId]
      )
    ).rejects.toThrow();
  });

  test('can remove a wishlist entry', async () => {
    await db.run(
      `DELETE FROM wishlists WHERE chat_id = ? AND model_id = ?`,
      ['test_chat_1001', seededModelId]
    );
    const rows = await db.query(
      `SELECT id FROM wishlists WHERE chat_id = ? AND model_id = ?`,
      ['test_chat_1001', seededModelId]
    );
    expect(rows.length).toBe(0);
  });

  test('wishlist is empty for unknown chat_id', async () => {
    const rows = await db.query(
      `SELECT id FROM wishlists WHERE chat_id = ?`,
      ['totally_unknown_chat_999']
    );
    expect(Array.isArray(rows)).toBe(true);
    expect(rows.length).toBe(0);
  });

  test('GET /api/favorites returns empty array when no IDs provided', async () => {
    const res = await request(app).get('/api/favorites');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBe(0);
  });
});

// ── Admin Messages ────────────────────────────────────────────────────────────

describe('Admin Messages', () => {
  test('GET /api/admin/messages/recent requires auth → 401 without token', async () => {
    const res = await request(app).get('/api/admin/messages/recent');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/messages/recent returns object with messages array', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/messages/recent')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('messages');
    expect(Array.isArray(res.body.messages)).toBe(true);
  });

  test('GET /api/admin/messages/recent?limit=5 returns at most 5 messages', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/messages/recent?limit=5')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.messages.length).toBeLessThanOrEqual(5);
  });

  test('GET /api/admin/messages/recent returns messages with order_number field', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/messages/recent')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.messages.length).toBeGreaterThan(0);
    const msg = res.body.messages[0];
    expect(msg).toHaveProperty('order_number');
    expect(msg).toHaveProperty('content');
    expect(msg).toHaveProperty('sender_type');
    expect(msg.sender_type).toBe('client');
  });
});
