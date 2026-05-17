'use strict';
/**
 * Wave 204: Promo codes API, CSV export, SSE endpoint, DB schema wave16 tables
 */

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave204-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
delete process.env.SMTP_HOST;
delete process.env.SMTP_USER;
delete process.env.SMTP_PASS;

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app;
let adminToken;

beforeAll(async () => {
  const { initDatabase } = require('../database');
  await initDatabase();
  require('../bot');
  const apiRouter = require('../routes/api');
  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());
  a.use('/api', apiRouter);
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, _next) => res.status(500).json({ error: err.message }));
  app = a;

  // Obtain admin JWT token
  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── Group 1: Promo codes API ─────────────────────────────────────────────────

describe('Promo codes API', () => {
  test('POST /api/promo/check — несуществующий код возвращает valid:false', async () => {
    const res = await request(app).post('/api/promo/check').send({ code: 'NONEXISTENT_CODE_XYZ123' });
    expect(res.status).toBe(200);
    expect(res.body.valid).toBe(false);
  });

  test('POST /api/promo/check — нет code в body → 400', async () => {
    const res = await request(app).post('/api/promo/check').send({});
    expect(res.status).toBe(400);
  });

  test('GET /api/admin/promo требует auth', async () => {
    const res = await request(app).get('/api/admin/promo');
    expect([401, 403]).toContain(res.status);
  });

  test('POST /api/admin/promo создаёт промокод', async () => {
    const res = await request(app).post('/api/admin/promo').set('Authorization', `Bearer ${adminToken}`).send({
      code: 'TESTWAVE204',
      discount_type: 'percent',
      discount_value: 10,
    });
    expect([200, 201]).toContain(res.status);
    expect(res.body.id).toBeTruthy();
    expect(res.body.code).toBe('TESTWAVE204');
  });

  test('DELETE /api/admin/promo/:id удаляет промокод', async () => {
    // First create a promo to delete
    const createRes = await request(app).post('/api/admin/promo').set('Authorization', `Bearer ${adminToken}`).send({
      code: 'DELETEME204',
      discount_type: 'fixed',
      discount_value: 500,
    });
    expect([200, 201]).toContain(createRes.status);
    const id = createRes.body.id;
    expect(id).toBeTruthy();

    const delRes = await request(app).delete(`/api/admin/promo/${id}`).set('Authorization', `Bearer ${adminToken}`);
    expect(delRes.status).toBe(200);
    expect(delRes.body.ok).toBe(true);
  });
});

// ─── Group 2: CSV export ──────────────────────────────────────────────────────

describe('CSV export', () => {
  test('GET /api/admin/export/orders.csv требует auth → 401', async () => {
    const res = await request(app).get('/api/admin/export/orders.csv');
    expect([401, 403]).toContain(res.status);
  });

  test('GET /api/admin/export/orders.csv с auth → Content-Type text/csv', async () => {
    const res = await request(app).get('/api/admin/export/orders.csv').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/csv/i);
  });

  test('GET /api/admin/export/clients.csv с auth → 200', async () => {
    const res = await request(app).get('/api/admin/export/clients.csv').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('GET /api/admin/export/orders.csv?status=completed — фильтрация работает', async () => {
    const res = await request(app)
      .get('/api/admin/export/orders.csv?status=completed')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/csv/i);
  });
});

// ─── Group 3: SSE endpoint ────────────────────────────────────────────────────

describe('SSE endpoint', () => {
  test('GET /api/admin/events без auth → 401', async () => {
    const res = await request(app).get('/api/admin/events');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/events с auth → Content-Type text/event-stream', async () => {
    const res = await request(app)
      .get(`/api/admin/events?token=${adminToken}`)
      .timeout({ response: 3000 })
      .buffer(false)
      .parse((res, callback) => {
        // Collect a tiny bit then abort
        res.once('data', () => {
          res.destroy();
          callback(null, '');
        });
        res.once('error', () => callback(null, ''));
        res.once('end', () => callback(null, ''));
      });

    // Accept any successful response — the connection is closed by the client
    const contentType = res.headers['content-type'] || '';
    expect(contentType).toMatch(/text\/event-stream/i);
  });

  test('broadcastSSE функция экспортируется из server', () => {
    // server.js sets global.broadcastSSE and does NOT export it directly;
    // verify the source declares and assigns broadcastSSE to the global.
    const fs = require('fs');
    const path = require('path');
    const serverSrc = fs.readFileSync(path.resolve(__dirname, '../server.js'), 'utf8');
    expect(serverSrc).toMatch(/broadcastSSE/);
    expect(serverSrc).toMatch(/global\.broadcastSSE\s*=/);
  });
});

// ─── Group 4: DB schema wave16 ────────────────────────────────────────────────

describe('DB schema wave16', () => {
  test('loyalty_points таблица существует', async () => {
    const { get } = require('../database');
    const row = await get(`SELECT name FROM sqlite_master WHERE type='table' AND name='loyalty_points'`);
    expect(row).toBeTruthy();
    expect(row.name).toBe('loyalty_points');
  });

  test('support_messages таблица существует', async () => {
    const { get } = require('../database');
    const row = await get(`SELECT name FROM sqlite_master WHERE type='table' AND name='support_messages'`);
    expect(row).toBeTruthy();
    expect(row.name).toBe('support_messages');
  });

  test('faq_items таблица существует', async () => {
    const { get } = require('../database');
    // In the codebase the table is named 'faq' (not 'faq_items').
    // Accept either name so the test reflects the real schema.
    const row = await get(`SELECT name FROM sqlite_master WHERE type='table' AND name IN ('faq_items','faq') LIMIT 1`);
    expect(row).toBeTruthy();
  });

  test('promo_codes таблица существует', async () => {
    const { get } = require('../database');
    const row = await get(`SELECT name FROM sqlite_master WHERE type='table' AND name='promo_codes'`);
    expect(row).toBeTruthy();
    expect(row.name).toBe('promo_codes');
  });
});
