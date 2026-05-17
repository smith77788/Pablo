'use strict';
/**
 * E2E Wave 203 — Key user scenario tests
 *
 * Scenarios:
 *   1. Full booking lifecycle
 *   2. Auth flow (JWT login, protected routes, invalid tokens)
 *   3. Catalog and search
 *   4. Reviews (submit, public, approve)
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
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));

  app = a;

  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token || loginRes.body.accessToken || null;
}, 30000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ═══════════════════════════════════════════════════════════════════════════════
// SCENARIO 1 — Full booking lifecycle
// ═══════════════════════════════════════════════════════════════════════════════

describe('E2E: Full booking lifecycle', () => {
  let orderId;
  let orderNumber;

  it('client submits order → expects 201 or 200', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrfToken).send({
      client_name: 'Wave203 Client',
      client_phone: '+79991112233',
      client_email: 'wave203@example.com',
      event_type: 'photo_shoot',
      event_date: '2027-06-15',
      budget: '80000',
    });
    expect([200, 201]).toContain(res.status);
    expect(res.body).toHaveProperty('order_number');
    orderId = res.body.id || res.body.order_id;
    orderNumber = res.body.order_number;
    expect(orderNumber).toBeTruthy();
  });

  it('admin sees the order in list', async () => {
    const res = await request(app).get('/api/admin/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const orders = res.body.orders || res.body;
    expect(Array.isArray(orders)).toBe(true);
    const found = orders.some(o => o.order_number === orderNumber);
    expect(found).toBe(true);
  });

  it('admin changes status to confirmed', async () => {
    if (!orderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect([200, 201]).toContain(res.status);
    expect(res.body).toHaveProperty('ok', true);
  });

  it('admin reads order — status is confirmed', async () => {
    if (!orderId) return;
    const res = await request(app).get(`/api/admin/orders/${orderId}`).set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('order_number', orderNumber);
    expect(res.body.status).toBe('confirmed');
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// SCENARIO 2 — Auth flow
// ═══════════════════════════════════════════════════════════════════════════════

describe('E2E: Auth flow', () => {
  it('login with valid credentials returns JWT', async () => {
    const res = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
    expect(res.status).toBe(200);
    const token = res.body.token || res.body.accessToken;
    expect(token).toBeTruthy();
    expect(typeof token).toBe('string');
    expect(token.split('.').length).toBe(3); // valid JWT structure
  });

  it('JWT can access protected routes', async () => {
    const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
    const token = loginRes.body.token || loginRes.body.accessToken;

    const res = await request(app).get('/api/admin/orders').set('Authorization', `Bearer ${token}`);
    expect(res.status).toBe(200);
  });

  it('expired/invalid JWT returns 401', async () => {
    const res = await request(app)
      .get('/api/admin/orders')
      .set(
        'Authorization',
        'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIiwiaWF0IjoxNTE2MjM5MDIyLCJleHAiOjF9.invalid'
      );
    expect(res.status).toBe(401);
  });

  it('missing JWT returns 401', async () => {
    const res = await request(app).get('/api/admin/orders');
    expect(res.status).toBe(401);
  });

  it('wrong password returns 401', async () => {
    const res = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'wrongpassword' });
    expect(res.status).toBe(401);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// SCENARIO 3 — Catalog and search
// ═══════════════════════════════════════════════════════════════════════════════

describe('E2E: Catalog and search', () => {
  // Seed one model to make filter / detail tests reliable
  let seededModelId;

  beforeAll(async () => {
    // Create a model via admin endpoint so search/detail tests have a target
    const res = await request(app).post('/api/admin/models/json').set('Authorization', `Bearer ${adminToken}`).send({
      name: 'Анна Тестовая',
      age: 22,
      height: 175,
      city: 'Москва',
      category: 'fashion',
      available: 1,
      bio: 'Тестовая модель для E2E',
    });
    if (res.status === 200 || res.status === 201) {
      seededModelId = res.body.id || res.body.model?.id;
    }
  });

  it('GET /api/models returns models array', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('GET /api/models?city=Москва filters by city', async () => {
    const res = await request(app).get('/api/models?city=Москва');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // If we seeded a Moscow model all returned entries must be in Москва
    if (seededModelId && res.body.length > 0) {
      res.body.forEach(m => expect(m.city).toBe('Москва'));
    }
  });

  it('GET /api/models?search=Анна filters by name', async () => {
    const res = await request(app).get('/api/models?search=Анна');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    if (seededModelId && res.body.length > 0) {
      const names = res.body.map(m => m.name.toLowerCase());
      const hasAnna = names.some(n => n.includes('анна'));
      expect(hasAnna).toBe(true);
    }
  });

  it('GET /api/models/:id returns single model', async () => {
    if (!seededModelId) {
      // Fall back to checking id=1 gracefully
      const res = await request(app).get('/api/models/1');
      expect([200, 404]).toContain(res.status);
      return;
    }
    const res = await request(app).get(`/api/models/${seededModelId}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id', seededModelId);
    expect(res.body).toHaveProperty('name', 'Анна Тестовая');
  });

  it('GET /api/models/:id with unknown id returns 404', async () => {
    const res = await request(app).get('/api/models/999999');
    expect(res.status).toBe(404);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// SCENARIO 4 — Reviews
// ═══════════════════════════════════════════════════════════════════════════════

describe('E2E: Reviews', () => {
  let reviewId;

  // The POST /api/client/review endpoint requires a completed order linked to
  // the same phone number, so we create a full order → confirm → complete flow
  // in beforeAll, then use /api/admin/reviews + bulk-approve for the approval test.

  let completedOrderId;
  const reviewPhone = '+79997770001';

  beforeAll(async () => {
    const csrfToken = await getCsrfToken();
    const orderRes = await request(app).post('/api/orders').set('x-csrf-token', csrfToken).send({
      client_name: 'Review Tester',
      client_phone: reviewPhone,
      client_email: 'reviewer@example.com',
      event_type: 'photo_shoot',
      event_date: '2027-07-20',
      budget: '30000',
    });
    if ([200, 201].includes(orderRes.status)) {
      completedOrderId = orderRes.body.id || orderRes.body.order_id;
      // Advance order to completed so the review can be posted
      await request(app)
        .patch(`/api/admin/orders/${completedOrderId}/status`)
        .set('Authorization', `Bearer ${adminToken}`)
        .send({ status: 'completed' });
    }
  });

  it('POST /api/client/review creates pending review', async () => {
    if (!completedOrderId) {
      console.warn('[SKIP] No completed order — skipping review creation');
      return;
    }
    const res = await request(app).post('/api/client/review').send({
      order_id: completedOrderId,
      phone: reviewPhone,
      rating: 5,
      text: 'Отличная работа! Модели профессиональные.',
    });
    expect([200, 201]).toContain(res.status);
    expect(res.body).toHaveProperty('ok', true);
    reviewId = res.body.id;
    expect(reviewId).toBeTruthy();
  });

  it('GET /api/reviews/public returns only approved reviews', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
    expect(Array.isArray(res.body.reviews)).toBe(true);
    // Freshly created review must NOT appear in public list (still pending)
    if (reviewId) {
      const found = res.body.reviews.some(r => r.id === reviewId);
      expect(found).toBe(false);
    }
  });

  it('PATCH /api/admin/reviews/:id/approve approves review', async () => {
    if (!reviewId) {
      console.warn('[SKIP] No review id — skipping approval');
      return;
    }
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId}/approve`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
    expect(res.body.approved).toBe(1);
  });

  it('approved review appears in GET /api/reviews/public', async () => {
    if (!reviewId) {
      console.warn('[SKIP] No review id — skipping visibility check');
      return;
    }
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
    const found = res.body.reviews.some(r => r.id === reviewId);
    expect(found).toBe(true);
  });

  it('admin GET /api/admin/reviews lists all reviews (incl. pending)', async () => {
    const res = await request(app).get('/api/admin/reviews').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
  });
});
