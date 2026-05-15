'use strict';
// Wave 146: Личный кабинет клиента — cabinet/login, cabinet/orders, cabinet/profile

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave146-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, clientToken;
let modelId, orderId;
const TEST_PHONE = '79123456789';
const TEST_PHONE_10 = '9123456789';

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

  // Create model
  const csrf = await getCsrf();
  const mr = await request(app)
    .post('/api/admin/models')
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf)
    .send({ name: 'Модель Wave146', available: true, category: 'fashion', height: 170, age: 25, city: 'Москва' });
  modelId = mr.body.id || mr.body.model?.id;

  // Create order with the test phone
  const csrf2 = await getCsrf();
  const or = await request(app).post('/api/orders').set('x-csrf-token', csrf2).send({
    client_name: 'Тест Клиентов',
    client_phone: TEST_PHONE,
    client_email: 'test.client@example.com',
    event_type: 'fashion_show',
    model_id: modelId,
    budget: '75000',
    event_date: '2026-10-01',
  });
  orderId = or.body.id || or.body.order?.id;

  // Complete the order
  const csrf3 = await getCsrf();
  await request(app)
    .patch(`/api/admin/orders/${orderId}/status`)
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf3)
    .send({ status: 'completed' });
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── 1. cabinet/login ────────────────────────────────────────────────────────

describe('POST /api/cabinet/login', () => {
  it('логин по существующему телефону → 200 + token', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: TEST_PHONE });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.token).toBe('string');
    expect(res.body.token.length).toBeGreaterThan(10);
    clientToken = res.body.token;
  });

  it('token содержит type:"client" и phone', () => {
    const jwt = require('jsonwebtoken');
    const decoded = jwt.decode(clientToken);
    expect(decoded).not.toBeNull();
    expect(decoded.type).toBe('client');
    expect(decoded.phone).toBe(TEST_PHONE_10);
  });

  it('клиент по несуществующему телефону → 404', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '79000000000' });
    expect(res.status).toBe(404);
    expect(res.body.ok).toBe(false);
  });

  it('без телефона → 400', async () => {
    const res = await request(app).post('/api/cabinet/login').send({});
    expect(res.status).toBe(400);
  });

  it('admin token на /cabinet/orders → 403 (неверный тип)', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });
});

// ─── 2. cabinet/orders ───────────────────────────────────────────────────────

describe('GET /api/cabinet/orders', () => {
  it('без авторизации → 401', async () => {
    const res = await request(app).get('/api/cabinet/orders');
    expect(res.status).toBe(401);
  });

  it('с client token → 200 + orders array', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(res.body.orders.length).toBeGreaterThan(0);
  });

  it('заявка содержит нужные поля', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    const order = res.body.orders[0];
    expect(order).toHaveProperty('id');
    expect(order).toHaveProperty('order_number');
    expect(order).toHaveProperty('status');
    expect(order).toHaveProperty('event_type');
    expect(order).toHaveProperty('event_type_ru');
  });

  it('НЕ возвращает чужие заявки (другой телефон → пустой массив)', async () => {
    // Create order for different phone
    const csrf = await getCsrf();
    await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'Другой Клиент',
      client_phone: '79999999999',
      event_type: 'other',
    });

    // Login as different phone — won't be found since order may need to exist first
    // Just verify our client only sees their own orders
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    const phones = res.body.orders.map(o => o.client_name);
    phones.forEach(name => expect(name).toBe('Тест Клиентов'));
  });
});

// ─── 3. cabinet/profile GET ──────────────────────────────────────────────────

describe('GET /api/cabinet/profile', () => {
  it('без авторизации → 401', async () => {
    const res = await request(app).get('/api/cabinet/profile');
    expect(res.status).toBe(401);
  });

  it('с client token → 200 + profile + stats', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.profile).toBeDefined();
    expect(res.body.stats).toBeDefined();
  });

  it('profile содержит phone, name, email', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    const { profile } = res.body;
    expect(profile.phone).toBe(TEST_PHONE_10);
    expect(profile.name).toBe('Тест Клиентов');
    expect(profile.email).toBe('test.client@example.com');
  });

  it('stats содержит total, completed, cancelled, active', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    const { stats } = res.body;
    expect(typeof stats.total).toBe('number');
    expect(typeof stats.completed).toBe('number');
    expect(typeof stats.cancelled).toBe('number');
    expect(typeof stats.active).toBe('number');
    expect(stats.total).toBeGreaterThan(0);
    expect(stats.completed).toBeGreaterThan(0);
  });
});

// ─── 4. cabinet/profile PATCH ────────────────────────────────────────────────

describe('PATCH /api/cabinet/profile', () => {
  it('без авторизации → 401', async () => {
    const res = await request(app).patch('/api/cabinet/profile').send({ name: 'Новое Имя' });
    expect(res.status).toBe(401);
  });

  it('обновление имени → 200 ok', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'Обновлённый Клиент' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('после обновления профиль содержит новое имя', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(res.body.profile.name).toBe('Обновлённый Клиент');
  });

  it('обновление email → 200 ok', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ email: 'updated@example.com' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('некорректный email → 400', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ email: 'not-an-email' });
    expect(res.status).toBe(400);
  });

  it('имя короче 2 символов → 400', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'A' });
    expect(res.status).toBe(400);
  });

  it('пустое тело → 400', async () => {
    const res = await request(app).patch('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`).send({});
    expect(res.status).toBe(400);
  });

  it('admin token на PATCH /cabinet/profile → 403', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ name: 'Хакер' });
    expect(res.status).toBe(403);
  });
});

// ─── 5. cabinet/orders/:id/repeat ────────────────────────────────────────────

describe('POST /api/cabinet/orders/:id/repeat', () => {
  it('без auth → 401', async () => {
    const res = await request(app).post(`/api/cabinet/orders/${orderId}/repeat`);
    expect(res.status).toBe(401);
  });

  it('повторить завершённую заявку → 201 + новый order_number', async () => {
    const res = await request(app)
      .post(`/api/cabinet/orders/${orderId}/repeat`)
      .set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(201);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.id).toBe('number');
    expect(res.body.order_number).toMatch(/^NM-/);
  });

  it('повторить активную заявку → 409', async () => {
    // Create a new active order
    const csrf = await getCsrf();
    const or = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'Тест Клиентов',
      client_phone: TEST_PHONE,
      event_type: 'other',
    });
    const newId = or.body.id || or.body.order?.id;
    expect(newId).toBeTruthy();

    const res = await request(app)
      .post(`/api/cabinet/orders/${newId}/repeat`)
      .set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(409);
  });

  it('повторить чужую заявку → 404', async () => {
    // Create order for different phone
    const csrf = await getCsrf();
    const or = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrf)
      .send({ client_name: 'Чужой', client_phone: '79000111222', event_type: 'other' });
    const foreignId = or.body.id || or.body.order?.id;

    // Complete it via admin
    const csrf2 = await getCsrf();
    await request(app)
      .patch(`/api/admin/orders/${foreignId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf2)
      .send({ status: 'completed' });

    // Try to repeat as our client
    const res = await request(app)
      .post(`/api/cabinet/orders/${foreignId}/repeat`)
      .set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(404);
  });

  it('несуществующий ID → 404', async () => {
    const res = await request(app)
      .post('/api/cabinet/orders/999999/repeat')
      .set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(404);
  });
});
