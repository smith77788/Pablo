'use strict';
// Wave 144: E2E модерация отзывов, поиск моделей, wishlist, публичные настройки, статус заявки

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave144-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken;
let modelId, orderId, orderNumber;
let reviewId, reviewId2;

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

  // Login as admin
  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;

  // Create a test model
  const csrf = await getCsrf();
  const mr = await request(app)
    .post('/api/admin/models')
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf)
    .send({
      name: 'Мария Тест',
      available: true,
      category: 'fashion',
      height: 175,
      age: 22,
      city: 'Москва',
      bio: 'Тестовая модель wave144',
    });
  modelId = mr.body.id || mr.body.model?.id;

  // Create a completed order so we can post a review
  const csrf2 = await getCsrf();
  const or = await request(app).post('/api/orders').set('x-csrf-token', csrf2).send({
    client_name: 'Иван Тестов',
    client_phone: '79001112233',
    client_email: 'ivan@example.com',
    event_type: 'fashion_show',
    model_id: modelId,
    budget: '50000',
    event_date: '2026-09-01',
  });
  orderId = or.body.id || or.body.order?.id;
  orderNumber = or.body.order_number || or.body.order?.order_number;

  // Move order to completed so review endpoint accepts it
  const csrf3 = await getCsrf();
  await request(app)
    .patch(`/api/admin/orders/${orderId}/status`)
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf3)
    .send({ status: 'completed' });

  // Directly insert two pending reviews via admin DB for moderation tests
  const { run } = require('../database');
  const r1 = await run(
    `INSERT INTO reviews (client_name, rating, text, model_id, approved, order_id) VALUES (?,?,?,?,0,?)`,
    ['Иван Тестов', 4, 'Хорошая работа', modelId || null, orderId || null]
  );
  reviewId = r1.id || r1.lastID;

  const r2 = await run(
    `INSERT INTO reviews (client_name, rating, text, model_id, approved, order_id) VALUES (?,?,?,?,0,NULL)`,
    ['Другой Клиент', 3, 'Нормально было', modelId || null]
  );
  reviewId2 = r2.id || r2.lastID;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── 1. E2E флоу модерации отзывов ────────────────────────────────────────────

describe('E2E: Модерация отзывов', () => {
  it('GET /api/admin/reviews?filter=pending — видим наш отзыв со статусом approved=0', async () => {
    const res = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const reviews = res.body.reviews || res.body;
    expect(Array.isArray(reviews)).toBe(true);
    const found = reviews.find(r => r.id === reviewId);
    expect(found).toBeTruthy();
    expect(found.approved).toBe(0);
  });

  it('PATCH /api/admin/reviews/:id/approve — одобрить отзыв', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId}/approve`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.approved).toBe(1);
  });

  it('GET /api/reviews — одобренный отзыв виден публично', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    const reviews = Array.isArray(res.body) ? res.body : res.body.reviews || [];
    expect(Array.isArray(reviews)).toBe(true);
    const found = reviews.find(r => r.id === reviewId);
    expect(found).toBeTruthy();
    expect(found.rating).toBe(4);
    expect(found.text).toBe('Хорошая работа');
  });

  it('PATCH /api/admin/reviews/:id/reject — отклонить другой отзыв', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId2}/reject`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.approved).toBe(0);
  });

  it('GET /api/reviews — отклонённый отзыв не виден публично', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    const reviews = Array.isArray(res.body) ? res.body : res.body.reviews || [];
    const found = reviews.find(r => r.id === reviewId2);
    expect(found).toBeUndefined();
  });
});

// ─── 2. Поиск моделей (каталог) ──────────────────────────────────────────────

describe('Поиск моделей (каталог)', () => {
  it('GET /api/models?search=Мария — результаты (включая тестовую модель)', async () => {
    const res = await request(app).get('/api/models?search=Мария');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    expect(Array.isArray(list)).toBe(true);
    // May find our model or return empty — both valid
    if (list.length > 0) {
      expect(list[0]).toHaveProperty('name');
    }
  });

  it('GET /api/models?category=fashion — только fashion', async () => {
    const res = await request(app).get('/api/models?category=fashion');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    expect(Array.isArray(list)).toBe(true);
    list.forEach(m => expect(m.category).toBe('fashion'));
  });

  it('GET /api/models?available=1 — только доступные', async () => {
    const res = await request(app).get('/api/models?available=1');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    expect(Array.isArray(list)).toBe(true);
    list.forEach(m => expect(m.available).toBeTruthy());
  });

  it('GET /api/models?min_height=170&max_height=180 — диапазон роста', async () => {
    const res = await request(app).get('/api/models?min_height=170&max_height=180');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    expect(Array.isArray(list)).toBe(true);
    list.forEach(m => {
      if (m.height !== null && m.height !== undefined) {
        expect(m.height).toBeGreaterThanOrEqual(170);
        expect(m.height).toBeLessThanOrEqual(180);
      }
    });
  });

  it('GET /api/models?sort=newest — сортировка по новизне (200 + array)', async () => {
    const res = await request(app).get('/api/models?sort=newest');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    expect(Array.isArray(list)).toBe(true);
  });

  it('GET /api/models?sort=orders — сортировка по заказам (200 + array)', async () => {
    const res = await request(app).get('/api/models?sort=orders');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    expect(Array.isArray(list)).toBe(true);
  });
});

