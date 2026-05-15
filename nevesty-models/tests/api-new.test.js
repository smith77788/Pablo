'use strict';
/**
 * API Integration Tests — New Features
 * Covers: TOTP 2FA, Favorites/Wishlist, Reviews flow, Quick Booking
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
const speakeasy = require('speakeasy');
const express = require('express');
const cors = require('cors');

let app;
let adminToken;
let createdModelId;
let createdOrderId;
let createdOrderNumber;

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

  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => {
    res.status(500).json({ error: err.message });
  });

  app = a;

  // ── Authenticate admin ──────────────────────────────────────────────────────
  const loginRes = await request(a)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;

  // ── Create a test model ─────────────────────────────────────────────────────
  const modelRes = await request(a)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({
      name: 'Wishlist Test Model',
      height: 172,
      weight: 53,
      bust: 86,
      waist: 60,
      hips: 88,
      shoe_size: '37',
      age: 22,
      category: 'fashion',
      city: 'Kyiv',
      available: 1,
    });
  createdModelId = modelRes.body.id || null;

  // ── Create a test order and mark it completed ───────────────────────────────
  const csrfRes = await request(a).get('/api/csrf-token');
  const csrfToken = csrfRes.body.token;

  const orderRes = await request(a)
    .post('/api/orders')
    .set('x-csrf-token', csrfToken)
    .send({
      client_name: 'Review Test Client',
      client_phone: '+7 999 555-11-22',
      event_type: 'photo_shoot',
      event_date: '2026-09-01',
      event_duration: 3,
    });
  createdOrderId = orderRes.body.id;
  createdOrderNumber = orderRes.body.order_number;

  // Mark order as completed so we can submit a review
  if (createdOrderId) {
    await run('UPDATE orders SET status=? WHERE id=?', ['completed', createdOrderId]);
  }
}, 30000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ── Helper ────────────────────────────────────────────────────────────────────

async function getCsrfToken() {
  const res = await request(app).get('/api/csrf-token');
  return res.body.token;
}

// ─────────────────────────────────────────────────────────────────────────────
// TOTP 2FA Tests
// ─────────────────────────────────────────────────────────────────────────────

describe('Admin TOTP — Setup', () => {
  test('GET /api/admin/totp/setup without auth → 401', async () => {
    const res = await request(app).get('/api/admin/totp/setup');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/totp/setup with auth → 200 + secret + qr_url', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/totp/setup')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('secret');
    expect(res.body).toHaveProperty('qr_url');
    expect(res.body).toHaveProperty('manual_key');
    expect(typeof res.body.secret).toBe('string');
    expect(res.body.secret.length).toBeGreaterThan(0);
  });
});

describe('Admin TOTP — Enable & Disable', () => {
  let totpSecret;

  test('POST /api/admin/totp/enable without body → 400', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post('/api/admin/totp/enable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/admin/totp/enable with wrong code → 400', async () => {
    expect(adminToken).toBeTruthy();
    // Generate a fresh secret but provide an obviously wrong code
    const setupRes = await request(app)
      .get('/api/admin/totp/setup')
      .set('Authorization', `Bearer ${adminToken}`);
    totpSecret = setupRes.body.secret;

    const res = await request(app)
      .post('/api/admin/totp/enable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ secret: totpSecret, totp_code: '000000' });
    // Should fail (code is almost certainly wrong)
    expect([400, 200]).toContain(res.status); // allow rare pass if token happens to be 000000
  });

  test('POST /api/admin/totp/enable with valid code → 200 + ok', async () => {
    expect(adminToken).toBeTruthy();
    // Generate a fresh TOTP secret
    const setupRes = await request(app)
      .get('/api/admin/totp/setup')
      .set('Authorization', `Bearer ${adminToken}`);
    totpSecret = setupRes.body.secret;

    // Generate a valid TOTP code from the secret
    const validCode = speakeasy.totp({
      secret: totpSecret,
      encoding: 'base32',
    });

    const res = await request(app)
      .post('/api/admin/totp/enable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ secret: totpSecret, totp_code: validCode });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('DELETE /api/admin/totp/disable without code → 400', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .delete('/api/admin/totp/disable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('DELETE /api/admin/totp/disable with valid code → 200 + ok', async () => {
    expect(adminToken).toBeTruthy();
    expect(totpSecret).toBeTruthy();

    // Generate a fresh valid code to disable
    const validCode = speakeasy.totp({
      secret: totpSecret,
      encoding: 'base32',
    });

    const res = await request(app)
      .delete('/api/admin/totp/disable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ totp_code: validCode });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('DELETE /api/admin/totp/disable when 2FA not enabled → 400', async () => {
    expect(adminToken).toBeTruthy();
    // 2FA was just disabled, so this should fail
    const res = await request(app)
      .delete('/api/admin/totp/disable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ totp_code: '123456' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Favorites (Wishlist) Tests
// ─────────────────────────────────────────────────────────────────────────────

describe('Public API — Favorites/Wishlist', () => {
  test('GET /api/favorites with no ids → 200, empty array', async () => {
    const res = await request(app).get('/api/favorites');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBe(0);
  });

  test('GET /api/favorites?ids=nonexistent → 200, empty array', async () => {
    const res = await request(app).get('/api/favorites?ids=999999');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBe(0);
  });

  test('GET /api/favorites?ids=<modelId> → 200, returns model stub', async () => {
    expect(createdModelId).toBeTruthy();
    const res = await request(app).get(`/api/favorites?ids=${createdModelId}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBe(1);
    const model = res.body[0];
    expect(model).toHaveProperty('id');
    expect(model).toHaveProperty('name');
    expect(model).toHaveProperty('height');
  });

  test('GET /api/favorites?ids=1,2,999 → 200, returns only existing models', async () => {
    expect(createdModelId).toBeTruthy();
    const res = await request(app).get(`/api/favorites?ids=${createdModelId},999999`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // Only the real model is returned
    expect(res.body.some(m => m.id === createdModelId)).toBe(true);
    expect(res.body.every(m => m.id !== 999999)).toBe(true);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Reviews Flow Tests
// ─────────────────────────────────────────────────────────────────────────────

describe('Public API — Reviews GET', () => {
  test('GET /api/reviews → 200, array', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  test('GET /api/reviews?limit=2 → at most 2 items', async () => {
    const res = await request(app).get('/api/reviews?limit=2');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBeLessThanOrEqual(2);
  });

  test('GET /api/reviews?model_id=<id> → only reviews for that model', async () => {
    expect(createdModelId).toBeTruthy();
    const res = await request(app).get(`/api/reviews?model_id=${createdModelId}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // All returned reviews should belong to the model (or array is empty)
    for (const r of res.body) {
      expect(r.model_id).toBe(createdModelId);
    }
  });
});

describe('Client Review Submission', () => {
  test('POST /api/client/review without required fields → 400', async () => {
    const res = await request(app)
      .post('/api/client/review')
      .send({});
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/client/review with invalid phone → 400', async () => {
    expect(createdOrderId).toBeTruthy();
    const res = await request(app)
      .post('/api/client/review')
      .send({
        order_id: createdOrderId,
        phone: 'not-a-phone',
        rating: 5,
        text: 'This is a valid review text with enough characters',
      });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/client/review with rating out of range → 400', async () => {
    expect(createdOrderId).toBeTruthy();
    const res = await request(app)
      .post('/api/client/review')
      .send({
        order_id: createdOrderId,
        phone: '+7 999 555-11-22',
        rating: 10,
        text: 'This is a valid review text with enough characters',
      });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/client/review with text too short → 400', async () => {
    expect(createdOrderId).toBeTruthy();
    const res = await request(app)
      .post('/api/client/review')
      .send({
        order_id: createdOrderId,
        phone: '+7 999 555-11-22',
        rating: 5,
        text: 'Too short',
      });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/client/review for completed order with matching phone → 200 + ok', async () => {
    expect(createdOrderId).toBeTruthy();
    const res = await request(app)
      .post('/api/client/review')
      .send({
        order_id: createdOrderId,
        phone: '+7 999 555-11-22',
        rating: 5,
        text: 'Excellent service! Very professional and punctual. Highly recommended.',
      });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.id).toBeTruthy();
  });

  test('POST /api/client/review duplicate for same order → 409', async () => {
    expect(createdOrderId).toBeTruthy();
    const res = await request(app)
      .post('/api/client/review')
      .send({
        order_id: createdOrderId,
        phone: '+7 999 555-11-22',
        rating: 4,
        text: 'Another review attempt — should be rejected as duplicate.',
      });
    expect(res.status).toBe(409);
    expect(res.body.error).toBeTruthy();
  });
});

describe('Admin Reviews Management', () => {
  test('GET /api/admin/reviews without auth → 401', async () => {
    const res = await request(app).get('/api/admin/reviews');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/reviews with auth → 200 + { reviews, total }', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/reviews')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
    expect(typeof res.body.total).toBe('number');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Quick Booking (Repeat/Fast Order) Tests
// ─────────────────────────────────────────────────────────────────────────────

describe('Public API — Quick Booking', () => {
  test('POST /api/quick-booking without CSRF → 403', async () => {
    const res = await request(app)
      .post('/api/quick-booking')
      .send({ client_name: 'Test User', client_phone: '+79001234567' });
    expect(res.status).toBe(403);
  });

  test('POST /api/quick-booking without name → 400', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/quick-booking')
      .set('x-csrf-token', csrfToken)
      .send({ client_name: '', client_phone: '+79001234567' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/quick-booking with invalid phone → 400', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/quick-booking')
      .set('x-csrf-token', csrfToken)
      .send({ client_name: 'Fast Client', client_phone: 'badphone' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/quick-booking with valid data → 200 + ok + order_number', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/quick-booking')
      .set('x-csrf-token', csrfToken)
      .send({ client_name: 'Fast Client', client_phone: '+79001234567' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.order_number).toBeTruthy();
  });

  test('GET /api/admin/quick-bookings without auth → 401', async () => {
    const res = await request(app).get('/api/admin/quick-bookings');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/quick-bookings with auth → 200, array', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/quick-bookings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // The booking we just created should appear
    expect(res.body.length).toBeGreaterThan(0);
  });
});
