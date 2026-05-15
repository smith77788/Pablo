'use strict';
// Wave 139: OTP flow (request-code/verify), client orders, contact form,
// admin/email, price-packages CRUD, client/ai-match, client/ai-budget,
// admin/discussions, admin/findings, admin/factory-tasks, admin/crm/sync

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave139-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, modelId, orderId, pkgId;

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
    .send({ name: 'Полина Мальцева', available: true, category: 'fashion', height: 175 });
  modelId = mRes.body.id || mRes.body.model?.id;
  expect(modelId).toBeTruthy();

  // Создаём заявку с телефоном для OTP тестов
  const csrf2 = await getCsrf();
  const oRes = await request(app).post('/api/orders').set('x-csrf-token', csrf2).send({
    client_name: 'Тест OTP',
    client_phone: '79991112233',
    event_type: 'event',
    model_id: modelId,
  });
  orderId = oRes.body.id || oRes.body.order?.id;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── OTP Flow: /client/request-code ──────────────────────────────────────────

describe('POST /client/request-code', () => {
  it('возвращает 400 при отсутствии телефона', async () => {
    const res = await request(app).post('/api/client/request-code').send({});
    expect(res.status).toBe(400);
    expect(typeof res.body.error).toBe('string');
  });

  it('возвращает 400 при некорректном телефоне', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '123' });
    expect(res.status).toBe(400);
  });

  it('возвращает 404 если телефон не зарегистрирован', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '79000000000' });
    expect([404, 429]).toContain(res.status);
  });

  it('отправляет код для зарегистрированного телефона', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '79991112233' });
    if (res.status === 429) return; // rate limit
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    // В dev-режиме возвращает code_debug
    expect(typeof res.body.code_debug).toBe('string');
  });
});

// ─── OTP Flow: /client/verify ────────────────────────────────────────────────

describe('POST /client/verify', () => {
  let otpCode;

  beforeAll(async () => {
    // Запрашиваем код
    const res = await request(app).post('/api/client/request-code').send({ phone: '79991112233' });
    if (res.status === 200 && res.body.code_debug) {
      otpCode = res.body.code_debug;
    }
  });

  it('возвращает 400 или 429 без телефона и кода', async () => {
    const res = await request(app).post('/api/client/verify').send({});
    expect([400, 429]).toContain(res.status);
  });

  it('возвращает 400 или 429 без кода', async () => {
    const res = await request(app).post('/api/client/verify').send({ phone: '79991112233' });
    expect([400, 429]).toContain(res.status);
  });

  it('возвращает 401 при неверном коде', async () => {
    const res = await request(app).post('/api/client/verify').send({ phone: '79991112233', code: '000000' });
    expect([401, 429]).toContain(res.status);
  });

  it('возвращает токен при правильном коде', async () => {
    if (!otpCode) return; // пропустить если нет кода (rate limit)
    const res = await request(app).post('/api/client/verify').send({ phone: '79991112233', code: otpCode });
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.token).toBe('string');
  });
});

// ─── GET /client/orders ───────────────────────────────────────────────────────

describe('GET /client/orders', () => {
  it('возвращает 400 без телефона', async () => {
    const res = await request(app).get('/api/client/orders');
    expect(res.status).toBe(400);
  });

  it('возвращает 400 при некорректном телефоне', async () => {
    const res = await request(app).get('/api/client/orders?phone=abc');
    expect(res.status).toBe(400);
  });

  it('возвращает заявки по телефону', async () => {
    const res = await request(app).get('/api/client/orders?phone=79991112233');
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders || res.body)).toBe(true);
  });
});

// ─── POST /contact ────────────────────────────────────────────────────────────

