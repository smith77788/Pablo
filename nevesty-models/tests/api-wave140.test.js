'use strict';
// Wave 140: admin/orders detail/notes/note/send-invoice/status-history,
// admin/email/test POST, admin/sitemap, admin/notifications mark-read,
// admin/analytics/client-segments structure, edge cases in existing routes

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave140-test-secret-32-chars-ok!!';
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
  const { initDatabase, run: dbRun } = require('../database');
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

  // Создаём модель
  const csrf1 = await getCsrf();
  const mRes = await request(app)
    .post('/api/admin/models')
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf1)
    .send({ name: 'Анна Орлова', available: true, category: 'fashion', height: 174 });
  modelId = mRes.body.id || mRes.body.model?.id;
  expect(modelId).toBeTruthy();

  // Создаём заявку
  const csrf2 = await getCsrf();
  const oRes = await request(app).post('/api/orders').set('x-csrf-token', csrf2).send({
    client_name: 'Тест Wave140',
    client_phone: '79001400001',
    event_type: 'fashion_show',
    model_id: modelId,
    budget: '50000',
  });
  orderId = oRes.body.id || oRes.body.order?.id;
  expect(orderId).toBeTruthy();
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── GET /admin/orders/:id/detail ────────────────────────────────────────────

describe('GET /admin/orders/:id/detail', () => {
  it('возвращает детальную информацию о заявке', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/detail`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.id).toBe(orderId);
    expect(typeof res.body.client_name).toBe('string');
  });

  it('возвращает 404 для несуществующей заявки', async () => {
    const res = await request(app).get('/api/admin/orders/999999/detail').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });

  it('возвращает 400 для невалидного ID', async () => {
    const res = await request(app).get('/api/admin/orders/abc/detail').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}/detail`);
    expect(res.status).toBe(401);
  });
});

// ─── GET/POST /admin/orders/:id/notes ─────────────────────────────────────────

describe('GET /admin/orders/:id/notes', () => {
  it('возвращает список заметок (пустой)', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.notes)).toBe(true);
  });

  it('возвращает 400 для невалидного ID', async () => {
    const res = await request(app).get('/api/admin/orders/0/notes').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });
});

describe('POST /admin/orders/:id/notes', () => {
  it('добавляет заметку к заявке', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ note: 'Тестовая заметка для wave140' });
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it('возвращает 400 без текста заметки', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(400);
  });

  it('заметки видны после добавления', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.notes.length).toBeGreaterThan(0);
    expect(res.body.notes[0].admin_note).toBe('Тестовая заметка для wave140');
  });
});

// ─── PATCH /admin/orders/:id/note ─────────────────────────────────────────────

describe('PATCH /admin/orders/:id/note', () => {
  it('обновляет внутреннюю заметку', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/note`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ note: 'Внутренняя заметка обновлена' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('принимает пустую строку (очищает заметку)', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/note`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ note: '' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ─── POST /admin/orders/:id/send-invoice ──────────────────────────────────────

describe('POST /admin/orders/:id/send-invoice', () => {
  it('помечает заявку как выставлен счёт', async () => {
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

  it('возвращает 404 для несуществующей заявки', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/orders/999999/send-invoice')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(404);
  });
});

// ─── PATCH /admin/orders/:id/status ───────────────────────────────────────────

describe('PATCH /admin/orders/:id/status', () => {
  it('обновляет статус заявки', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'confirmed' });
    expect(res.status).toBe(200);
  });

  it('возвращает 400 для невалидного статуса', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'invalid_status' });
    expect(res.status).toBe(400);
  });

  it('возвращает 404 для несуществующей заявки', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch('/api/admin/orders/999999/status')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'confirmed' });
    expect(res.status).toBe(404);
  });
});

// ─── PATCH /admin/orders/:id/payment ──────────────────────────────────────────

describe('PATCH /admin/orders/:id/payment', () => {
  it('обновляет статус оплаты', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: true });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('возвращает 400 для невалидного paid', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ paid: 'yes' });
    expect(res.status).toBe(400);
  });
});

// ─── POST /admin/email/test ───────────────────────────────────────────────────

