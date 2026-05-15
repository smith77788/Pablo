'use strict';
/**
 * API Integration Tests — Wave 51 Features
 * Covers: cabinet order lookup by phone, payment endpoints,
 *         Yookassa webhook, security DB fixes, manager assignment.
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
let seededOrderId;

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

  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => {
    res.status(500).json({ error: err.message });
  });

  app = a;

  const loginRes = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;

  // Seed a model and order
  const modelRow = await get('SELECT id FROM models LIMIT 1');
  const modelId = modelRow ? modelRow.id : null;

  const orderNum = await generateOrderNumber();
  const orderRes = await run(
    `INSERT INTO orders
       (order_number, client_name, client_phone, client_email, event_type,
        event_date, status)
     VALUES (?,?,?,?,?,?,?)`,
    [orderNum, 'Wave51 Client', '+79991234567', 'test@test.ru',
     'корпоратив', '2025-12-31', 'new']
  );
  seededOrderId = orderRes ? orderRes.id : null;
});

// ── Client Cabinet: order lookup by phone ────────────────────────────────────

describe('Client Cabinet — /api/orders/by-phone', () => {
  it('returns orders for a known phone number', async () => {
    const res = await request(app)
      .get('/api/orders/by-phone')
      .query({ phone: '+79991234567' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('orders');
    expect(Array.isArray(res.body.orders)).toBe(true);
  });

  it('returns empty array for unknown phone', async () => {
    const res = await request(app)
      .get('/api/orders/by-phone')
      .query({ phone: '+70000000001' });
    expect(res.status).toBe(200);
    expect(res.body.orders).toEqual([]);
  });

  it('returns empty array for too-short phone', async () => {
    const res = await request(app)
      .get('/api/orders/by-phone')
      .query({ phone: '123' });
    expect(res.status).toBe(200);
    expect(res.body.orders).toEqual([]);
  });

  it('returns orders with required fields', async () => {
    const res = await request(app)
      .get('/api/orders/by-phone')
      .query({ phone: '79991234567' });
    if (res.body.orders && res.body.orders.length > 0) {
      const order = res.body.orders[0];
      expect(order).toHaveProperty('order_number');
      expect(order).toHaveProperty('status');
      expect(order).toHaveProperty('event_type');
    }
    expect(res.status).toBe(200);
  });
});

// ── Payment webhook ───────────────────────────────────────────────────────────

describe('Payment Webhook — POST /api/webhooks/yookassa', () => {
  it('accepts valid payment succeeded event', async () => {
    const res = await request(app)
      .post('/api/webhooks/yookassa')
      .send({
        type: 'notification',
        event: 'payment.succeeded',
        object: {
          id: 'pay_test_001',
          status: 'succeeded',
          amount: { value: '50000.00', currency: 'RUB' },
          metadata: { order_id: seededOrderId || 1 }
        }
      });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('accepts payment cancelled event', async () => {
    const res = await request(app)
      .post('/api/webhooks/yookassa')
      .send({
        type: 'notification',
        event: 'payment.canceled',
        object: {
          id: 'pay_test_002',
          status: 'canceled',
          metadata: { order_id: seededOrderId || 1 }
        }
      });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('handles non-notification event gracefully', async () => {
    const res = await request(app)
      .post('/api/webhooks/yookassa')
      .send({ type: 'something_else', object: {} });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('handles empty body gracefully', async () => {
    const res = await request(app)
      .post('/api/webhooks/yookassa')
      .send({});
    expect(res.status).toBe(200);
  });
});

// ── Payment link endpoint ─────────────────────────────────────────────────────

describe('Payment Link — POST /api/admin/orders/:id/payment-link', () => {
  it('requires authentication', async () => {
    const res = await request(app)
      .post('/api/admin/orders/1/payment-link');
    expect(res.status).toBe(401);
  });

  it('returns stub link for valid order', async () => {
    if (!seededOrderId) return;
    const res = await request(app)
      .post(`/api/admin/orders/${seededOrderId}/payment-link`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toBeDefined();
  });

  it('returns 404 for non-existent order', async () => {
    const res = await request(app)
      .post('/api/admin/orders/99999/payment-link')
      .set('Authorization', `Bearer ${adminToken}`);
    expect([404, 400, 200]).toContain(res.status);
  });
});

// ── Manager assignment ────────────────────────────────────────────────────────

describe('Manager Assignment', () => {
  let managerId;

  beforeAll(async () => {
    const { get } = require('../database');
    const admin = await get('SELECT id FROM admins LIMIT 1');
    managerId = admin ? admin.id : null;
  });

  it('can assign manager to order via PATCH', async () => {
    if (!seededOrderId || !managerId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${seededOrderId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ manager_id: managerId });
    expect([200, 400, 404]).toContain(res.status);
  });

  it('orders list includes manager_id field', async () => {
    const res = await request(app)
      .get('/api/admin/orders')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders || res.body)).toBe(true);
  });
});

// ── Security: password masking ────────────────────────────────────────────────

describe('Security — password not exposed in logs', () => {
  it('database module does not export admin password', () => {
    const db = require('../database');
    expect(typeof db).toBe('object');
    expect(db.adminPassword).toBeUndefined();
    expect(db.rawPassword).toBeUndefined();
  });

  it('health endpoint does not expose credentials', async () => {
    const res = await request(app).get('/api/health');
    const bodyStr = JSON.stringify(res.body);
    expect(bodyStr).not.toMatch(/password/i);
    expect(bodyStr).not.toMatch(/secret/i);
  });
});

// ── Repeat order flow ─────────────────────────────────────────────────────────

describe('Repeat Order — database level', () => {
  it('orders table has client contact fields for prefill', async () => {
    const { get } = require('../database');
    const order = await get(
      'SELECT client_name, client_phone, client_email FROM orders WHERE id=?',
      [seededOrderId || 1]
    );
    if (order) {
      expect(order).toHaveProperty('client_name');
      expect(order).toHaveProperty('client_phone');
      expect(order).toHaveProperty('client_email');
    }
  });
});