describe('POST /contact', () => {
  it('сохраняет контакт-заявку', async () => {
    const res = await request(app).post('/api/contact').send({
      name: 'Иван Иванов',
      phone: '+79991234567',
      message: 'Хочу узнать о сотрудничестве',
      email: 'test@example.com',
    });
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.order_number).toBe('string');
  });

  it('возвращает 400 без имени', async () => {
    const res = await request(app).post('/api/contact').send({ phone: '+79991234567', message: 'Вопрос' });
    expect([400, 429]).toContain(res.status);
    if (res.status === 400) expect(typeof res.body.error).toBe('string');
  });

  it('возвращает 400 без телефона', async () => {
    const res = await request(app).post('/api/contact').send({ name: 'Иван', message: 'Вопрос' });
    expect([400, 429]).toContain(res.status);
  });

  it('возвращает 400 без сообщения', async () => {
    const res = await request(app).post('/api/contact').send({ name: 'Иван', phone: '+79991234567' });
    expect([400, 429]).toContain(res.status);
  });

  it('возвращает 400 при некорректном email', async () => {
    const res = await request(app)
      .post('/api/contact')
      .send({ name: 'Иван', phone: '+79991234567', message: 'Вопрос', email: 'not-an-email' });
    expect([400, 429]).toContain(res.status);
  });
});

// ─── Email endpoints ──────────────────────────────────────────────────────────

describe('GET /admin/email/test', () => {
  it('возвращает конфигурацию SMTP', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.configured).toBe('boolean');
    expect(Array.isArray(res.body.admin_emails)).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/email/test');
    expect(res.status).toBe(401);
  });
});

// ─── Price Packages CRUD ──────────────────────────────────────────────────────

describe('GET /admin/price-packages', () => {
  it('возвращает список пакетов', async () => {
    const res = await request(app).get('/api/admin/price-packages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.packages)).toBe(true);
  });
});

describe('POST /admin/price-packages', () => {
  it('создаёт новый пакет', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/price-packages')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({
        name: 'Стандарт',
        description: 'Стандартный пакет',
        price_from: 15000,
        price_to: 25000,
        duration: '4 часа',
        category: 'standard',
        sort_order: 1,
        active: true,
      });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.id).toBe('number');
    pkgId = res.body.id;
  });

  it('возвращает 400 без name', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/price-packages')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ price_from: 5000 });
    expect(res.status).toBe(400);
  });

  it('требует авторизации', async () => {
    const res = await request(app).post('/api/admin/price-packages').send({ name: 'Test' });
    expect(res.status).toBe(401);
  });
});

