'use strict';
// Wave 141: analytics/funnel, analytics/top-models, analytics/event-types,
// analytics/sources, analytics/monthly, analytics/client-segments (structure),
// admin/broadcasts POST, admin/db-stats, admin/db/vacuum, admin/notifications,
// admin/notifications/read (mark-read), admin/crm-status, managers

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave141-test-secret-32-chars-ok!!';
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

  // Авторизация
  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
  expect(adminToken).toBeTruthy();

  // Создаём модель
  const csrf1 = await getCsrf();
  const mRes = await request(app)
    .post('/api/admin/models')
    .set('Authorization', `Bearer ${adminToken}`)
    .set('x-csrf-token', csrf1)
    .send({ name: 'Полина Звёздная', available: true, featured: false, height: 175, category: 'fashion' });
  modelId = mRes.body.id || mRes.body.model?.id;
  expect(modelId).toBeTruthy();

  // Создаём заявку
  const csrf2 = await getCsrf();
  const oRes = await request(app).post('/api/orders').set('x-csrf-token', csrf2).send({
    client_name: 'Тест Wave141',
    client_phone: '79001410001',
    event_type: 'photo_shoot',
    model_id: modelId,
  });
  orderId = oRes.body.id || oRes.body.order?.id;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── Analytics: Funnel ────────────────────────────────────────────────────────

describe('GET /admin/analytics/funnel', () => {
  it('возвращает stages, total и conversion_rate', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.stages)).toBe(true);
    expect(typeof res.body.total).toBe('number');
    expect(typeof res.body.conversion_rate).toBe('number');
  });

  it('stages содержат элементы с именем и счётчиком', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // Если stages непустой, проверяем структуру
    if (res.body.stages.length > 0) {
      const stage = res.body.stages[0];
      expect(stage).toHaveProperty('count');
    }
  });

  it('принимает параметр ?days=30', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/funnel?days=30')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.total).toBe('number');
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: Top Models ────────────────────────────────────────────────────

describe('GET /admin/analytics/top-models', () => {
  it('возвращает объект с полем models (Array)', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('принимает параметр ?limit=5', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-models?limit=5')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    expect(res.body.models.length).toBeLessThanOrEqual(5);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: Event-Types ───────────────────────────────────────────────────

describe('GET /admin/analytics/event-types', () => {
  it('возвращает объект с полем types (Array)', async () => {
    const res = await request(app).get('/api/admin/analytics/event-types').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.types)).toBe(true);
  });

  it('принимает параметр ?days=7', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/event-types?days=7')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.types)).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/event-types');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: Sources ───────────────────────────────────────────────────────

describe('GET /admin/analytics/sources', () => {
  it('возвращает объект с полем sources (Array)', async () => {
    const res = await request(app).get('/api/admin/analytics/sources').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.sources)).toBe(true);
  });

  it('принимает параметр ?days=30', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/sources?days=30')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.sources)).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/sources');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: Monthly ───────────────────────────────────────────────────────

describe('GET /admin/analytics/monthly', () => {
  it('возвращает months (Array) и count (number)', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.months)).toBe(true);
    expect(typeof res.body.count).toBe('number');
  });

  it('принимает параметр ?year=2025', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/monthly?year=2025')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.months)).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: Client Segments ───────────────────────────────────────────────

describe('GET /admin/analytics/client-segments', () => {
  it('возвращает объект или массив (segments определён)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // может быть объект или массив
    expect(typeof res.body === 'object' && res.body !== null).toBe(true);
  });

  it('принимает параметр ?days=90', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments?days=90')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/client-segments');
    expect(res.status).toBe(401);
  });
});

// ─── POST /admin/broadcasts ───────────────────────────────────────────────────

describe('POST /admin/broadcasts', () => {
  it('создаёт рассылку с валидным text', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ text: 'Тестовая рассылка wave141', segment: 'all' });
    expect(res.status).toBe(200);
    expect(typeof res.body.id).toBe('number');
    expect(typeof res.body.scheduled_at).toBe('string');
  });

  it('создаёт рассылку с сегментом completed', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ text: 'Рассылка для завершённых', segment: 'completed' });
    expect(res.status).toBe(200);
    expect(typeof res.body.id).toBe('number');
  });

  it('возвращает 400 при пустом тексте', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ text: '' });
    expect(res.status).toBe(400);
    expect(res.body.error).toBeTruthy();
  });

  it('возвращает 400 при scheduled_at в прошлом', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ text: 'Тест прошлой даты', scheduled_at: '2020-01-01T00:00:00Z' });
    expect(res.status).toBe(400);
  });

  it('требует авторизации', async () => {
    const res = await request(app).post('/api/admin/broadcasts').send({ text: 'Тест' });
    expect(res.status).toBe(401);
  });
});

