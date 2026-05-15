'use strict';
// Wave 148: Cabinet integration tests — sitemap, health extended metrics, cabinet full flow

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave148-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

const serverContent = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');
const sitemapPath = path.join(__dirname, '../public/sitemap.xml');

let app, adminToken, clientToken;
const TEST_PHONE = '79151234567';
const TEST_PHONE_10 = '9151234567';

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
  await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
    client_name: 'Wave148 Клиент',
    client_phone: TEST_PHONE,
    client_email: 'wave148@example.com',
    event_type: 'photo_shoot',
    budget: '30000',
  });

  const cl = await request(app).post('/api/cabinet/login').send({ phone: TEST_PHONE });
  clientToken = cl.body.token;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── 1. Sitemap.xml includes new pages ───────────────────────────────────────

describe('Wave 148: Sitemap includes all key pages', () => {
  let sitemapContent;

  beforeAll(() => {
    if (fs.existsSync(sitemapPath)) {
      sitemapContent = fs.readFileSync(sitemapPath, 'utf8');
    }
  });

  it('sitemap.xml exists', () => {
    expect(fs.existsSync(sitemapPath)).toBe(true);
  });

  it('sitemap contains catalog.html', () => {
    if (!sitemapContent) return;
    expect(sitemapContent).toMatch(/catalog\.html/);
  });

  it('server.js dynamic sitemap includes cabinet.html', () => {
    const serverContent = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');
    expect(serverContent).toMatch(/cabinet\.html/);
  });

  it('sitemap contains reviews.html', () => {
    if (!sitemapContent) return;
    expect(sitemapContent).toMatch(/reviews\.html/);
  });

  it('sitemap contains faq.html', () => {
    if (!sitemapContent) return;
    expect(sitemapContent).toMatch(/faq\.html/);
  });
});

// ─── 2. Health endpoint source-code checks ───────────────────────────────────

describe('Wave 148: Health endpoint extended metrics (source checks)', () => {
  it('server.js defines buildHealthResponse function', () => {
    expect(serverContent).toMatch(/buildHealthResponse/);
  });

  it('server.js has /api/health route', () => {
    expect(serverContent).toMatch(/\/api\/health/);
  });

  it('health response includes rss_mb', () => {
    expect(serverContent).toMatch(/rss_mb/);
  });

  it('health response includes heap_used_mb', () => {
    expect(serverContent).toMatch(/heap_used_mb/);
  });

  it('health response includes uptime_seconds', () => {
    expect(serverContent).toMatch(/uptime_seconds/);
  });

  it('health response includes cpu metrics', () => {
    expect(serverContent).toMatch(/cpu:/);
  });
});

// ─── 3. Cabinet login edge cases ─────────────────────────────────────────────

describe('Wave 148: Cabinet login edge cases', () => {
  it('POST /cabinet/login without phone → 400', async () => {
    const res = await request(app).post('/api/cabinet/login').send({});
    expect(res.status).toBe(400);
  });

  it('POST /cabinet/login with invalid phone format → 400', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: 'abc' });
    expect(res.status).toBe(400);
  });

  it('POST /cabinet/login with unknown phone → 404', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '70000000001' });
    expect(res.status).toBe(404);
  });

  it('POST /cabinet/login with valid phone → token has type:client', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: TEST_PHONE });
    expect(res.status).toBe(200);
    expect(res.body.token).toBeDefined();
    const jwt = require('jsonwebtoken');
    const decoded = jwt.verify(res.body.token, process.env.JWT_SECRET);
    expect(decoded.type).toBe('client');
  });

  it('token phone is normalized to 10 digits', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: TEST_PHONE });
    expect(res.status).toBe(200);
    const jwt = require('jsonwebtoken');
    const decoded = jwt.verify(res.body.token, process.env.JWT_SECRET);
    expect(decoded.phone).toMatch(/^\d{10}$/);
    expect(decoded.phone).toBe(TEST_PHONE_10);
  });
});

// ─── 4. Cabinet profile — field validation ───────────────────────────────────

describe('Wave 148: Cabinet profile PATCH field validation', () => {
  it('name длиной 1 символ → 400', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'А' });
    expect(res.status).toBe(400);
  });

  it('name длиной 2 символа → 200', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'Аб' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('name 200 символов → 200', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'К'.repeat(200) });
    expect(res.status).toBe(200);
  });

  it('name 201 символ → 400', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'К'.repeat(201) });
    expect(res.status).toBe(400);
  });

  it('корректный email → 200', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ email: 'valid@example.com' });
    expect(res.status).toBe(200);
  });

  it('некорректный email → 400', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ email: 'notanemail' });
    expect(res.status).toBe(400);
  });

  it('нельзя изменить phone через PATCH body', async () => {
    await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ client_phone: '79999999999', name: 'Тест Имя' });
    const profile = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(profile.body.profile.phone).toBe(TEST_PHONE_10);
  });
});

// ─── 5. Admin JWT blocked on client endpoints ─────────────────────────────────

describe('Wave 148: Admin token rejected on client-only endpoints', () => {
  it('GET /cabinet/profile с admin token → 403', async () => {
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });

  it('PATCH /cabinet/profile с admin token → 403', async () => {
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ name: 'Тест' });
    expect(res.status).toBe(403);
  });

  it('GET /cabinet/orders с admin token → 403', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });
});

// ─── 6. Cabinet orders list ───────────────────────────────────────────────────

describe('Wave 148: Cabinet orders list', () => {
  it('GET /cabinet/orders → 200 с корректным token', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    const orders = res.body.orders || res.body;
    expect(Array.isArray(orders)).toBe(true);
  });

  it('каждый заказ содержит id, status, order_number', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    const orders = res.body.orders || res.body;
    if (Array.isArray(orders) && orders.length > 0) {
      const order = orders[0];
      expect(order).toHaveProperty('id');
      expect(order).toHaveProperty('status');
      expect(order).toHaveProperty('order_number');
    }
  });

  it('заказы содержат event_type_ru поле', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    const orders = res.body.orders || res.body;
    if (Array.isArray(orders) && orders.length > 0) {
      expect(orders[0]).toHaveProperty('event_type_ru');
    }
  });
});