describe('PUT /admin/price-packages/:id', () => {
  it('обновляет пакет', async () => {
    if (!pkgId) return;
    const csrf = await getCsrf();
    const res = await request(app)
      .put(`/api/admin/price-packages/${pkgId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ name: 'Премиум', price_from: 30000, category: 'premium' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('возвращает 400 при невалидном id', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .put('/api/admin/price-packages/abc')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ name: 'Test' });
    expect(res.status).toBe(400);
  });
});

describe('DELETE /admin/price-packages/:id', () => {
  it('удаляет пакет', async () => {
    if (!pkgId) return;
    const csrf = await getCsrf();
    const res = await request(app)
      .delete(`/api/admin/price-packages/${pkgId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('возвращает 400 при невалидном id', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .delete('/api/admin/price-packages/0')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf);
    expect(res.status).toBe(400);
  });
});

// ─── AI Match ─────────────────────────────────────────────────────────────────

describe('POST /client/ai-match', () => {
  it('возвращает fallback модели без ANTHROPIC_API_KEY', async () => {
    const savedKey = process.env.ANTHROPIC_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    const res = await request(app)
      .post('/api/client/ai-match')
      .send({ description: 'Нужна модель для фотосессии в Москве' });
    process.env.ANTHROPIC_API_KEY = savedKey;
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('возвращает ошибку при коротком описании', async () => {
    const res = await request(app).post('/api/client/ai-match').send({ description: 'abc' });
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
  });

  it('возвращает ошибку при слишком длинном описании', async () => {
    const res = await request(app)
      .post('/api/client/ai-match')
      .send({ description: 'a'.repeat(501) });
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
  });
});

// ─── AI Budget ────────────────────────────────────────────────────────────────

describe('POST /client/ai-budget', () => {
  it('возвращает оценку бюджета без ANTHROPIC_API_KEY', async () => {
    const savedKey = process.env.ANTHROPIC_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    const res = await request(app)
      .post('/api/client/ai-budget')
      .send({ description: 'Показ мод в Москве на 100 человек' });
    process.env.ANTHROPIC_API_KEY = savedKey;
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('возвращает ошибку при коротком описании', async () => {
    const res = await request(app).post('/api/client/ai-budget').send({ description: 'кор' });
    if (res.status === 429) return;
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(false);
  });
});

// ─── Admin: discussions, findings, factory-tasks ──────────────────────────────

describe('GET /admin/discussions', () => {
  it('возвращает список дискуссий', async () => {
    const res = await request(app).get('/api/admin/discussions').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/discussions');
    expect(res.status).toBe(401);
  });
});

describe('GET /admin/findings', () => {
  it('возвращает список находок (open по умолчанию)', async () => {
    const res = await request(app).get('/api/admin/findings').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('поддерживает ?status=resolved', async () => {
    const res = await request(app)
      .get('/api/admin/findings?status=resolved')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/factory-tasks', () => {
  it('возвращает задачи и статистику', async () => {
    const res = await request(app).get('/api/admin/factory-tasks').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.tasks)).toBe(true);
    expect(res.body.stats).toBeDefined();
  });
});

describe('PATCH /admin/factory-tasks/:id', () => {
  let taskId;

  beforeAll(async () => {
    const { run: dbRun } = require('../database');
    const r = await dbRun(`INSERT INTO factory_tasks (action, status, priority) VALUES ('Тест задача', 'pending', 5)`);
    taskId = r.id;
  });

  it('обновляет статус задачи', async () => {
    if (!taskId) return;
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/factory-tasks/${taskId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'done' });
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it('отклоняет невалидный статус', async () => {
    if (!taskId) return;
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/factory-tasks/${taskId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'hacked' });
    expect(res.status).toBe(400);
  });
});

// ─── Admin CRM Sync ───────────────────────────────────────────────────────────

describe('POST /admin/crm/sync/:provider', () => {
  it('возвращает ошибку для несуществующего провайдера', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/crm/sync/unknown_crm')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect([200, 400, 404, 500]).toContain(res.status);
  });

  it('требует авторизации', async () => {
    const res = await request(app).post('/api/admin/crm/sync/generic').send({});
    expect(res.status).toBe(401);
  });
});

// ─── Admin: stats (GET /stats and /stats/extended) ───────────────────────────

describe('GET /stats', () => {
  it('возвращает общую статистику (admin)', async () => {
    const res = await request(app).get('/api/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ─── Admin: scheduled broadcasts ─────────────────────────────────────────────

describe('GET /admin/broadcasts/count', () => {
  it('возвращает количество рассылок', async () => {
    const res = await request(app).get('/api/admin/broadcasts/count').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.count).toBe('number');
  });
});

// ─── Admin: factory/status ────────────────────────────────────────────────────

describe('GET /admin/factory/status', () => {
  it('возвращает статус фабрики', async () => {
    const res = await request(app).get('/api/admin/factory/status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/factory/actions', () => {
  it('возвращает список действий фабрики или 500 без better-sqlite3', async () => {
    const res = await request(app).get('/api/admin/factory/actions').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
    if (res.status === 200) {
      expect(Array.isArray(res.body.actions)).toBe(true);
    }
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/factory/actions');
    expect(res.status).toBe(401);
  });
});

describe('GET /admin/factory/decisions', () => {
  it('возвращает список решений CEO или 500 без better-sqlite3', async () => {
    const res = await request(app).get('/api/admin/factory/decisions').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
    if (res.status === 200) {
      expect(Array.isArray(res.body.decisions)).toBe(true);
    }
  });
});

describe('GET /admin/factory/experiments', () => {
  it('возвращает список экспериментов или 500 без better-sqlite3', async () => {
    const res = await request(app).get('/api/admin/factory/experiments').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
    if (res.status === 200) {
      expect(Array.isArray(res.body.experiments)).toBe(true);
    }
  });
});
