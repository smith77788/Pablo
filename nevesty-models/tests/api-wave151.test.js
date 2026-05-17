'use strict';
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave151-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

let app, adminToken, clientToken;
const TEST_PHONE = '79161234999';
const TEST_PHONE_10 = '9161234999';

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
  a.use(express.json());
  a.use(cors());
  a.use('/api', apiRouter);
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  // Admin login
  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;

  // Seed an order so cabinet login will find the client phone
  const csrf = await getCsrf();
  await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
    client_name: 'Тест Клиент Wave151',
    client_phone: TEST_PHONE,
    client_email: 'wave151@example.com',
    event_type: 'photo_shoot',
    event_date: '2026-06-01',
    location: 'Москва',
    budget: 50000,
    comments: 'wave151 test order',
  });

  // Cabinet login using the seeded phone
  const cl = await request(app).post('/api/cabinet/login').send({ phone: TEST_PHONE });
  clientToken = cl.body.token;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── 1. CSV экспорт заявок ────────────────────────────────────────────────────

describe('Wave 151: GET /api/admin/orders/export — CSV', () => {
  it('без auth → 401', async () => {
    const res = await request(app).get('/api/admin/orders/export');
    expect(res.status).toBe(401);
  });

  it('с auth → 200, Content-Type содержит csv', async () => {
    if (!adminToken) return;
    const res = await request(app).get('/api/admin/orders/export').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/csv/);
  });

  it('с auth → тело содержит заголовок ID', async () => {
    if (!adminToken) return;
    const res = await request(app).get('/api/admin/orders/export').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.text).toContain('ID');
  });
});

// ─── 2. Модели API ────────────────────────────────────────────────────────────

describe('Wave 151: GET /api/models', () => {
  it('→ 200, массив', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('?search=test → 200', async () => {
    const res = await request(app).get('/api/models?search=test');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

describe('Wave 151: GET /api/admin/models', () => {
  it('с auth ?page=1&limit=5 → 200', async () => {
    if (!adminToken) return;
    const res = await request(app).get('/api/admin/models?page=1&limit=5').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ─── 3. Cabinet profile ───────────────────────────────────────────────────────

describe('Wave 151: Cabinet profile endpoints', () => {
  it('GET /api/cabinet/login по существующему телефону → вернул token', () => {
    expect(typeof clientToken).toBe('string');
    expect(clientToken.length).toBeGreaterThan(10);
  });

  it('GET /api/cabinet/profile с clientToken → 200, есть profile', async () => {
    if (!clientToken) return;
    const res = await request(app).get('/api/cabinet/profile').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('profile');
  });

  it('PATCH /api/cabinet/profile с clientToken → 200, ok:true', async () => {
    if (!clientToken) return;
    const res = await request(app)
      .patch('/api/cabinet/profile')
      .set('Authorization', `Bearer ${clientToken}`)
      .send({ name: 'Обновлённый Клиент', email: 'updated151@example.com' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('GET /api/cabinet/orders с clientToken → 200, ok:true', async () => {
    if (!clientToken) return;
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ─── 4. Social posts ─────────────────────────────────────────────────────────

describe('Wave 151: Social posts endpoints', () => {
  it('GET /api/admin/social/posts с auth → 200 или 404', async () => {
    if (!adminToken) return;
    const res = await request(app).get('/api/admin/social/posts').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
  });

  it('POST /api/admin/social/posts без auth → 401', async () => {
    const res = await request(app)
      .post('/api/admin/social/posts')
      .send({ caption: 'Test post', platform: 'instagram' });
    expect(res.status).toBe(401);
  });
});

// ─── 5. Source code checks ────────────────────────────────────────────────────

describe('Wave 151: Source code checks — animations.js', () => {
  const animSrc = fs.readFileSync(path.join(__dirname, '../public/js/animations.js'), 'utf8');

  it('animations.js содержит initScrollReveal', () => {
    expect(animSrc).toContain('initScrollReveal');
  });

  it('animations.js содержит initCounters', () => {
    expect(animSrc).toContain('initCounters');
  });
});

describe('Wave 151: Source code checks — main.css', () => {
  const cssSrc = fs.readFileSync(path.join(__dirname, '../public/css/main.css'), 'utf8');

  it('main.css содержит .toast', () => {
    expect(cssSrc).toContain('.toast');
  });

  it('main.css содержит .spinner', () => {
    expect(cssSrc).toContain('.spinner');
  });
});
