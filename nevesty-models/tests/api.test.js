'use strict';
/**
 * API Integration Tests — Nevesty Models
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
let createdOrderId;
let createdOrderNumber;
let createdModelId;

// ── Server bootstrap ─────────────────────────────────────────────────────────

beforeAll(async () => {
  const { initDatabase } = require('../database');
  await initDatabase();

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());

  // Bot is disabled (empty token) — initBot returns null safely
  const bot = initBot(a);
  if (bot) apiRouter.setBot(bot);

  a.use('/api', apiRouter);

  // Simple health endpoint (mirrors server.js)
  const { get: dbGet } = require('../database');
  a.get('/health', async (req, res) => {
    try {
      await dbGet('SELECT 1 as ok');
      res.json({ status: 'ok', uptime: process.uptime(), ts: new Date().toISOString() });
    } catch (e) {
      res.status(503).json({ status: 'down', error: e.message });
    }
  });

  // Global error handler
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => {
    res.status(500).json({ error: err.message });
  });

  app = a;
}, 30000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ── Health ───────────────────────────────────────────────────────────────────

describe('Health', () => {
  test('GET /health → 200 + status ok', async () => {
    const res = await request(app).get('/health');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ok');
  });
});

// ── Config ───────────────────────────────────────────────────────────────────

describe('Config', () => {
  test('GET /api/config → 200 + bot_username field', async () => {
    const res = await request(app).get('/api/config');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('bot_username');
  });
});

// ── Auth ─────────────────────────────────────────────────────────────────────

describe('Auth', () => {
  test('POST /api/admin/login with valid credentials → 200 + token', async () => {
    const res = await request(app)
      .post('/api/admin/login')
      .send({ username: 'admin', password: 'admin123' });
    expect(res.status).toBe(200);
    expect(res.body.token).toBeTruthy();
    expect(res.body.admin).toBeDefined();
    expect(res.body.admin.username).toBe('admin');
    adminToken = res.body.token;
  });

  test('POST /api/admin/login with wrong password → 401', async () => {
    const res = await request(app)
      .post('/api/admin/login')
      .send({ username: 'admin', password: 'wrongpassword' });
    expect(res.status).toBe(401);
    expect(res.body.error).toBeTruthy();
  });

  test('GET /api/admin/orders without token → 401', async () => {
    const res = await request(app).get('/api/admin/orders');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/models without token → 401', async () => {
    const res = await request(app).get('/api/admin/models');
    expect(res.status).toBe(401);
  });
});

// ── Public: Models ───────────────────────────────────────────────────────────

describe('Public API — Models', () => {
  test('GET /api/models → 200, array', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  test('GET /api/models?available=1 → only available models', async () => {
    const res = await request(app).get('/api/models?available=1');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    for (const model of res.body) {
      expect(model.available).toBe(1);
    }
  });

  test('GET /api/models/:id → 200 with name + height fields', async () => {
    const listRes = await request(app).get('/api/models');
    expect(listRes.body.length).toBeGreaterThan(0);
    const firstId = listRes.body[0].id;

    const res = await request(app).get(`/api/models/${firstId}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('name');
    expect(res.body).toHaveProperty('height');
  });

  test('GET /api/models/:id with non-existent id → 404', async () => {
    const res = await request(app).get('/api/models/999999');
    expect(res.status).toBe(404);
  });
});

// ── Public: Settings ─────────────────────────────────────────────────────────

describe('Public API — Settings', () => {
  test('GET /api/settings/public → 200, object', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    expect(typeof res.body).toBe('object');
    expect(Array.isArray(res.body)).toBe(false);
  });
});

// ── Public: Reviews ───────────────────────────────────────────────────────────

describe('Public API — Reviews', () => {
  test('GET /api/reviews/public → 200, array', async () => {
    const res = await request(app).get('/api/reviews/public');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  test('GET /api/reviews/public?limit=3 → max 3 items', async () => {
    const res = await request(app).get('/api/reviews/public?limit=3');
    expect(res.status).toBe(200);
    expect(res.body.length).toBeLessThanOrEqual(3);
  });
});

// ── Public: FAQ ───────────────────────────────────────────────────────────────

describe('Public API — FAQ', () => {
  test('GET /api/faq → 200, array', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

// ── Helper: get a fresh CSRF token ────────────────────────────────────────────

async function getCsrfToken() {
  const res = await request(app).get('/api/csrf-token');
  return res.body.token;
}

// ── Public: Orders ────────────────────────────────────────────────────────────

describe('Public API — Orders', () => {
  test('POST /api/orders with valid body → 200 or 201 + order_number', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrfToken)
      .send({
        client_name: 'Тест Клиент',
        client_phone: '+7 999 123-45-67',
        event_type: 'photo_shoot',
        event_date: '2026-08-15',
        event_duration: 4,
      });
    expect([200, 201]).toContain(res.status);
    expect(res.body.order_number).toBeTruthy();
    expect(res.body.id).toBeTruthy();
    createdOrderId = res.body.id;
    createdOrderNumber = res.body.order_number;
  });

  test('POST /api/orders without client_name → 400', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrfToken)
      .send({ client_phone: '+7 999 123-45-67', event_type: 'photo_shoot' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('POST /api/orders with invalid phone → 400', async () => {
    const csrfToken = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrfToken)
      .send({ client_name: 'Тест Клиент', client_phone: 'not-a-phone', event_type: 'photo_shoot' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  test('GET /api/orders/status/:order_number → 200 with status field', async () => {
    expect(createdOrderNumber).toBeTruthy();
    const res = await request(app).get(`/api/orders/status/${createdOrderNumber}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('order_number');
    expect(res.body).toHaveProperty('status');
  });
});

// ── Contact form ──────────────────────────────────────────────────────────────

describe('Public API — Contact', () => {
  test('POST /api/contact → 200, 201 or 429 (rate limited)', async () => {
    const res = await request(app)
      .post('/api/contact')
      .send({ name: 'Test User', phone: '+79001234567', message: 'Test message from CI' });
    expect([200, 201, 429]).toContain(res.status);
  });
});

// ── Admin: Orders (protected) ─────────────────────────────────────────────────

describe('Admin API — Orders', () => {
  test('GET /api/admin/orders → 200 + { orders, total, page, pages }', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/orders')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(typeof res.body.total).toBe('number');
    expect(typeof res.body.page).toBe('number');
    expect(typeof res.body.pages).toBe('number');
  });

  test('GET /api/admin/orders?page=1&limit=5 → respects limit', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/orders?page=1&limit=5')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(res.body.orders.length).toBeLessThanOrEqual(5);
    expect(res.body.page).toBe(1);
  });

  test('PUT /api/admin/orders/:id with status="confirmed" → 200', async () => {
    expect(adminToken).toBeTruthy();
    expect(createdOrderId).toBeTruthy();
    const res = await request(app)
      .put(`/api/admin/orders/${createdOrderId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/admin/orders/:id with invalid status → 400', async () => {
    expect(adminToken).toBeTruthy();
    expect(createdOrderId).toBeTruthy();
    const res = await request(app)
      .put(`/api/admin/orders/${createdOrderId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'invalid_status_xyz' });
    expect(res.status).toBe(400);
  });
});

// ── Admin: Models (protected) ─────────────────────────────────────────────────

describe('Admin API — Models', () => {
  test('POST /api/admin/models/json → 200 or 201 (create model, auth)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .post('/api/admin/models/json')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({
        name: 'Test Model CI',
        height: 175,
        weight: 55,
        bust: 88,
        waist: 62,
        hips: 90,
        shoe_size: '38',
        age: 23,
        category: 'fashion',
        city: 'Kyiv',
        available: 1,
      });
    expect([200, 201]).toContain(res.status);
    expect(res.body.id || res.body.model_id || res.body.success).toBeTruthy();
    createdModelId = res.body.id || createdModelId;
  });

  test('GET /api/admin/models → 200 + models array (auth)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/models')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const models = res.body.models || res.body;
    expect(Array.isArray(models)).toBe(true);
  });
});

// ── Admin: Stats & Audit (protected) ─────────────────────────────────────────

describe('Admin API — Stats & Audit', () => {
  test('GET /api/admin/stats → 200 with orders field (auth)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(
      res.body.hasOwnProperty('new_orders') || res.body.hasOwnProperty('orders_today') || res.body.hasOwnProperty('total_orders')
    ).toBe(true);
  });

  test('GET /api/admin/db-stats → 200 with tables array (auth)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/db-stats')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.tables)).toBe(true);
    expect(res.body.tables.length).toBeGreaterThan(0);
  });

  test('GET /api/admin/audit-log → 200, has rows (auth)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/audit-log')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // endpoint returns { rows: [], total: N } or plain array (both valid)
    const isArray = Array.isArray(res.body);
    const isObj = res.body && typeof res.body === 'object' && Array.isArray(res.body.rows);
    expect(isArray || isObj).toBe(true);
  });

  test('GET /api/admin/managers → 200, array (auth)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/managers')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  test('GET /api/admin/factory-tasks → 200 (auth)', async () => {
    expect(adminToken).toBeTruthy();
    const res = await request(app)
      .get('/api/admin/factory-tasks')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.hasOwnProperty('tasks') || Array.isArray(res.body)).toBe(true);
  });
});
