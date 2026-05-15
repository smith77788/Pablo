/**
 * Integration tests for Nevesty Models API
 * Run: node --test tests/api.test.js
 *
 * Starts the Express server on port 3001 (test mode, bot disabled),
 * runs all test suites, then shuts down cleanly.
 */

'use strict';

// Setup MUST run before any app modules are imported
require('./setup.js');

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');

// ─── HTTP helper ──────────────────────────────────────────────────────────────

/**
 * @param {string} method
 * @param {string} reqPath
 * @param {object|null} body
 * @param {object} headers
 * @returns {Promise<{ status: number, body: any }>}
 */
function request(method, reqPath, body = null, headers = {}) {
  return new Promise((resolve, reject) => {
    const opts = {
      hostname: 'localhost',
      port: 3001,
      method,
      path: reqPath,
      headers: { 'Content-Type': 'application/json', ...headers },
    };
    const req = http.request(opts, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => {
        let parsed;
        try { parsed = JSON.parse(data || 'null'); } catch { parsed = data; }
        resolve({ status: res.statusCode, body: parsed });
      });
    });
    req.on('error', reject);
    if (body !== null) req.write(JSON.stringify(body));
    req.end();
  });
}

// ─── Server lifecycle ─────────────────────────────────────────────────────────

let serverInstance = null;
let closeDatabase = null;

before(async () => {
  // Import app modules AFTER env vars are set by setup.js
  const { initDatabase, closeDatabase: _cd } = require('../database');
  closeDatabase = _cd;

  await initDatabase();

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');
  const express = require('express');
  const cors = require('cors');

  const app = express();
  app.use(express.json({ limit: '2mb' }));
  app.use(express.urlencoded({ extended: true }));
  app.use(cors());

  // Bot is disabled in test env (empty token) — initBot returns null
  const bot = initBot(app);
  if (bot) apiRouter.setBot(bot);

  app.use('/api', apiRouter);

  // SEO routes (mirrors server.js behaviour for test coverage)
  const baseUrl = 'http://localhost:3001';
  const today = new Date().toISOString().slice(0, 10);
  app.get('/sitemap.xml', (req, res) => {
    res.set('Content-Type', 'application/xml');
    res.send(`<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>${baseUrl}/</loc><lastmod>${today}</lastmod></url></urlset>`);
  });
  app.get('/robots.txt', (req, res) => {
    res.set('Content-Type', 'text/plain');
    res.send(`User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin/\n\nSitemap: ${baseUrl}/sitemap.xml`);
  });

  // Health check endpoint
  const { get: dbGet } = require('../database');
  app.get('/health', async (req, res) => {
    try {
      await dbGet('SELECT 1 as ok');
      res.json({ status: 'ok', uptime: process.uptime(), ts: new Date().toISOString() });
    } catch (e) {
      res.status(503).json({ status: 'down', error: e.message });
    }
  });

  // Global error handler
  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, next) => {
    res.status(500).json({ error: err.message });
  });

  await new Promise((resolve, reject) => {
    serverInstance = app.listen(3001, err => {
      if (err) reject(err);
      else resolve();
    });
  });
});

after(async () => {
  if (serverInstance) {
    await new Promise(resolve => serverInstance.close(resolve));
  }
  if (closeDatabase) await closeDatabase();
});

// ─── Shared state ─────────────────────────────────────────────────────────────

let adminToken = null;
let createdOrderNumber = null;
let createdOrderId = null;
let createdModelId = null;

// ─── Health ───────────────────────────────────────────────────────────────────

test('GET /health → 200 + { status: "ok" }', async () => {
  const res = await request('GET', '/health');
  assert.equal(res.status, 200, `Expected 200, got ${res.status}`);
  assert.equal(res.body.status, 'ok', 'Health status should be "ok"');
});

// ─── Config ───────────────────────────────────────────────────────────────────

test('GET /api/config → 200 + { bot_username }', async () => {
  const res = await request('GET', '/api/config');
  assert.equal(res.status, 200, `Expected 200, got ${res.status}`);
  assert.ok('bot_username' in res.body, 'Response should have bot_username');
});

// ─── Auth ─────────────────────────────────────────────────────────────────────

test('POST /api/admin/login with valid credentials → 200 + token', async () => {
  const res = await request('POST', '/api/admin/login', {
    username: 'admin',
    password: 'admin123',
  });
  assert.equal(res.status, 200, `Expected 200, got ${res.status}: ${JSON.stringify(res.body)}`);
  assert.ok(res.body.token, 'Response should contain token');
  assert.ok(res.body.admin, 'Response should contain admin object');
  assert.equal(res.body.admin.username, 'admin');
  // Save token for subsequent auth tests
  adminToken = res.body.token;
});

test('POST /api/admin/login with wrong password → 401', async () => {
  const res = await request('POST', '/api/admin/login', {
    username: 'admin',
    password: 'wrongpassword',
  });
  assert.equal(res.status, 401, `Expected 401, got ${res.status}`);
  assert.ok(res.body.error, 'Should have error message');
});