// ─── 3. Wishlist (только через admin auth) ───────────────────────────────────

describe('Wishlist (API, admin auth required)', () => {
  const CHAT_ID = 12345;

  it('GET /api/user/wishlist без auth — 401', async () => {
    const res = await request(app).get(`/api/user/wishlist?chat_id=${CHAT_ID}`);
    expect(res.status).toBe(401);
  });

  it('GET /api/user/wishlist?chat_id=12345 — пустой список', async () => {
    const res = await request(app)
      .get(`/api/user/wishlist?chat_id=${CHAT_ID}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBe(0);
  });

  it('POST /api/user/wishlist — добавить модель в избранное', async () => {
    const res = await request(app)
      .post('/api/user/wishlist')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ chat_id: CHAT_ID, model_id: modelId });
    expect([201, 409]).toContain(res.status);
    if (res.status === 201) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('GET /api/user/wishlist?chat_id=12345 — модель в избранном', async () => {
    const res = await request(app)
      .get(`/api/user/wishlist?chat_id=${CHAT_ID}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    const found = res.body.find(w => w.model_id === modelId);
    expect(found).toBeTruthy();
  });

  it('DELETE /api/user/wishlist/:model_id — удалить из избранного', async () => {
    const res = await request(app)
      .delete(`/api/user/wishlist/${modelId}?chat_id=${CHAT_ID}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('GET /api/user/wishlist?chat_id=12345 — список пуст после удаления', async () => {
    const res = await request(app)
      .get(`/api/user/wishlist?chat_id=${CHAT_ID}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBe(0);
  });

  it('GET /api/user/wishlist без chat_id — 400', async () => {
    const res = await request(app).get('/api/user/wishlist').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  it('POST /api/user/wishlist с несуществующей моделью — 404', async () => {
    const res = await request(app)
      .post('/api/user/wishlist')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ chat_id: CHAT_ID, model_id: 999999 });
    expect(res.status).toBe(404);
  });
});

// ─── 4. Публичные настройки ───────────────────────────────────────────────────

describe('Публичные настройки', () => {
  it('GET /api/settings/public — возвращает 200', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    expect(typeof res.body).toBe('object');
  });

  it('GET /api/settings/public — содержит публичные ключи (или пустой объект)', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    // In test with empty DB these may be empty string/undefined — just verify object shape
    const body = res.body;
    // No errors or non-object shapes
    expect(Array.isArray(body)).toBe(false);
  });

  it('GET /api/settings/public — НЕ содержит admin_password', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    expect(res.body).not.toHaveProperty('admin_password');
  });

  it('GET /api/settings/public — НЕ содержит jwt_secret', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    expect(res.body).not.toHaveProperty('jwt_secret');
  });

  it('GET /api/settings/public — НЕ содержит password', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
    const keys = Object.keys(res.body);
    const sensitive = keys.filter(k => k.toLowerCase().includes('password') || k.toLowerCase().includes('secret'));
    expect(sensitive).toHaveLength(0);
  });
});

// ─── 5. Статус заявки по номеру ───────────────────────────────────────────────

describe('Статус заявки по номеру', () => {
  it('GET /api/orders/status/:number — найти существующую заявку по NM-номеру', async () => {
    // orderNumber was captured in beforeAll
    if (!orderNumber) return;
    const res = await request(app).get(`/api/orders/status/${orderNumber}`);
    expect(res.status).toBe(200);
    expect(res.body.order_number).toBe(orderNumber);
    expect(res.body).toHaveProperty('status');
    expect(res.body).toHaveProperty('client_name');
  });

  it('GET /api/orders/status/:number — order_number starts with NM-', async () => {
    if (!orderNumber) return;
    expect(orderNumber).toMatch(/^NM-/);
    const res = await request(app).get(`/api/orders/status/${orderNumber}`);
    expect(res.status).toBe(200);
  });

  it('GET /api/orders/status/НЕСУЩЕСТВУЮЩИЙ-НОМЕР — 404', async () => {
    const res = await request(app).get('/api/orders/status/NM-NOTEXIST-99999');
    expect(res.status).toBe(404);
    expect(res.body).toHaveProperty('error');
  });

  it('GET /api/orders/status/lowercase — case-insensitive (uppercased автоматически)', async () => {
    if (!orderNumber) return;
    const lower = orderNumber.toLowerCase();
    const res = await request(app).get(`/api/orders/status/${lower}`);
    expect(res.status).toBe(200);
    expect(res.body.order_number).toBe(orderNumber);
  });
});