// ─── GET /admin/db-stats ──────────────────────────────────────────────────────

describe('GET /admin/db-stats', () => {
  it('возвращает tables (Array), size_bytes и size_mb', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.tables)).toBe(true);
    expect(res.body.tables.length).toBeGreaterThan(0);
    expect(typeof res.body.size_bytes).toBe('number');
    expect(typeof res.body.size_mb).toBe('number');
    expect(Array.isArray(res.body.schema_versions)).toBe(true);
  });

  it('каждая таблица имеет name и count', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    for (const t of res.body.tables) {
      expect(typeof t.name).toBe('string');
      expect(typeof t.count).toBe('number');
    }
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/db-stats');
    expect(res.status).toBe(401);
  });
});

// ─── POST /admin/db/vacuum ────────────────────────────────────────────────────

describe('POST /admin/db/vacuum', () => {
  it('выполняет WAL checkpoint + VACUUM, возвращает ok:true', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/db/vacuum')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.message).toBe('string');
  });

  it('требует авторизации', async () => {
    const res = await request(app).post('/api/admin/db/vacuum').send({});
    expect(res.status).toBe(401);
  });
});

// ─── GET /admin/notifications ─────────────────────────────────────────────────

describe('GET /admin/notifications', () => {
  it('возвращает notifications (Array), unread_count, total', async () => {
    const res = await request(app).get('/api/admin/notifications').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.notifications)).toBe(true);
    expect(typeof res.body.unread_count).toBe('number');
    expect(typeof res.body.total).toBe('number');
  });

  it('каждое уведомление имеет id, type, title, read', async () => {
    const res = await request(app).get('/api/admin/notifications').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    for (const n of res.body.notifications) {
      expect(typeof n.id).toBe('string');
      expect(typeof n.type).toBe('string');
      expect(typeof n.title).toBe('string');
      expect(typeof n.read).toBe('boolean');
    }
  });

  it('фильтр ?status=unread работает', async () => {
    const res = await request(app)
      .get('/api/admin/notifications?status=unread')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.notifications)).toBe(true);
    // все уведомления должны быть непрочитанными
    for (const n of res.body.notifications) {
      expect(n.read).toBe(false);
    }
  });

  it('фильтр ?status=all возвращает полный список', async () => {
    const res = await request(app)
      .get('/api/admin/notifications?status=all')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.notifications)).toBe(true);
  });

  it('параметр ?limit=5 ограничивает количество', async () => {
    const res = await request(app).get('/api/admin/notifications?limit=5').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.notifications.length).toBeLessThanOrEqual(5);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/notifications');
    expect(res.status).toBe(401);
  });
});

// ─── POST /admin/notifications/read (mark-read) ───────────────────────────────

describe('POST /admin/notifications/read', () => {
  it('помечает уведомления как прочитанные по ids', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/notifications/read')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ ids: ['order_new_1', 'review_1'] });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('помечает все прочитанными при пустом ids (без массива)', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/notifications/read')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).post('/api/admin/notifications/read').send({ ids: [] });
    expect(res.status).toBe(401);
  });
});

// ─── PATCH /admin/notifications/read-all ─────────────────────────────────────