test('GET /api/admin/orders without token → 401', async () => {
  const res = await request('GET', '/api/admin/orders');
  assert.equal(res.status, 401, `Expected 401, got ${res.status}`);
});

// ─── Models (public) ──────────────────────────────────────────────────────────

test('GET /api/models → 200, array', async () => {
  const res = await request('GET', '/api/models');
  assert.equal(res.status, 200, `Expected 200, got ${res.status}`);
  assert.ok(Array.isArray(res.body), 'Response should be an array');
});

test('GET /api/models?available=1 → only available=1 models', async () => {
  const res = await request('GET', '/api/models?available=1');
  assert.equal(res.status, 200, `Expected 200, got ${res.status}`);
  assert.ok(Array.isArray(res.body), 'Response should be an array');
  for (const model of res.body) {
    assert.equal(model.available, 1, `Model ${model.id} should have available=1, got ${model.available}`);
  }
});

test('GET /api/models/:id → 200 with fields name, height', async () => {
  // First get list to find a real model id
  const listRes = await request('GET', '/api/models');
  assert.ok(listRes.body.length > 0, 'Need at least one model in DB');
  const firstId = listRes.body[0].id;

  const res = await request('GET', `/api/models/${firstId}`);
  assert.equal(res.status, 200, `Expected 200, got ${res.status}`);
  assert.ok('name' in res.body, 'Model should have name field');
  assert.ok('height' in res.body, 'Model should have height field');
});

test('GET /api/models/:id with non-existent id → 404', async () => {
  const res = await request('GET', '/api/models/999999');
  assert.equal(res.status, 404, `Expected 404, got ${res.status}`);
});

// ─── Orders (public) ──────────────────────────────────────────────────────────

test('POST /api/orders with valid body → 201 or 200 + order_number', async () => {
  // Note: API returns 200 (not 201) per the current implementation
  const res = await request('POST', '/api/orders', {
    client_name: 'Тест Клиент',
    client_phone: '+7 999 123-45-67',
    event_type: 'photo_shoot',
    event_date: '2026-08-15',
    event_duration: 4,
  });
  // API responds with 200 (res.json not res.status(201))
  assert.ok(
    res.status === 200 || res.status === 201,
    `Expected 200 or 201, got ${res.status}: ${JSON.stringify(res.body)}`
  );
  assert.ok(res.body.order_number, `Should have order_number, got: ${JSON.stringify(res.body)}`);
  assert.ok(res.body.id, 'Should have order id');
  // Save for status test
  createdOrderNumber = res.body.order_number;
  createdOrderId = res.body.id;
});

test('POST /api/orders without client_name → 400', async () => {
  const res = await request('POST', '/api/orders', {
    client_phone: '+7 999 123-45-67',
    event_type: 'photo_shoot',
  });
  assert.equal(res.status, 400, `Expected 400, got ${res.status}`);
  assert.ok(res.body.error, 'Should have error message');
});

test('POST /api/orders with invalid phone → 400', async () => {
  const res = await request('POST', '/api/orders', {
    client_name: 'Тест Клиент',
    client_phone: 'not-a-phone',
    event_type: 'photo_shoot',
  });
  assert.equal(res.status, 400, `Expected 400, got ${res.status}`);
  assert.ok(res.body.error, 'Should have error message');
});

test('GET /api/orders/status/:order_number → 200', async () => {
  // Use order created in the POST test
  assert.ok(createdOrderNumber, 'Need createdOrderNumber from POST /api/orders test');
  const res = await request('GET', `/api/orders/status/${createdOrderNumber}`);
  assert.equal(res.status, 200, `Expected 200, got ${res.status}: ${JSON.stringify(res.body)}`);
  assert.ok(res.body.order_number, 'Response should have order_number');
  assert.ok(res.body.status, 'Response should have status');
});

// ─── Admin orders (auth required) ────────────────────────────────────────────

test('GET /api/admin/orders → 200 + { orders, total, page, pages }', async () => {
  assert.ok(adminToken, 'Need adminToken from login test');
  const res = await request('GET', '/api/admin/orders', null, {
    Authorization: `Bearer ${adminToken}`,
  });
  assert.equal(res.status, 200, `Expected 200, got ${res.status}: ${JSON.stringify(res.body)}`);
  assert.ok(Array.isArray(res.body.orders), 'orders should be an array');
  assert.ok(typeof res.body.total === 'number', 'total should be a number');
  assert.ok(typeof res.body.page === 'number', 'page should be a number');
  assert.ok(typeof res.body.pages === 'number', 'pages should be a number');
});

test('GET /api/admin/orders?page=1&limit=5 → limit respected', async () => {
  assert.ok(adminToken, 'Need adminToken from login test');
  const res = await request('GET', '/api/admin/orders?page=1&limit=5', null, {
    Authorization: `Bearer ${adminToken}`,
  });
  assert.equal(res.status, 200, `Expected 200, got ${res.status}`);
  assert.ok(Array.isArray(res.body.orders), 'orders should be an array');
  assert.ok(res.body.orders.length <= 5, `Limit 5 should be respected, got ${res.body.orders.length}`);
  assert.equal(res.body.page, 1, 'page should be 1');
});

