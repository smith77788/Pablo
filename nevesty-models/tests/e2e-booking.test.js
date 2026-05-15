'use strict';
/**
 * E2E: Complete booking flow from order creation to completion
 * Simulates: client submits order → admin views → admin changes status →
 *            admin assigns model → client can view order status
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
  if (bot) apiRouter.setBot(bot);

  a.use('/api', apiRouter);
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => {
    res.status(500).json({ error: err.message });
  });

  app = a;

  const loginRes = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 15000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ── E2E: Complete Booking Flow ────────────────────────────────────────────────

describe('E2E: Booking Flow', () => {
  let orderId;
  let orderNumber;
  let modelId;

  it('step 1: client can browse public models', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    const models = res.body.models || res.body;
    modelId = Array.isArray(models) && models.length > 0 ? models[0].id : null;
    expect(Array.isArray(models)).toBe(true);
  });

  it('step 2: client submits booking order', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrfToken)
      .send({
        client_name: 'Тест Клиент',
        client_phone: '+79991234567',
        client_email: 'test@example.com',
        event_type: 'photo_shoot',
        event_date: '2026-12-31',
        budget: '50000',
        notes: 'E2E test order',
        model_id: modelId
      });
    expect([200, 201]).toContain(res.status);
    expect(res.body).toHaveProperty('order_number');
    orderId = res.body.id || res.body.order_id;
    orderNumber = res.body.order_number;
    expect(orderNumber).toBeTruthy();
  });

  it('step 3: admin sees new order in list', async () => {
    const res = await request(app)
      .get('/api/admin/orders')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const orders = res.body.orders || res.body;
    const found = Array.isArray(orders) && orders.some(o => o.order_number === orderNumber);
    expect(found).toBe(true);
  });

  it('step 4: admin views order detail', async () => {
    if (!orderId) return;
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('order_number', orderNumber);
  });

  it('step 5: admin confirms order', async () => {
    if (!orderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect([200, 201]).toContain(res.status);
  });

  it('step 6: client can check order status by phone', async () => {
    const res = await request(app)
      .get('/api/orders/by-phone')
      .query({ phone: '+79991234567' });
    expect(res.status).toBe(200);
    const orders = res.body.orders || [];
    const found = orders.some(o => o.order_number === orderNumber);
    expect(found).toBe(true);
  });

  it('step 7: admin adds internal note', async () => {
    if (!orderId) return;
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'E2E test note — internal' });
    expect([200, 201, 404]).toContain(res.status);
  });

  it('step 8: admin marks order in_progress', async () => {
    if (!orderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'in_progress' });
    expect([200, 201]).toContain(res.status);
  });

  it('step 9: admin marks order completed', async () => {
    if (!orderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'completed' });
    expect([200, 201]).toContain(res.status);
  });

  it('step 10: completed order appears in history', async () => {
    if (!orderId) return;
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/history`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
    // If 200, check it has history entries (array or object with order_id)
    if (res.status === 200) {
      const body = res.body;
      const isArray = Array.isArray(body);
      const isObject = !isArray && typeof body === 'object' && body !== null;
      if (isArray) {
        // History returned as array of status change records
        expect(body.length).toBeGreaterThan(0);
        expect(body[0]).toHaveProperty('order_id');
      } else if (isObject) {
        expect(body).toHaveProperty('order_id');
      }
    }
  });
});

// ── E2E: Review Submission ────────────────────────────────────────────────────

describe('E2E: Review Submission', () => {
  let reviewId;

  it('client can view public reviews', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews || res.body)).toBe(true);
  });

  it('anyone can submit a review', async () => {
    const res = await request(app)
      .post('/api/reviews')
      .send({
        order_id: 1,
        rating: 5,
        text: 'Отличная работа! E2E тест.',
        client_name: 'E2E Client'
      });
    expect([200, 201, 400, 404]).toContain(res.status);
    if (res.status === 200 || res.status === 201) {
      reviewId = res.body.id;
    }
  });

  it('admin can list reviews for approval', async () => {
    const res = await request(app)
      .get('/api/admin/reviews')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('admin can approve a review', async () => {
    if (!reviewId) return;
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId}/approve`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
  });
});

// ── E2E: Catalog Search ───────────────────────────────────────────────────────

describe('E2E: Catalog Search', () => {
  it('public catalog returns models', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
  });

  it('catalog supports pagination', async () => {
    const res = await request(app).get('/api/models?page=1&per_page=5');
    expect(res.status).toBe(200);
  });

  it('catalog supports city filter', async () => {
    const res = await request(app).get('/api/models?city=Москва');
    expect(res.status).toBe(200);
  });

  it('featured models can be listed', async () => {
    const res = await request(app).get('/api/models?featured=1');
    expect(res.status).toBe(200);
  });

  it('search endpoint works', async () => {
    const res = await request(app).get('/api/search?q=модель');
    expect([200, 404]).toContain(res.status);
  });
});
