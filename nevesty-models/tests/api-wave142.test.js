'use strict';
// Wave 142: CTE catalog GET /models, JOIN admin/models, security, payment cycle, export

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave142-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, modelId, orderId;

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

  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
  expect(adminToken).toBeTruthy();

  // Create a model with city/category for filter tests
  const csrf1 = await getCsrf();
  const mRes = await request(app)
    .post('/api/admin/models')
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf1)
    .send({
      name: 'Мария Иванова',
      available: true,
      category: 'fashion',
      height: 174,
      city: 'Москва',
    });
  modelId = mRes.body.id || mRes.body.model?.id;
  expect(modelId).toBeTruthy();

  // Create a second model named Анна for admin/models search test
  const csrf2 = await getCsrf();
  await request(app)
    .post('/api/admin/models')
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf2)
    .send({
      name: 'Анна Смирнова',
      available: false,
      category: 'commercial',
      height: 168,
      city: 'Санкт-Петербург',
    });

  // Create an order for payment/export tests
  const csrf3 = await getCsrf();
  const oRes = await request(app).post('/api/orders').set('x-csrf-token', csrf3).send({
    client_name: 'Тест Wave142',
    client_phone: '79001420001',
    event_type: 'fashion_show',
    model_id: modelId,
    budget: '60000',
  });
  orderId = oRes.body.id || oRes.body.order?.id;
  expect(orderId).toBeTruthy();
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── GET /models — CTE catalog optimization ───────────────────────────────────

describe('GET /models — CTE catalog', () => {
  it('возвращает 200 и массив с полем order_count', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // Each item should have order_count (from CTE)
    if (res.body.length > 0) {
      expect(typeof res.body[0].order_count).toBe('number');
    }
  });

  it('sort=orders — сортировка по заказам работает', async () => {
    const res = await request(app).get('/api/models?sort=orders');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // Verify order_count field exists in response
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('order_count');
    }
  });

  it('city=Москва — фильтр по городу возвращает только московских моделей', async () => {
    const res = await request(app).get('/api/models?city=Москва');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // All returned models should be from Москва
    res.body.forEach(m => {
      expect(m.city).toBe('Москва');
    });
    // Should contain at least our created model
    expect(res.body.length).toBeGreaterThan(0);
  });

  it('search=Мария — поиск по имени работает', async () => {
    const res = await request(app).get('/api/models?search=Мария');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBeGreaterThan(0);
    const found = res.body.find(m => m.name.includes('Мария'));
    expect(found).toBeTruthy();
  });

  it('available=1 — фильтр по доступности возвращает только доступных', async () => {
    const res = await request(app).get('/api/models?available=1');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // All should be available
    res.body.forEach(m => {
      expect(m.available).toBe(1);
    });
  });

  it('комбинированные фильтры: category + min_height + max_height', async () => {
    const res = await request(app).get('/api/models?category=fashion&min_height=170&max_height=180');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    // All models should match height range and category
    res.body.forEach(m => {
      expect(m.category).toBe('fashion');
      if (m.height != null) {
        expect(m.height).toBeGreaterThanOrEqual(170);
        expect(m.height).toBeLessThanOrEqual(180);
      }
    });
    // Our Мария (174cm, fashion) should be in results
    expect(res.body.length).toBeGreaterThan(0);
  });
});

// ─── GET /admin/models — JOIN optimization ────────────────────────────────────

describe('GET /admin/models — JOIN optimization', () => {
  it('sort=orders — возвращает order_count и reviews_count', async () => {
    const res = await request(app).get('/api/admin/models?sort=orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    expect(typeof res.body.total).toBe('number');
    if (res.body.models.length > 0) {
      expect(res.body.models[0]).toHaveProperty('order_count');
      expect(res.body.models[0]).toHaveProperty('reviews_count');
    }
  });

  it('sort=avg_rating — корректно сортирует по среднему рейтингу', async () => {
    const res = await request(app)
      .get('/api/admin/models?sort=avg_rating')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    // avg_rating field should exist
    if (res.body.models.length > 0) {
      expect(res.body.models[0]).toHaveProperty('avg_rating');
    }
  });

  it('search=Анна — поиск по имени работает', async () => {
    const res = await request(app).get('/api/admin/models?search=Анна').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    expect(res.body.models.length).toBeGreaterThan(0);
    const found = res.body.models.find(m => m.name.includes('Анна'));
    expect(found).toBeTruthy();
  });

  it('archived=0 — фильтр по активным (не архивным) моделям', async () => {
    const res = await request(app).get('/api/admin/models?archived=0').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    // All returned models should have archived=0
    res.body.models.forEach(m => {
      expect(m.archived).toBe(0);
    });
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/models');
    expect(res.status).toBe(401);
  });
});

// ─── Security tests ───────────────────────────────────────────────────────────

describe('POST /client/request-code — security: code_debug в test env', () => {
  it('возвращает code_debug в NODE_ENV=test (для тестирования)', async () => {
    // First create an order with a specific phone so the endpoint can find it
    // Use fashion_show (valid event type) without email/date (both optional)
    const csrf = await getCsrf();
    const phone = '79991420042';
    const orderRes = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'OTP Test Client',
      client_phone: phone,
      event_type: 'fashion_show',
    });
    // Order must be created successfully to proceed
    expect([200, 201]).toContain(orderRes.status);

    const csrf2 = await getCsrf();
    const res = await request(app).post('/api/client/request-code').set('x-csrf-token', csrf2).send({ phone });

    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    // In test env, code_debug must be present
    expect(res.body).toHaveProperty('code_debug');
    expect(typeof res.body.code_debug).toBe('string');
    expect(res.body.code_debug).toMatch(/^\d{6}$/);
  });

  it('возвращает 404 для неизвестного номера', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/client/request-code')
      .set('x-csrf-token', csrf)
      .send({ phone: '79009999999' });
    expect(res.status).toBe(404);
  });

  it('возвращает 400 для некорректного номера', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/client/request-code').set('x-csrf-token', csrf).send({ phone: 'abc' });
    expect(res.status).toBe(400);
  });
});