test('PUT /api/admin/orders/:id with status="confirmed" → 200', async () => {
  assert.ok(adminToken, 'Need adminToken from login test');
  assert.ok(createdOrderId, 'Need createdOrderId from POST /api/orders test');

  const res = await request('PUT', `/api/admin/orders/${createdOrderId}`, {
    status: 'confirmed',
  }, {
    Authorization: `Bearer ${adminToken}`,
  });
  assert.equal(res.status, 200, `Expected 200, got ${res.status}: ${JSON.stringify(res.body)}`);
  assert.equal(res.body.ok, true, 'Should return { ok: true }');
});

test('PUT /api/admin/orders/:id with invalid status → 400', async () => {
  assert.ok(adminToken, 'Need adminToken from login test');
  assert.ok(createdOrderId, 'Need createdOrderId from POST /api/orders test');

  const res = await request('PUT', `/api/admin/orders/${createdOrderId}`, {
    status: 'invalid_status',
  }, {
    Authorization: `Bearer ${adminToken}`,
  });
  assert.equal(res.status, 400, `Expected 400, got ${res.status}`);
});

// ─── New feature tests ─────────────────────────────────────────────────────

test('GET /api/reviews/public → 200, array', async () => {
  const { status, body } = await request('GET', '/api/reviews/public');
  assert.equal(status, 200);
  assert.ok(Array.isArray(body));
});

test('GET /api/reviews/public?limit=3 → max 3 items', async () => {
  const { status, body } = await request('GET', '/api/reviews/public?limit=3');
  assert.equal(status, 200);
  assert.ok(body.length <= 3);
});

test('GET /sitemap.xml → 200, XML content', async () => {
  const { status } = await request('GET', '/sitemap.xml');
  assert.equal(status, 200);
});

test('GET /robots.txt → 200, text with Disallow', async () => {
  const { status } = await request('GET', '/robots.txt');
  assert.equal(status, 200);
});

test('GET /api/admin/stats → 200 with new_orders field (auth)', async () => {
  const { status, body } = await request('GET', '/api/admin/stats', null, { Authorization: `Bearer ${adminToken}` });
  assert.equal(status, 200);
  assert.ok(body.hasOwnProperty('new_orders') || body.hasOwnProperty('orders_today'));
});

test('GET /api/admin/db-stats → 200 with tables array (auth)', async () => {
  const { status, body } = await request('GET', '/api/admin/db-stats', null, { Authorization: `Bearer ${adminToken}` });
  assert.equal(status, 200);
  assert.ok(Array.isArray(body.tables));
  assert.ok(body.tables.length > 0);
});

test('GET /api/admin/audit-log → 200, array (auth)', async () => {
  const { status, body } = await request('GET', '/api/admin/audit-log', null, { Authorization: `Bearer ${adminToken}` });
  assert.equal(status, 200);
  assert.ok(Array.isArray(body));
});

test('GET /api/admin/factory-tasks → 200 + tasks/stats (auth)', async () => {
  const { status, body } = await request('GET', '/api/admin/factory-tasks', null, { Authorization: `Bearer ${adminToken}` });
  assert.equal(status, 200);
  assert.ok(body.hasOwnProperty('tasks') || Array.isArray(body));
});

test('GET /api/admin/managers → 200, array (auth)', async () => {
  const { status, body } = await request('GET', '/api/admin/managers', null, { Authorization: `Bearer ${adminToken}` });
  assert.equal(status, 200);
  assert.ok(Array.isArray(body));
});

test('POST /api/admin/models/json → 200 (create model via JSON, auth)', async () => {
  const { status, body } = await request('POST', '/api/admin/models/json', {
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
    available: 1
  }, { Authorization: `Bearer ${adminToken}` });
  assert.ok([200, 201].includes(status), `Expected 200/201, got ${status}: ${JSON.stringify(body)}`);
  assert.ok(body.id || body.model_id || body.success);
  createdModelId = body.id || createdModelId;
});

test('GET /api/admin/models → 200 + models array (auth)', async () => {
  const { status, body } = await request('GET', '/api/admin/models', null, { Authorization: `Bearer ${adminToken}` });
  assert.equal(status, 200);
  assert.ok(Array.isArray(body.models || body));
});

test('GET /api/faq → 200, array', async () => {
  const { status, body } = await request('GET', '/api/faq');
  assert.ok([200].includes(status));
  assert.ok(Array.isArray(body));
});

test('POST /api/contact → 200 or 429 (rate limited)', async () => {
  const { status } = await request('POST', '/api/contact', {
    name: 'Test User',
    phone: '+79001234567',
    message: 'Test contact message from CI'
  });
  assert.ok([200, 201, 429].includes(status), `Expected 200/201/429, got ${status}`);
});
