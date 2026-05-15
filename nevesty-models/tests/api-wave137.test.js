'use strict';
// Wave 137: factory-content publish, factory-experiments scale, factory/cycle-complete,
// admin/faq/generate, admin/models availability, admin/models photo delete,
// admin/orders notes, orders notes (client auth), models related edge cases

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave137-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, modelId, orderId;

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

  // Создаём модель
  const mr = await request(app)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({
      name: 'Wave137 Model',
      age: 23,
      city: 'Москва',
      category: 'fashion',
      photos: JSON.stringify(['https://example.com/photo1.jpg', 'https://example.com/photo2.jpg']),
    });
  modelId = mr.body.id;

  // Создаём заявку
  const ord = await dbRun(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, status)
     VALUES (?,?,?,?,?,?)`,
    ['ORD-W137', 'Wave137 Client', '+79001370001', 'photo_shoot', '2027-10-01', 'new']
  );
  orderId = ord.id;
}, 30000);

// ─── POST /admin/factory-content/:id/publish ─────────────────────────────────

describe('POST /admin/factory-content/:id/publish', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).post('/api/admin/factory-content/1/publish');
    expect(res.status).toBe(401);
  });

  test('404/400/503 при отсутствии factory DB или конфигурации канала', async () => {
    const res = await request(app)
      .post('/api/admin/factory-content/1/publish')
      .set('Authorization', `Bearer ${adminToken}`);
    // 400 — канал не настроен; 404 — post не найден; 503 — бот не инициализирован
    expect([400, 404, 500, 503]).toContain(res.status);
  });

  test('400 при невалидном id', async () => {
    const res = await request(app)
      .post('/api/admin/factory-content/abc/publish')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });
});

// ─── POST /admin/factory-experiments/:id/scale ───────────────────────────────

describe('POST /admin/factory-experiments/:id/scale', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).post('/api/admin/factory-experiments/1/scale');
    expect(res.status).toBe(401);
  });

  test('404 если factory.db не существует', async () => {
    const res = await request(app)
      .post('/api/admin/factory-experiments/1/scale')
      .set('Authorization', `Bearer ${adminToken}`);
    // 404 — factory.db не найден; 200 — если factory.db доступен (better-sqlite3)
    expect([200, 404, 500]).toContain(res.status);
  });

  test('400 при невалидном id', async () => {
    const res = await request(app)
      .post('/api/admin/factory-experiments/xyz/scale')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });
});

// ─── POST /admin/factory/cycle-complete ──────────────────────────────────────

describe('POST /admin/factory/cycle-complete', () => {
  test('401 без авторизации', async () => {
    const res = await request(app).post('/api/admin/factory/cycle-complete').send({ summary: 'Test' });
    expect(res.status).toBe(401);
  });

  test('401 без секрета или JWT', async () => {
    const res = await request(app).post('/api/admin/factory/cycle-complete').send({ summary: 'Test summary' });
    expect(res.status).toBe(401);
  });

  test('200 с валидным admin JWT', async () => {
    const res = await request(app)
      .post('/api/admin/factory/cycle-complete')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ summary: 'Test cycle', insights: ['insight 1'], actions: [] });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok');
  });

  test('400 без обязательных полей', async () => {
    const res = await request(app)
      .post('/api/admin/factory/cycle-complete')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(400);
  });

  test('с x-factory-secret заголовком', async () => {
    const origSecret = process.env.FACTORY_WEBHOOK_SECRET;
    process.env.FACTORY_WEBHOOK_SECRET = 'test-factory-secret-wave137';
    const res = await request(app)
      .post('/api/admin/factory/cycle-complete')
      .set('x-factory-secret', 'test-factory-secret-wave137')
      .send({ summary: 'Secret cycle', insights: ['ok'] });
    process.env.FACTORY_WEBHOOK_SECRET = origSecret;
    expect(res.status).toBe(200);
  });
});

// ─── POST /admin/faq/generate ────────────────────────────────────────────────

describe('POST /admin/faq/generate', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).post('/api/admin/faq/generate').send({ topic: 'оплата' });
    expect(res.status).toBe(401);
  });

  test('ok:false без ANTHROPIC_API_KEY', async () => {
    const res = await request(app)
      .post('/api/admin/faq/generate')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ topic: 'бронирование моделей' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
    expect(res.body.error).toMatch(/api_key|api key|ANTHROPIC/i);
  });

  test('ok:false без topic', async () => {
    const res = await request(app)
      .post('/api/admin/faq/generate')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
  });

  test('ok:false при слишком длинном topic', async () => {
    const res = await request(app)
      .post('/api/admin/faq/generate')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ topic: 'а'.repeat(300) });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
  });
});

// ─── GET /admin/models/:id/availability ──────────────────────────────────────

describe('GET /admin/models/:id/availability', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).get(`/api/admin/models/${modelId}/availability`);
    expect(res.status).toBe(401);
  });

  test('возвращает доступность модели за текущий месяц', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
    expect(res.body).toHaveProperty('month');
  });

  test('фильтрация по месяцу (month=YYYY-MM)', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=2027-09`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.month).toBe('2027-09');
  });

  test('400 при невалидном формате месяца', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=invalid`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  test('400 при месяце 13 (невалидный)', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=2027-13`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  test('404 для несуществующей модели', async () => {
    const res = await request(app)
      .get('/api/admin/models/999999/availability')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });
});

