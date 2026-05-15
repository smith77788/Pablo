'use strict';
// Wave 149: Security regression tests — WebSocket auth, JWT algo, repeat rate-limit

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave149-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

const serverContent = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');
const cabinetContent = fs.readFileSync(path.join(__dirname, '../public/cabinet.html'), 'utf8');

let app, adminToken, clientToken;
const TEST_PHONE = '79161234567';
const TEST_PHONE_10 = '9161234567';
let orderId;

async function getCsrf() {
  const r = await request(app).get('/api/csrf-token');
  return r.body.token;
}

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
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;

  const csrf = await getCsrf();
  const or = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
    client_name: 'Wave149 Клиент',
    client_phone: TEST_PHONE,
    client_email: 'wave149@example.com',
    event_type: 'photo_shoot',
    budget: '25000',
  });
  orderId = or.body.id || or.body.order?.id;

  const cl = await request(app).post('/api/cabinet/login').send({ phone: TEST_PHONE });
  clientToken = cl.body.token;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── 1. JWT algorithm pinning source checks ───────────────────────────────────

describe('Wave 149: JWT algorithm pinning', () => {
  it('routes/api.js uses algorithms:["HS256"] in requireClientAuth jwt.verify', () => {
    const apiContent = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    expect(apiContent).toMatch(/algorithms.*HS256|HS256.*algorithms/);
  });

  it('routes/api.js uses algorithm:"HS256" in cabinet/login jwt.sign', () => {
    const apiContent = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    expect(apiContent).toMatch(/algorithm.*HS256.*expiresIn|expiresIn.*HS256/s);
  });

  it('client token does NOT contain name field in payload', async () => {
    const jwt = require('jsonwebtoken');
    const decoded = jwt.verify(clientToken, process.env.JWT_SECRET);
    expect(decoded.name).toBeUndefined();
  });

  it('client token contains type:client, phone (10 digits)', async () => {
    const jwt = require('jsonwebtoken');
    const decoded = jwt.verify(clientToken, process.env.JWT_SECRET);
    expect(decoded.type).toBe('client');
    expect(decoded.phone).toMatch(/^\d{10}$/);
  });
});

// ─── 2. WebSocket phone auth — source code checks ─────────────────────────────

describe('Wave 149: WebSocket phone auth (source code)', () => {
  it('server.js requires JWT token for phone subscription', () => {
    expect(serverContent).toMatch(/msg\.token|token.*jwt.*verify|jwt.*verify.*token/s);
  });

  it('server.js validates token type === client for phone subscription', () => {
    expect(serverContent).toMatch(/decoded\.type.*client|type.*client.*wsSubscribePhone/s);
  });

  it('server.js returns Unauthorized error for invalid token', () => {
    expect(serverContent).toMatch(/Unauthorized/);
  });

  it('cabinet.html sends token in WebSocket subscribe message', () => {
    expect(cabinetContent).toMatch(/nm_client_token.*subscribe|subscribe.*nm_client_token|token.*subscribe.*phone/s);
  });

  it('cabinet.html includes SAFE_STATUSES whitelist for WebSocket updates', () => {
    expect(cabinetContent).toMatch(/SAFE_STATUSES/);
  });
});

// ─── 3. repeat endpoint with bookingLimiter ────────────────────────────────────

describe('Wave 149: /cabinet/orders/:id/repeat with bookingLimiter', () => {
  it('routes/api.js includes bookingLimiter in repeat endpoint', () => {
    const apiContent = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    expect(apiContent).toMatch(
      /cabinet\/orders\/:id\/repeat.*requireClientAuth.*bookingLimiter|bookingLimiter.*requireClientAuth.*repeat/s
    );
  });

  it('повторить активную (new) заявку → 409', async () => {
    if (!orderId) return;
    const res = await request(app)
      .post(`/api/cabinet/orders/${orderId}/repeat`)
      .set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(409);
  });

  it('без auth → 401', async () => {
    if (!orderId) return;
    const res = await request(app).post(`/api/cabinet/orders/${orderId}/repeat`);
    expect(res.status).toBe(401);
  });

  it('с admin token → 403', async () => {
    if (!orderId) return;
    const res = await request(app)
      .post(`/api/cabinet/orders/${orderId}/repeat`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });

  it('невалидный ID → 400', async () => {
    const res = await request(app)
      .post('/api/cabinet/orders/not-a-number/repeat')
      .set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(400);
  });
});

// ─── 4. PATCH /cabinet/profile — client_prefs sync ───────────────────────────

describe('Wave 149: PATCH /cabinet/profile syncs client_prefs', () => {
  it('успешный PATCH → 200 ok:true', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'Тест Wave149', email: 'w149@test.com' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('профиль отражает новые данные после PATCH', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    expect(res.body.profile.name).toBe('Тест Wave149');
    expect(res.body.profile.email).toBe('w149@test.com');
  });

  it('телефон не изменяется через PATCH', async () => {
    await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ client_phone: '70000000000', phone: '70000000000', name: 'Тест' });
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(res.body.profile.phone).toBe(TEST_PHONE_10);
  });
});

// ─── 5. cabinet/orders response structure ─────────────────────────────────────

describe('Wave 149: /cabinet/orders response structure', () => {
  it('возвращает ok:true и массив orders', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.orders)).toBe(true);
  });

  it('routes/api.js НЕ использует GROUP BY o.id в cabinet/orders', () => {
    const apiContent = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    const idx = apiContent.indexOf("/cabinet/orders'");
    const nearby = apiContent.slice(idx, idx + 1000);
    expect(nearby).not.toMatch(/GROUP BY o\.id/);
  });
});
