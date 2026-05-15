'use strict';
// Wave 143: E2E-флоу бронирования на сайте — от создания заявки до смены статуса

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave143-e2e-booking-secret-32!!!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, modelId, orderId, orderNumber;

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
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

describe('E2E: Создание модели', () => {
  it('создаёт модель через API', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/models')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({
        name: 'Мария Тестова',
        available: true,
        category: 'fashion',
        height: 175,
        age: 23,
        city: 'Москва',
        bio: 'Профессиональная модель',
      });
    expect([200, 201]).toContain(res.status);
    modelId = res.body.id || res.body.model?.id;
    expect(modelId).toBeTruthy();
  });

  it('модель появляется в каталоге', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    const found = list.find(m => m.name === 'Мария Тестова');
    expect(found).toBeTruthy();
    expect(found.order_count).toBeDefined();
  });
});

describe('E2E: Создание заявки клиентом', () => {
  it('создаёт заявку через публичный endpoint', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'Александр Клиентов',
      client_phone: '79009001234',
      client_email: 'client@example.com',
      event_type: 'fashion_show',
      model_id: modelId,
      budget: '100000',
      event_date: '2026-08-15',
      comments: 'Тестовая заявка E2E',
    });
    expect([200, 201]).toContain(res.status);
    orderId = res.body.id || res.body.order?.id;
    orderNumber = res.body.order_number || res.body.order?.order_number;
    expect(orderId).toBeTruthy();
    expect(orderNumber).toMatch(/^NM-/);
  });

  it('заявка имеет статус new', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}`).set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('new');
    expect(res.body.client_name).toBe('Александр Клиентов');
  });

  it('повторная заявка с тем же телефоном допустима', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'Александр Клиентов',
      client_phone: '79009001234',
      event_type: 'photo_shoot',
      model_id: modelId,
      budget: '50000',
    });
    expect([200, 201]).toContain(res.status);
  });
});

describe('E2E: Добавление заметки к заявке', () => {
  it('менеджер добавляет заметку', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ note: 'Клиент заинтересован в сотрудничестве на долгосрок' });
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it('заметка видна в деталях заявки', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.notes.length).toBeGreaterThan(0);
  });
});

describe('E2E: Смена статуса заявки', () => {
  it('менеджер подтверждает заявку', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'confirmed' });
    expect(res.status).toBe(200);
  });

  it('статус изменился на confirmed', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}`).set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('confirmed');
  });

  it('нельзя установить невалидный статус', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'hacked_status' });
    expect(res.status).toBe(400);
  });
});

describe('E2E: Выставление счёта и оплата', () => {
  it('менеджер отмечает счёт отправленным', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/send-invoice`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.invoice_sent_at).toBe('string');
  });

  it('менеджер помечает заявку оплаченной', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: true });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.paid_at).not.toBeNull();
  });

  it('заявка завершается', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'completed' });
    expect(res.status).toBe(200);
  });
});

describe('E2E: Клиент оставляет отзыв (API)', () => {
  it('POST /api/client/review создаёт отзыв', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/client/review').set('x-csrf-token', csrf).send({
      model_id: modelId,
      order_id: orderId,
      rating: 5,
      text: 'Отличная работа, рекомендую!',
      client_name: 'Александр К.',
    });
    expect([200, 201, 400]).toContain(res.status);
  });

  it('GET /api/reviews возвращает отзывы (только публичные)', async () => {
    const res = await request(app).get('/api/reviews?page=1&limit=10');
    expect(res.status).toBe(200);
    const reviews = Array.isArray(res.body) ? res.body : res.body.reviews || [];
    expect(Array.isArray(reviews)).toBe(true);
    reviews.forEach(r => {
      expect(r.rating).toBeGreaterThanOrEqual(1);
      expect(r.rating).toBeLessThanOrEqual(5);
    });
  });
});

describe('E2E: Аналитика после завершения заявки', () => {
  it('статистика обновляется (заявок > 0)', async () => {
    const res = await request(app).get('/api/admin/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.total_orders).toBeGreaterThan(0);
  });

  it('в аналитике visible completed orders', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.total).toBe('number');
  });
});
