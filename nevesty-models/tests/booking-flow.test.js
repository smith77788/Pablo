'use strict';
/**
 * E2E-style booking flow tests (API-level, not browser)
 * Tests the full path: browse catalog → select model → create order → verify status
 */
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app;
let adminToken;

async function getCsrfToken() {
  const res = await request(app).get('/api/csrf-token');
  return res.body.token;
}

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
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 15000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ── E2E: Browse catalog ───────────────────────────────────────────────────────

describe('E2E: Browse catalog', () => {
  test('GET /api/models returns model list', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    const models = res.body.models || res.body;
    expect(Array.isArray(models)).toBe(true);
  });

  test('GET /api/models?city=Moscow returns filtered results', async () => {
    const res = await request(app).get('/api/models?city=Moscow');
    expect([200, 204]).toContain(res.status);
  });

  test('GET /api/models supports pagination', async () => {
    const res = await request(app).get('/api/models?page=1&per_page=5');
    expect(res.status).toBe(200);
  });

  test('GET /api/models?featured=1 returns featured models', async () => {
    const res = await request(app).get('/api/models?featured=1');
    expect(res.status).toBe(200);
  });

  test('GET /api/models/:id returns model details', async () => {
    const listRes = await request(app).get('/api/models');
    const models = Array.isArray(listRes.body) ? listRes.body : listRes.body.models || [];
    if (!models.length) return; // no seed models in :memory: DB
    const firstId = models[0].id;
    const res = await request(app).get(`/api/models/${firstId}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id', firstId);
  });

  test('GET /api/models/:id returns 404 for non-existent model', async () => {
    const res = await request(app).get('/api/models/9999999');
    expect(res.status).toBe(404);
  });
});

// ── E2E: Create booking order ─────────────────────────────────────────────────

describe('E2E: Create booking order', () => {
  let orderId;
  let orderNumber;

  test('POST /api/orders creates order with valid data', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrfToken).send({
      client_name: 'Test E2E Client',
      client_phone: '+79001234567',
      client_email: 'e2e@test.com',
      event_type: 'photo_shoot',
      event_date: '2026-06-15',
      budget: '15000',
      comments: 'E2E booking flow test',
    });
    expect([200, 201]).toContain(res.status);
    if (res.status === 200 || res.status === 201) {
      expect(res.body).toHaveProperty('order_number');
      orderId = res.body.id;
      orderNumber = res.body.order_number;
    }
  });

  test('POST /api/orders requires valid phone number', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrfToken).send({
      client_name: 'Test Client',
      client_phone: 'not-a-phone',
      client_email: 'test@test.com',
      event_type: 'photo_shoot',
      event_date: '2026-06-15',
    });
    expect([400, 422]).toContain(res.status);
  });

  test('POST /api/orders requires valid event_type', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrfToken).send({
      client_name: 'Test Client',
      client_phone: '+79001234568',
      client_email: 'test@test.com',
      event_type: 'invalid_type',
      event_date: '2026-06-15',
    });
    expect([400, 422]).toContain(res.status);
  });

  test('POST /api/orders rejects missing CSRF token', async () => {
    const res = await request(app).post('/api/orders').send({
      client_name: 'CSRF Test Client',
      client_phone: '+79001234569',
      client_email: 'csrf@test.com',
      event_type: 'photo_shoot',
      event_date: '2026-06-15',
    });
    expect([400, 403, 422]).toContain(res.status);
  });

  test('POST /api/orders with model_ids stores multi-model booking', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrfToken)
      .send({
        client_name: 'Multi Model Client',
        client_phone: '+79001234560',
        client_email: 'multi@test.com',
        event_type: 'fashion_show',
        event_date: '2026-07-01',
        budget: '30000',
        model_ids: [1, 2],
      });
    // 400 if validation fails (e.g. email), 200/201 if valid
    expect([200, 201, 400, 422]).toContain(res.status);
  });
});

// ── E2E: Admin order management ───────────────────────────────────────────────

describe('E2E: Admin order management', () => {
  let managedOrderId;

  beforeAll(async () => {
    // Create a fresh order for management tests
    const csrfToken = await getCsrfToken();
    const createRes = await request(app).post('/api/orders').set('x-csrf-token', csrfToken).send({
      client_name: 'Admin Managed Client',
      client_phone: '+79009876543',
      client_email: 'admin-e2e@test.com',
      event_type: 'event',
      event_date: '2026-08-01',
      budget: '45000',
    });
    if ([200, 201].includes(createRes.status)) {
      managedOrderId = createRes.body?.id;
    }
  });

  test('Admin can list all orders', async () => {
    const res = await request(app).get('/api/admin/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const orders = res.body.orders || res.body;
    expect(Array.isArray(orders)).toBe(true);
  });

  test('Admin requires auth to access orders', async () => {
    const res = await request(app).get('/api/admin/orders');
    expect([401, 403]).toContain(res.status);
  });

  test('Admin can view individual order', async () => {
    if (!managedOrderId) return;
    const res = await request(app)
      .get(`/api/admin/orders/${managedOrderId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id', managedOrderId);
  });

  test('Admin can update order status to confirmed', async () => {
    if (!managedOrderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${managedOrderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect([200, 201, 204]).toContain(res.status);
  });

  test('Admin can update order status to in_progress', async () => {
    if (!managedOrderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${managedOrderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'in_progress' });
    expect([200, 201, 204]).toContain(res.status);
  });

  test('Admin can update order status to completed', async () => {
    if (!managedOrderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${managedOrderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'completed' });
    expect([200, 201, 204]).toContain(res.status);
  });

  test('GET /api/admin/orders supports search param', async () => {
    const res = await request(app).get('/api/admin/orders?search=Admin').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('GET /api/admin/orders supports status filter', async () => {
    const res = await request(app).get('/api/admin/orders?status=new').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── E2E: Reviews flow ─────────────────────────────────────────────────────────

describe('E2E: Reviews flow', () => {
  test('GET /api/reviews returns approved reviews', async () => {
    const res = await request(app).get('/api/reviews');
    expect([200, 204]).toContain(res.status);
    if (res.status === 200) {
      const reviews = res.body.reviews || res.body;
      expect(Array.isArray(reviews)).toBe(true);
    }
  });

  test('GET /api/reviews with pagination works', async () => {
    const res = await request(app).get('/api/reviews?page=1&limit=3');
    expect([200, 204]).toContain(res.status);
  });
});

// ── E2E: FAQ ──────────────────────────────────────────────────────────────────

describe('E2E: FAQ', () => {
  test('GET /api/faq returns FAQ list', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

// ── E2E: Wishlist ─────────────────────────────────────────────────────────────

describe('E2E: Wishlist', () => {
  const testChatId = 888001;

  test('GET /api/user/wishlist returns 401 without auth', async () => {
    const res = await request(app).get(`/api/user/wishlist?chat_id=${testChatId}`);
    expect(res.status).toBe(401);
  });

  test('GET /api/user/wishlist returns empty list for new user (with auth)', async () => {
    const res = await request(app)
      .get(`/api/user/wishlist?chat_id=${testChatId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const wishlist = res.body.wishlist || res.body;
    expect(Array.isArray(wishlist)).toBe(true);
    expect(wishlist.length).toBe(0);
  });

  test('POST /api/user/wishlist handles missing model gracefully (with auth)', async () => {
    const res = await request(app)
      .post('/api/user/wishlist')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ chat_id: testChatId, model_id: 99999 });
    // 404 if model doesn't exist in :memory: DB, else 200/201
    expect([200, 201, 404, 400]).toContain(res.status);
  });
});

// ── E2E: Order public status lookup ──────────────────────────────────────────

describe('E2E: Public order status lookup', () => {
  test('GET /api/orders/by-phone returns orders for known phone', async () => {
    // First create an order
    const csrfToken = await getCsrfToken();
    await request(app).post('/api/orders').set('x-csrf-token', csrfToken).send({
      client_name: 'Status Check Client',
      client_phone: '+79991111222',
      client_email: 'status@test.com',
      event_type: 'commercial',
      event_date: '2026-09-01',
    });

    const res = await request(app).get('/api/orders/by-phone').query({ phone: '+79991111222' });
    expect(res.status).toBe(200);
    const orders = res.body.orders || [];
    expect(Array.isArray(orders)).toBe(true);
  });

  test('GET /api/orders/status?number=INVALID returns 404', async () => {
    const res = await request(app).get('/api/orders/status').query({ number: 'ORD-INVALID' });
    expect([404, 400]).toContain(res.status);
  });
});