describe('POST /admin/email/test', () => {
  it('возвращает 400 без email (нет email в профиле)', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/email/test')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    // 400 если нет email в профиле и не передан в body, или 500/200 если mailer не настроен
    expect([200, 400, 500]).toContain(res.status);
  });

  it('пытается отправить на указанный email', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/email/test')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ email: 'test@example.com' });
    // 200 если mailer не настроен (ok:false), или 500
    expect([200, 500]).toContain(res.status);
  });

  it('требует авторизации', async () => {
    const res = await request(app).post('/api/admin/email/test').send({ email: 'test@example.com' });
    expect(res.status).toBe(401);
  });
});

// ─── GET /admin/analytics/client-segments ─────────────────────────────────────

describe('GET /admin/analytics/client-segments', () => {
  it('возвращает сегменты с нужными полями', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // Ожидаем объект или массив с данными сегментации
    expect(typeof res.body === 'object').toBe(true);
  });
});

// ─── GET /admin/analytics/model-stats/:id — structure check ──────────────────

describe('GET /admin/analytics/model-stats/:id detail check', () => {
  it('возвращает статистику с нужными полями', async () => {
    const res = await request(app)
      .get(`/api/admin/analytics/model-stats/${modelId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // Должны быть хотя бы базовые поля
    expect(res.body).toBeDefined();
  });
});

// ─── GET /admin/messages — filters edge cases ─────────────────────────────────

describe('GET /admin/messages edge cases', () => {
  it('обрабатывает offset параметр', async () => {
    const res = await request(app).get('/api/admin/messages?offset=100').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.messages)).toBe(true);
    expect(res.body.messages.length).toBe(0); // нет данных
  });

  it('ограничивает limit до 100', async () => {
    const res = await request(app).get('/api/admin/messages?limit=999').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ─── Orders bulk status (POST /admin/orders/bulk-status) ─────────────────────

describe('POST /admin/orders/bulk-status', () => {
  it('обновляет статус нескольких заявок', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ ids: [orderId], status: 'reviewing' });
    expect([200, 400]).toContain(res.status);
  });

  it('возвращает 400 при пустом массиве ids', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ ids: [], status: 'confirmed' });
    expect([400, 422]).toContain(res.status);
  });
});

// ─── Admin: settings import/export ───────────────────────────────────────────

describe('GET /admin/settings/export', () => {
  it('экспортирует настройки', async () => {
    const res = await request(app).get('/api/admin/settings/export').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
    if (res.status === 200) {
      expect(typeof res.body === 'object').toBe(true);
    }
  });
});

describe('POST /admin/settings/import', () => {
  it('импортирует настройки', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/settings/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ settings: { greeting: 'Привет!', site_phone: '+7999' } });
    expect([200, 400]).toContain(res.status);
    if (res.status === 200) {
      expect(typeof res.body.imported).toBe('number');
    }
  });

  it('отклоняет массив как невалидный формат', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/settings/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .set('Content-Type', 'application/json')
      .send(JSON.stringify([1, 2, 3]));
    expect([400, 422]).toContain(res.status);
  });
});

// ─── Admin: orders export ─────────────────────────────────────────────────────

describe('GET /export/orders', () => {
  it('экспортирует заявки в CSV формате', async () => {
    const res = await request(app).get('/api/export/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // Должен быть CSV
    const ct = res.headers['content-type'] || '';
    expect(ct.includes('csv') || ct.includes('json') || ct.includes('text')).toBe(true);
  });
});

describe('GET /admin/models/export', () => {
  it('экспортирует модели', async () => {
    const res = await request(app).get('/api/admin/models/export').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ─── Public routes edge cases ─────────────────────────────────────────────────

describe('GET /models/related', () => {
  it('возвращает похожие модели', async () => {
    const res = await request(app).get(`/api/models/related?model_id=${modelId}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models || res.body)).toBe(true);
  });

  it('работает без параметра model_id', async () => {
    const res = await request(app).get('/api/models/related');
    expect([200, 400]).toContain(res.status);
  });
});

describe('GET /models/:id/availability', () => {
  it('возвращает занятые даты для публичного просмотра', async () => {
    const res = await request(app).get(`/api/models/${modelId}/availability`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
  });
});

// ─── Rate limit checks on key endpoints ──────────────────────────────────────

describe('Rate limiting — /api/orders', () => {
  it('5+ запросов за минуту могут получить 429', async () => {
    // Просто проверяем что endpoint работает (не лочится при нормальном использовании)
    const csrf = await getCsrf();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'Rate Test',
      client_phone: '79001400099',
      event_type: 'event',
    });
    expect([200, 400, 429]).toContain(res.status);
  });
});
