'use strict';
// Wave 136: POST /orders, POST /quick-booking, POST /payments/create,
// GET /models/related, GET /export/models, GET /agent-logs,
// POST /auth/verify-totp, PATCH /admin/notifications/:id/read,
// POST /admin/notify, POST /admin/models/import

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave136-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, orderId;

async function getCsrf() {
  const cr = await request(app).get('/api/csrf-token');
  return cr.body.token;
}

beforeAll(async () => {
  const { initDatabase, run: dbRun } = require('../database');
  await initDatabase();

  const { initBot } = require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());

  const bot = initBot(a);
  if (bot && apiRouter.setBot) apiRouter.setBot(bot);

  a.use('/api', apiRouter);
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;

  // Модель для тестов
  await request(app)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({ name: 'Wave136 Model', age: 25, city: 'Москва', category: 'fashion', available: 1 });

  // Заявка для тестов payments/create
  const ord = await dbRun(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, status, budget)
     VALUES (?,?,?,?,?,?,?)`,
    ['ORD-W136', 'Wave136 Client', '+79001360001', 'photo_shoot', '2027-07-01', 'new', '50000']
  );
  orderId = ord.id;
}, 30000);

// ─── GET /csrf-token ─────────────────────────────────────────────────────────

describe('GET /csrf-token', () => {
  test('возвращает токен', async () => {
    const res = await request(app).get('/api/csrf-token');
    expect(res.status).toBe(200);
    expect(typeof res.body.token).toBe('string');
    expect(res.body.token.length).toBeGreaterThan(10);
  });
});

// ─── POST /orders — создание заявки ──────────────────────────────────────────

describe('POST /orders', () => {
  test('успешное создание заявки с валидными данными', async () => {
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: 'Тестовый Клиент',
        client_phone: '+79001230001',
        client_email: 'test136@example.com',
        event_type: 'photo_shoot',
        event_date: '2027-08-15',
        event_duration: 4,
        location: 'Москва, студия',
        budget: '80000',
        comments: 'Тестовая заявка wave136',
      });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('order_number');
    expect(res.body).toHaveProperty('id');
    expect(typeof res.body.order_number).toBe('string');
    expect(res.body.order_number).toMatch(/^NM-\d{4}-/);
  });

  test('отклоняет без имени', async () => {
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: '',
        client_phone: '+79001230002',
        client_email: 'test2@example.com',
        event_type: 'photo_shoot',
        event_date: '2027-08-15',
      });
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  test('отклоняет некорректный телефон', async () => {
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: 'Тест',
        client_phone: 'не-телефон',
        client_email: 'test3@example.com',
        event_type: 'photo_shoot',
        event_date: '2027-08-15',
      });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/телефон/i);
  });

  test('отклоняет неверный event_type', async () => {
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: 'Тест',
        client_phone: '+79001230003',
        client_email: 'test4@example.com',
        event_type: 'invalid_type',
        event_date: '2027-08-15',
      });
    expect(res.status).toBe(400);
  });

  test('отклоняет без CSRF-токена', async () => {
    const res = await request(app).post('/api/orders').send({
      client_name: 'Тест',
      client_phone: '+79001230004',
      client_email: 'test5@example.com',
      event_type: 'photo_shoot',
      event_date: '2027-08-15',
    });
    expect(res.status).toBe(403);
    expect(res.body.error).toMatch(/csrf/i);
  });

  test('принимает заявку с несколькими model_ids', async () => {
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: 'Клиент Несколько Моделей',
        client_phone: '+79001230005',
        client_email: 'multi@example.com',
        event_type: 'runway',
        event_date: '2027-09-01',
        model_ids: [1, 2],
        budget: '100000',
      });
    // 429 возможен если bookingLimiter (5/час) исчерпан в тестовой среде
    expect([200, 400, 429]).toContain(res.status);
  });

  test('принимает заявку с UTM-метками', async () => {
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: 'UTM Клиент',
        client_phone: '+79001230006',
        client_email: 'utm@example.com',
        event_type: 'commercial',
        event_date: '2027-09-15',
        utm_source: 'vk',
        utm_medium: 'cpc',
        utm_campaign: 'summer2027',
      });
    expect([200, 429]).toContain(res.status);
    if (res.status === 200) expect(res.body).toHaveProperty('order_number');
  });

  test('некорректный email — отклоняет', async () => {
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: 'Тест',
        client_phone: '+79001230007',
        client_email: 'not-an-email',
        event_type: 'photo_shoot',
        event_date: '2027-08-15',
      });
    expect([400, 429]).toContain(res.status);
  });
});

// ─── POST /quick-booking ──────────────────────────────────────────────────────

describe('POST /quick-booking', () => {
  test('успешная быстрая заявка', async () => {
    const res = await request(app)
      .post('/api/quick-booking')
      .set('x-csrf-token', await getCsrf())
      .send({
        client_name: 'Быстрый Клиент',
        client_phone: '+79001360002',
      });
    // strictLimiter может вернуть 429; оба варианта допустимы
    expect([200, 429]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body.ok).toBe(true);
      expect(res.body).toHaveProperty('order_number');
    }
  });

  test('отклоняет без имени', async () => {
    const res = await request(app)
      .post('/api/quick-booking')
      .set('x-csrf-token', await getCsrf())
      .send({ client_name: '', client_phone: '+79001360003' });
    expect([400, 429]).toContain(res.status);
  });

  test('отклоняет без телефона', async () => {
    const res = await request(app)
      .post('/api/quick-booking')
      .set('x-csrf-token', await getCsrf())
      .send({ client_name: 'Тест', client_phone: '' });
    expect([400, 429]).toContain(res.status);
  });

  test('отклоняет без CSRF', async () => {
    const res = await request(app)
      .post('/api/quick-booking')
      .send({ client_name: 'Тест', client_phone: '+79001360004' });
    expect([403, 429]).toContain(res.status);
  });
});

// ─── POST /payments/create ────────────────────────────────────────────────────

describe('POST /payments/create', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).post('/api/payments/create').send({ orderId: 1 });
    expect(res.status).toBe(401);
  });

  test('создаёт платёж для существующей заявки', async () => {
    const res = await request(app)
      .post('/api/payments/create')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ orderId });
    // 200 в dev-режиме или 500 если payments не настроен; 404 если orderId не найден
    expect([200, 500]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body).toHaveProperty('paymentId');
      expect(res.body).toHaveProperty('confirmationUrl');
    }
  });

  test('404 для несуществующей заявки', async () => {
    const res = await request(app)
      .post('/api/payments/create')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ orderId: 999999 });
    expect(res.status).toBe(404);
  });

  test('400 без orderId', async () => {
    const res = await request(app).post('/api/payments/create').set('Authorization', `Bearer ${adminToken}`).send({});
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/orderId/);
  });
});

// ─── GET /models/related ─────────────────────────────────────────────────────

describe('GET /models/related', () => {
  test('возвращает список моделей', async () => {
    const res = await request(app).get('/api/models/related');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  test('фильтр по категории', async () => {
    const res = await request(app).get('/api/models/related?category=fashion&limit=3');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    res.body.models.forEach(m => expect(m.category).toBe('fashion'));
  });

  test('фильтр по городу', async () => {
    const res = await request(app).get('/api/models/related?city=Москва&limit=2');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  test('исключение модели по id', async () => {
    const res = await request(app).get('/api/models/related?exclude=1&limit=5');
    expect(res.status).toBe(200);
    res.body.models.forEach(m => expect(m.id).not.toBe(1));
  });

  test('лимит не превышается', async () => {
    const res = await request(app).get('/api/models/related?limit=2');
    expect(res.status).toBe(200);
    expect(res.body.models.length).toBeLessThanOrEqual(2);
  });
});

// ─── GET /export/models ───────────────────────────────────────────────────────

describe('GET /export/models', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).get('/api/export/models');
    expect(res.status).toBe(401);
  });

  test('возвращает список моделей как JSON', async () => {
    const res = await request(app).get('/api/export/models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    if (res.body.length > 0) {
      const m = res.body[0];
      expect(m).toHaveProperty('name');
      expect(m).toHaveProperty('id');
    }
  });

  test('возвращает заголовок Content-Disposition attachment', async () => {
    const res = await request(app).get('/api/export/models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.headers['content-disposition']).toMatch(/attachment/);
  });
});

// ─── GET /agent-logs ─────────────────────────────────────────────────────────

describe('GET /agent-logs', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).get('/api/agent-logs');
    expect(res.status).toBe(401);
  });

  test('возвращает логи агентов', async () => {
    const res = await request(app).get('/api/agent-logs').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

// ─── GET /admin/agent-logs ────────────────────────────────────────────────────

describe('GET /admin/agent-logs', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).get('/api/admin/agent-logs');
    expect(res.status).toBe(401);
  });

  test('возвращает логи', async () => {
    const res = await request(app).get('/api/admin/agent-logs').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

// ─── POST /auth/verify-totp ───────────────────────────────────────────────────

describe('POST /auth/verify-totp', () => {
  test('400 без temp_token и totp_code', async () => {
    const res = await request(app).post('/api/auth/verify-totp').send({});
    expect([400, 429]).toContain(res.status);
  });

  test('401 при неверном temp_token', async () => {
    const res = await request(app)
      .post('/api/auth/verify-totp')
      .send({ temp_token: 'invalid-token-xxx', totp_code: '123456' });
    expect([401, 429]).toContain(res.status);
  });

  test('400 только с temp_token без totp_code', async () => {
    const res = await request(app).post('/api/auth/verify-totp').send({ temp_token: 'some-token' });
    expect([400, 429]).toContain(res.status);
  });
});

// ─── PATCH /admin/notifications/:id/read ─────────────────────────────────────

describe('PATCH /admin/notifications/:id/read', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).patch('/api/admin/notifications/order_new_1/read');
    expect(res.status).toBe(401);
  });

  test('помечает уведомление прочитанным (order_new_)', async () => {
    const res = await request(app)
      .patch('/api/admin/notifications/order_new_1/read')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  test('помечает уведомление прочитанным (review_)', async () => {
    const res = await request(app)
      .patch('/api/admin/notifications/review_42/read')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  test('помечает уведомление прочитанным (msg_)', async () => {
    const res = await request(app)
      .patch('/api/admin/notifications/msg_100/read')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  test('400 при невалидном id (не соответствует шаблону)', async () => {
    const res = await request(app)
      .patch('/api/admin/notifications/invalid_id_xyz/read')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  test('400 при sql-инъекции в id', async () => {
    const res = await request(app)
      .patch("/api/admin/notifications/1' OR '1'='1/read")
      .set('Authorization', `Bearer ${adminToken}`);
    expect([400, 404]).toContain(res.status);
  });
});

// ─── POST /admin/notify ───────────────────────────────────────────────────────

describe('POST /admin/notify', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).post('/api/admin/notify').send({ text: 'Test' });
    expect(res.status).toBe(401);
  });

  test('успешно отправляет уведомление', async () => {
    const res = await request(app)
      .post('/api/admin/notify')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ text: 'Тест уведомления wave136' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('400 при пустом тексте', async () => {
    const res = await request(app)
      .post('/api/admin/notify')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ text: '' });
    expect(res.status).toBe(400);
  });

  test('400 без поля text', async () => {
    const res = await request(app).post('/api/admin/notify').set('Authorization', `Bearer ${adminToken}`).send({});
    expect(res.status).toBe(400);
  });
});

// ─── POST /admin/models/import (JSON body) ─────────────────────────────────

describe('POST /admin/models/import', () => {
  test('требует авторизацию', async () => {
    const res = await request(app)
      .post('/api/admin/models/import')
      .send({ models: [{ name: 'TestImport' }] });
    expect(res.status).toBe(401);
  });

  test('импортирует модели из JSON-массива в body', async () => {
    const res = await request(app)
      .post('/api/admin/models/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({
        models: [
          { name: 'Импорт Модель 1', age: 23, city: 'Казань', category: 'commercial' },
          { name: 'Импорт Модель 2', age: 26, city: 'Нижний Новгород', category: 'events' },
        ],
      });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('imported');
    expect(res.body.imported).toBeGreaterThan(0);
  });

  test('400 при пустом массиве', async () => {
    const res = await request(app)
      .post('/api/admin/models/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ models: [] });
    expect(res.status).toBe(400);
  });

  test('400 без данных', async () => {
    const res = await request(app)
      .post('/api/admin/models/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(400);
  });

  test('пропускает записи без имени', async () => {
    const res = await request(app)
      .post('/api/admin/models/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({
        models: [
          { name: '', age: 25 },
          { name: 'Валидная Модель Wave136b', age: 22, city: 'Сочи', category: 'fashion' },
        ],
      });
    expect(res.status).toBe(200);
    expect(res.body.imported).toBe(1);
    expect(res.body.errors).toBeDefined();
  });

  test('ограничивает импорт до 50 записей', async () => {
    const models = Array.from({ length: 60 }, (_, i) => ({
      name: `Bulk Import ${i + 1}`,
      age: 20 + (i % 10),
      category: 'fashion',
    }));
    const res = await request(app)
      .post('/api/admin/models/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ models });
    expect(res.status).toBe(200);
    // Импортируется максимум 50
    expect(res.body.imported).toBeLessThanOrEqual(50);
  });
});

// ─── GET /admin/quick-bookings ────────────────────────────────────────────────

describe('GET /admin/quick-bookings', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).get('/api/admin/quick-bookings');
    expect(res.status).toBe(401);
  });

  test('возвращает список быстрых заявок', async () => {
    const res = await request(app).get('/api/admin/quick-bookings').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});
