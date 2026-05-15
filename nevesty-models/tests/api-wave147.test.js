'use strict';
// Wave 147: Security тесты — cabinet max-length, phone validation, repeat order edge cases

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave147-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const jwt = require('jsonwebtoken');

let app, adminToken, clientToken;
let orderId;
const TEST_PHONE = '79111222333';
const TEST_PHONE_10 = '9111222333';

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
  app = a;

  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;

  // Create an order so we can test cabinet login
  const csrf = await getCsrf();
  const or = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
    client_name: 'Безопасный Клиент',
    client_phone: TEST_PHONE,
    client_email: 'safe@example.com',
    event_type: 'fashion_show',
    budget: '50000',
  });
  orderId = or.body.id || or.body.order?.id;

  // Get client token
  const cl = await request(app).post('/api/cabinet/login').send({ phone: TEST_PHONE });
  clientToken = cl.body.token;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── 1. Phone format validation in requireClientAuth ─────────────────────────

describe('requireClientAuth — phone format validation', () => {
  it('token с некорректным phone (не 10 цифр) → 400', async () => {
    const badToken = jwt.sign({ type: 'client', phone: 'notaphone' }, process.env.JWT_SECRET, { expiresIn: '1h' });
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${badToken}`);
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/Invalid token payload/i);
  });

  it('token с null phone → 400', async () => {
    const badToken = jwt.sign({ type: 'client', phone: null }, process.env.JWT_SECRET, { expiresIn: '1h' });
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${badToken}`);
    expect(res.status).toBe(400);
  });

  it('token с 11-значным phone → 400', async () => {
    const badToken = jwt.sign({ type: 'client', phone: '79111222333' }, process.env.JWT_SECRET, { expiresIn: '1h' });
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${badToken}`);
    expect(res.status).toBe(400);
  });

  it('валидный 10-значный phone → 200', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
  });
});

// ─── 2. PATCH /cabinet/profile — max-length validation ───────────────────────

describe('PATCH /cabinet/profile — max-length validation', () => {
  it('name длиннее 200 символов → 400', async () => {
    const longName = 'А'.repeat(201);
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: longName });
    expect(res.status).toBe(400);
  });

  it('email длиннее 254 символов → 400', async () => {
    const longEmail = 'a'.repeat(249) + '@x.com'; // 255 chars
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ email: longEmail });
    expect(res.status).toBe(400);
  });

  it('name ровно 200 символов → 200', async () => {
    const maxName = 'К'.repeat(200);
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: maxName });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('email ровно 254 символа → 200', async () => {
    const maxEmail = 'a'.repeat(248) + '@x.com';
    expect(maxEmail.length).toBe(254);
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ email: maxEmail });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('name из 1 символа → 400 (ниже минимума)', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'A' });
    expect(res.status).toBe(400);
  });

  it('нельзя изменить client_phone через PATCH (массовое присвоение)', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ client_phone: '79999999999', name: 'Валидное Имя' });
    expect(res.status).toBe(200); // только name изменится
    // Проверяем что телефон не изменился в профиле
    const profile = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(profile.body.profile.phone).toBe(TEST_PHONE_10);
  });
});

// ─── 3. POST /cabinet/orders/:id/repeat — дополнительные edge cases ──────────

describe('POST /cabinet/orders/:id/repeat — security', () => {
  it('невалидный ID (строка) → 400', async () => {
    const res = await request(app).post('/api/cabinet/orders/abc/repeat').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(400);
  });

  it('повторить активную заявку (status=new) → 409', async () => {
    const res = await request(app)
      .post(`/api/cabinet/orders/${orderId}/repeat`)
      .set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(409);
    expect(res.body.error).toBeDefined();
  });

  it('без auth → 401', async () => {
    const res = await request(app).post(`/api/cabinet/orders/${orderId}/repeat`);
    expect(res.status).toBe(401);
  });
});

// ─── 4. Admin JWT на client endpoints → 403 ──────────────────────────────────

describe('Admin JWT на client-only endpoints → 403', () => {
  it('GET /api/cabinet/profile с admin token → 403', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });

  it('GET /api/cabinet/orders с admin token → 403', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });

  it('POST /api/cabinet/orders/:id/repeat с admin token → 403', async () => {
    const res = await request(app)
      .post(`/api/cabinet/orders/${orderId}/repeat`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });

  it('PATCH /api/cabinet/profile с admin token → 403', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ name: 'Хакер' });
    expect(res.status).toBe(403);
  });
});