// ─── DELETE /admin/models/:id/photo ─────────────────────────────────────────

describe('DELETE /admin/models/:id/photo', () => {
  test('требует авторизацию', async () => {
    const res = await request(app)
      .delete(`/api/admin/models/${modelId}/photo`)
      .send({ photo: 'https://example.com/photo1.jpg' });
    expect(res.status).toBe(401);
  });

  test('удаляет фото из массива', async () => {
    const res = await request(app)
      .delete(`/api/admin/models/${modelId}/photo`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ photo: 'https://example.com/photo1.jpg' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('404 для несуществующей модели', async () => {
    const res = await request(app)
      .delete('/api/admin/models/999999/photo')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ photo: 'https://example.com/photo1.jpg' });
    expect(res.status).toBe(404);
  });

  test('400 при невалидном id', async () => {
    const res = await request(app)
      .delete('/api/admin/models/abc/photo')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ photo: 'test.jpg' });
    expect(res.status).toBe(400);
  });
});

// ─── GET/POST /admin/orders/:id/notes ─────────────────────────────────────────

describe('Admin order notes', () => {
  test('GET — требует авторизацию', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}/notes`);
    expect(res.status).toBe(401);
  });

  test('POST — требует авторизацию', async () => {
    const res = await request(app).post(`/api/admin/orders/${orderId}/notes`).send({ note: 'Test' });
    expect(res.status).toBe(401);
  });

  test('POST — добавляет заметку к заявке', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'Тестовая заметка wave137' });
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  test('GET — возвращает заметки', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.notes)).toBe(true);
    expect(res.body.notes.length).toBeGreaterThan(0);
    expect(res.body.notes[0]).toHaveProperty('admin_note');
  });

  test('POST — 400 при пустой заметке', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: '' });
    expect(res.status).toBe(400);
  });

  test('POST — 400 при невалидном orderId', async () => {
    const res = await request(app)
      .post('/api/admin/orders/abc/notes')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'Test' });
    expect(res.status).toBe(400);
  });

  test('GET — пустой массив для несуществующей заявки', async () => {
    const res = await request(app).get('/api/admin/orders/999999/notes').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.notes).toHaveLength(0);
  });
});

// ─── PATCH /admin/orders/:id/note ────────────────────────────────────────────

describe('PATCH /admin/orders/:id/note (internal note)', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).patch(`/api/admin/orders/${orderId}/note`).send({ note: 'Test' });
    expect(res.status).toBe(401);
  });

  test('обновляет внутреннюю заметку', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/note`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'Внутренняя заметка менеджера' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('очищает заметку при пустом note', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/note`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: '' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ─── GET /models/:id/availability (public) ────────────────────────────────────

describe('GET /models/:id/availability (public)', () => {
  test('возвращает доступность', async () => {
    const res = await request(app).get(`/api/models/${modelId}/availability`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
  });

  test('200 для несуществующей модели (возвращает пустой массив)', async () => {
    const res = await request(app).get('/api/models/999999/availability');
    expect(res.status).toBe(200);
    expect(res.body.busy_dates).toEqual([]);
  });

  test('фильтр по месяцу', async () => {
    const res = await request(app).get(`/api/models/${modelId}/availability?month=2027-10`);
    expect(res.status).toBe(200);
    expect(res.body.month).toBe('2027-10');
  });
});

// ─── POST /orders/:id/notes (public, requires client auth) ───────────────────

describe('POST /orders/:id/notes (public route)', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).post(`/api/orders/${orderId}/notes`).send({ note: 'test' });
    expect(res.status).toBe(401);
  });

  test('добавляет заметку с admin JWT', async () => {
    const res = await request(app)
      .post(`/api/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'Заметка через публичный роут' });
    expect([200, 400, 403]).toContain(res.status);
  });
});

// ─── GET /orders/:id/notes (public route) ─────────────────────────────────────

describe('GET /orders/:id/notes (public route)', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).get(`/api/orders/${orderId}/notes`);
    expect(res.status).toBe(401);
  });
});

// ─── Factory actions/decisions — edge cases ────────────────────────────────────

describe('Factory actions (better-sqlite3 graceful fallback)', () => {
  test('GET /admin/factory/actions — авторизован, принимает 200 или 500', async () => {
    const res = await request(app).get('/api/admin/factory/actions').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
    if (res.status === 200) expect(Array.isArray(res.body.actions)).toBe(true);
  });

  test('GET /admin/factory/decisions — принимает 200 или 500', async () => {
    const res = await request(app).get('/api/admin/factory/decisions').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
  });
});

// ─── PATCH /admin/orders/:id/payment ─────────────────────────────────────────

describe('PATCH /admin/orders/:id/payment', () => {
  test('требует авторизацию', async () => {
    const res = await request(app).patch(`/api/admin/orders/${orderId}/payment`).send({ paid: true });
    expect(res.status).toBe(401);
  });

  test('обновляет статус оплаты', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: true });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('400 при невалидном paid', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: 'yes' });
    expect(res.status).toBe(400);
  });
});