describe('PATCH /admin/notifications/read-all', () => {
  it('помечает все уведомления как прочитанные', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch('/api/admin/notifications/read-all')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
    expect(typeof res.body.count).toBe('number');
  });

  it('требует авторизации', async () => {
    const res = await request(app).patch('/api/admin/notifications/read-all').send({});
    expect(res.status).toBe(401);
  });
});

// ─── PATCH /admin/notifications/:id/read ─────────────────────────────────────

describe('PATCH /admin/notifications/:id/read', () => {
  it('помечает конкретное уведомление прочитанным (order_new)', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch('/api/admin/notifications/order_new_1/read')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it('помечает review_ уведомление прочитанным', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch('/api/admin/notifications/review_1/read')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it('возвращает 400 для невалидного id формата', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .patch('/api/admin/notifications/invalid_id/read')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(400);
  });

  it('требует авторизации', async () => {
    const res = await request(app).patch('/api/admin/notifications/order_new_1/read').send({});
    expect(res.status).toBe(401);
  });
});

// ─── PATCH /admin/orders/:id/assign (через PUT) ───────────────────────────────
// Назначение менеджера выполняется через PUT /admin/orders/:id с полем manager_id.
// PATCH /admin/orders/:id/assign может не существовать — используем PUT.

describe('PUT /admin/orders/:id (assign manager_id)', () => {
  it('назначает manager_id заявке (200 или 404 если нет менеджера)', async () => {
    if (!orderId) return;
    const csrf = await getCsrf();
    const res = await request(app)
      .put(`/api/admin/orders/${orderId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ manager_id: 1 });
    // 200 если менеджер существует, 400/404 если нет
    expect([200, 400, 404]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('возвращает 400 для невалидного ID заявки', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .put('/api/admin/orders/abc')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ manager_id: 1 });
    expect(res.status).toBe(400);
  });

  it('возвращает 404 для несуществующей заявки', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .put('/api/admin/orders/999999')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ manager_id: 1 });
    expect(res.status).toBe(404);
  });

  it('требует авторизации', async () => {
    const res = await request(app).put(`/api/admin/orders/1`).send({ manager_id: 1 });
    expect(res.status).toBe(401);
  });
});

// ─── GET /admin/crm-status ────────────────────────────────────────────────────
// Endpoint: GET /api/admin/crm-status (не /crm/status)

describe('GET /admin/crm-status', () => {
  it('возвращает статус CRM интеграций (generic, amocrm, bitrix24)', async () => {
    const res = await request(app).get('/api/admin/crm-status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.generic).toBe('boolean');
    expect(typeof res.body.amocrm).toBe('boolean');
    expect(typeof res.body.bitrix24).toBe('boolean');
  });

  it('в тест-среде все интеграции отключены (false)', async () => {
    const res = await request(app).get('/api/admin/crm-status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    // В тестовой среде env переменные не установлены
    expect(res.body.generic).toBe(false);
    expect(res.body.amocrm).toBe(false);
    expect(res.body.bitrix24).toBe(false);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/crm-status');
    expect(res.status).toBe(401);
  });
});

// ─── GET /admin/broadcasts ────────────────────────────────────────────────────

describe('GET /admin/broadcasts', () => {
  it('возвращает список рассылок (массив)', async () => {
    const res = await request(app).get('/api/admin/broadcasts').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('принимает ?limit=5', async () => {
    const res = await request(app).get('/api/admin/broadcasts?limit=5').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body.length).toBeLessThanOrEqual(5);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/broadcasts');
    expect(res.status).toBe(401);
  });
});

// ─── GET /admin/broadcasts/count ─────────────────────────────────────────────

describe('GET /admin/broadcasts/count', () => {
  it('возвращает count для сегмента all', async () => {
    const res = await request(app)
      .get('/api/admin/broadcasts/count?segment=all')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.count).toBe('number');
  });

  it('возвращает count для сегмента completed', async () => {
    const res = await request(app)
      .get('/api/admin/broadcasts/count?segment=completed')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.count).toBe('number');
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/broadcasts/count');
    expect(res.status).toBe(401);
  });
});