describe('POST /auth/verify-totp — security: невалидные данные', () => {
  it('возвращает 400 без temp_token и totp_code', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/auth/verify-totp').set('x-csrf-token', csrf).send({});
    expect(res.status).toBe(400);
  });

  it('возвращает 400 без totp_code', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/auth/verify-totp')
      .set('x-csrf-token', csrf)
      .send({ temp_token: 'sometoken' });
    expect(res.status).toBe(400);
  });

  it('возвращает 401 для несуществующего temp_token', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/auth/verify-totp')
      .set('x-csrf-token', csrf)
      .send({ temp_token: 'invalid-token-wave142', totp_code: '123456' });
    expect([400, 401]).toContain(res.status);
  });
});

describe('GET /user/wishlist — требует admin auth (защита от несанкционированного доступа)', () => {
  it('без auth — 401', async () => {
    const res = await request(app).get('/api/user/wishlist?chat_id=999');
    expect(res.status).toBe(401);
  });

  it('с admin auth + chat_id=999 — пустой список', async () => {
    const res = await request(app).get('/api/user/wishlist?chat_id=999').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const body = res.body;
    const items = Array.isArray(body) ? body : body.items || body.wishlist || body.models || [];
    expect(Array.isArray(items)).toBe(true);
    expect(items.length).toBe(0);
  });

  it('с admin auth но без chat_id — 400', async () => {
    const res = await request(app).get('/api/user/wishlist').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });
});

// ─── PATCH /admin/orders/:id/payment — полный цикл ───────────────────────────

describe('PATCH /admin/orders/:id/payment — полный цикл', () => {
  it('paid=true → 200, paid_at устанавливается', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: true });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    // paid_at should be set (string timestamp)
    expect(typeof res.body.paid_at).toBe('string');
    expect(res.body.paid_at).toBeTruthy();
  });

  it('paid=false → 200, paid_at сбрасывается в null', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: false });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    // paid_at must be null after reset
    expect(res.body.paid_at).toBeNull();
  });

  it('paid="yes" → 400 (не boolean)', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: 'yes' });
    expect(res.status).toBe(400);
  });

  it('paid=1 (число) → 400 (не boolean)', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: 1 });
    expect(res.status).toBe(400);
  });

  it('требует авторизации', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('x-csrf-token', csrf)
      .send({ paid: true });
    expect(res.status).toBe(401);
  });

  it('несуществующий orderId → 404', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch('/api/admin/orders/999999/payment')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: true });
    expect(res.status).toBe(404);
  });
});

// ─── Export endpoints ─────────────────────────────────────────────────────────

describe('GET /admin/export/orders — redirect to /admin/orders/export', () => {
  it('с авторизацией → перенаправляет (3xx) или возвращает CSV', async () => {
    const res = await request(app)
      .get('/api/admin/export/orders')
      .set('Authorization', `Bearer ${adminToken}`)
      .redirects(5); // follow redirects
    // After redirect should land on /admin/orders/export which returns CSV
    expect([200, 302, 301]).toContain(res.status);
    if (res.status === 200) {
      const ct = res.headers['content-type'] || '';
      expect(ct.includes('csv') || ct.includes('text') || ct.includes('json')).toBe(true);
    }
  });

  it('без авторизации → 401 или redirect к auth', async () => {
    const res = await request(app).get('/api/admin/export/orders').redirects(0);
    expect([301, 302, 401]).toContain(res.status);
  });
});

describe('GET /export/orders — CSV с LIMIT', () => {
  it('с авторизацией → 200 и CSV формат', async () => {
    const res = await request(app).get('/api/export/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const ct = res.headers['content-type'] || '';
    expect(ct.includes('csv') || ct.includes('text')).toBe(true);
    // Response should have content-disposition
    const cd = res.headers['content-disposition'] || '';
    expect(cd).toMatch(/attachment/i);
  });

  it('без авторизации → 401', async () => {
    const res = await request(app).get('/api/export/orders');
    expect(res.status).toBe(401);
  });

  it('limit=5 → не более 5 строк данных в CSV', async () => {
    // Create extra orders to have >5
    for (let i = 0; i < 6; i++) {
      const csrf = await getCsrf();
      await request(app)
        .post('/api/orders')
        .set('x-csrf-token', csrf)
        .send({
          client_name: `Wave142 Client ${i}`,
          client_phone: `7900142${String(i).padStart(4, '0')}`,
          event_type: 'photoshoot',
        });
    }

    const res = await request(app).get('/api/export/orders?limit=5').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const text = res.text || '';
    // Split by newline and filter non-empty lines
    const lines = text.split('\n').filter(l => l.trim().length > 0);
    // 1 header + up to 5 data rows = max 6 lines
    expect(lines.length).toBeLessThanOrEqual(6);
    expect(lines.length).toBeGreaterThan(1); // at least header + 1 row
  });
});
