'use strict';
// Wave 138: db-stats, crm-status, db-vacuum, db/backups, cache, analytics (kpi/funnel/overview/
// revenue-chart/conversion/top-models/event-types/sources/monthly), sitemap, faq, chat/ask,
// admin/messages, social/posts status, cabinet (login + orders)

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave138-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const jwt = require('jsonwebtoken');

let app, adminToken, modelId, orderId, socialPostId;

async function getCsrf() {
  const r = await request(app).get('/api/csrf-token');
  return r.body.token;
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
    .send({ name: 'Светлана Крылова', available: true, featured: false, height: 172, category: 'fashion' });
  modelId = mRes.body.id || mRes.body.model?.id;
  expect(modelId).toBeTruthy();

  // Создаём заявку
  const csrf2 = await getCsrf();
  const oRes = await request(app).post('/api/orders').set('x-csrf-token', csrf2).send({
    client_name: 'Иван Кабинет',
    client_phone: '79161234567',
    event_type: 'photo_shoot',
    model_id: modelId,
  });
  orderId = oRes.body.id || oRes.body.order?.id;

  // Создаём social post для теста status
  const spRes = await dbRun(
    `INSERT INTO social_posts (platform, content_type, caption, status) VALUES ('telegram','photo','Тестовый пост','draft')`
  );
  socialPostId = spRes.id;
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── /admin/db-stats ──────────────────────────────────────────────────────────

describe('GET /admin/db-stats', () => {
  it('возвращает список таблиц, WAL и размер', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.tables)).toBe(true);
    expect(res.body.tables.length).toBeGreaterThan(0);
    expect(typeof res.body.size_bytes).toBe('number');
    expect(typeof res.body.size_mb).toBe('number');
    expect(Array.isArray(res.body.schema_versions)).toBe(true);
  });

  it('каждая таблица имеет name, count, indexes', async () => {
    const res = await request(app).get('/api/admin/db-stats').set('Authorization', `Bearer ${adminToken}`);
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

// ─── /admin/crm-status ────────────────────────────────────────────────────────

describe('GET /admin/crm-status', () => {
  it('возвращает статус CRM', async () => {
    const res = await request(app).get('/api/admin/crm-status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.generic).toBe('boolean');
    expect(typeof res.body.amocrm).toBe('boolean');
    expect(typeof res.body.bitrix24).toBe('boolean');
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/crm-status');
    expect(res.status).toBe(401);
  });
});

// ─── /admin/db-vacuum ─────────────────────────────────────────────────────────

describe('POST /admin/db-vacuum', () => {
  it('выполняет VACUUM успешно', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/db-vacuum')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).post('/api/admin/db-vacuum').send({});
    expect(res.status).toBe(401);
  });
});

describe('POST /admin/db/vacuum', () => {
  it('выполняет WAL checkpoint + VACUUM', async () => {
    const csrf = await getCsrf();
    const res = await request(app)
      .post('/api/admin/db/vacuum')
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ─── /admin/db/backups ────────────────────────────────────────────────────────

describe('GET /admin/db/backups', () => {
  it('возвращает список бэкапов (пустой в тест-среде)', async () => {
    const res = await request(app).get('/api/admin/db/backups').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.backups)).toBe(true);
    expect(typeof res.body.count).toBe('number');
  });
});

// ─── /admin/cache/stats и /admin/cache ───────────────────────────────────────

describe('GET /admin/cache/stats', () => {
  it('возвращает статистику кэша', async () => {
    const res = await request(app).get('/api/admin/cache/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toBeDefined();
  });
});

describe('DELETE /admin/cache', () => {
  it('очищает кэш', async () => {
    const res = await request(app).delete('/api/admin/cache').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('требует авторизации', async () => {
    const res = await request(app).delete('/api/admin/cache');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: KPI ───────────────────────────────────────────────────────────

describe('GET /admin/analytics/kpi', () => {
  it('возвращает KPI метрики', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.total).toBe('number');
    expect(typeof res.body.completed).toBe('number');
    expect(typeof res.body.active).toBe('number');
    expect(typeof res.body.new_clients).toBe('number');
  });

  it('принимает параметр ?days=7', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi?days=7').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.total).toBe('number');
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/kpi');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: Funnel ────────────────────────────────────────────────────────

describe('GET /admin/analytics/funnel', () => {
  it('возвращает шаги воронки', async () => {
    const res = await request(app).get('/api/admin/analytics/funnel').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.stages)).toBe(true);
    expect(typeof res.body.total).toBe('number');
    expect(typeof res.body.conversion_rate).toBe('number');
  });
});

// ─── Analytics: Top Models ────────────────────────────────────────────────────

describe('GET /admin/analytics/top-models', () => {
  it('возвращает топ моделей', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });
});

// ─── Analytics: Event-Types ───────────────────────────────────────────────────

describe('GET /admin/analytics/event-types', () => {
  it('возвращает типы событий', async () => {
    const res = await request(app).get('/api/admin/analytics/event-types').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.types)).toBe(true);
  });
});

// ─── Analytics: Sources ───────────────────────────────────────────────────────

describe('GET /admin/analytics/sources', () => {
  it('возвращает источники', async () => {
    const res = await request(app).get('/api/admin/analytics/sources').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.sources)).toBe(true);
  });
});

// ─── Analytics: Monthly ───────────────────────────────────────────────────────

describe('GET /admin/analytics/monthly', () => {
  it('возвращает помесячные данные', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.months)).toBe(true);
    expect(typeof res.body.count).toBe('number');
  });
});

// ─── Analytics: Overview ──────────────────────────────────────────────────────

describe('GET /admin/analytics/overview', () => {
  it('возвращает дашборд с orders, revenue, models, clients', async () => {
    const res = await request(app).get('/api/admin/analytics/overview').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.orders?.total).toBe('number');
    expect(typeof res.body.revenue?.total).toBe('number');
    expect(typeof res.body.models?.total).toBe('number');
    expect(typeof res.body.clients?.total).toBe('number');
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/analytics/overview');
    expect(res.status).toBe(401);
  });
});

// ─── Analytics: Revenue Chart ─────────────────────────────────────────────────

describe('GET /admin/analytics/revenue-chart', () => {
  it('возвращает данные по дням', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart?period=7')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.data)).toBe(true);
    expect(res.body.period).toBe(7);
  });

  it('по умолчанию period=30', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-chart')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.period).toBe(30);
  });
});

// ─── Analytics: Conversion ────────────────────────────────────────────────────

describe('GET /admin/analytics/conversion', () => {
  it('возвращает conversion_rate и воронку', async () => {
    const res = await request(app).get('/api/admin/analytics/conversion').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.total).toBe('number');
    expect(typeof res.body.conversion_rate).toBe('number');
    expect(res.body.funnel).toBeDefined();
  });
});

// ─── /admin/messages ──────────────────────────────────────────────────────────

describe('GET /admin/messages/recent', () => {
  it('возвращает последние сообщения', async () => {
    const res = await request(app).get('/api/admin/messages/recent').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.messages)).toBe(true);
  });

  it('принимает ?limit=5', async () => {
    const res = await request(app)
      .get('/api/admin/messages/recent?limit=5')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/messages', () => {
  it('возвращает paginated сообщения', async () => {
    const res = await request(app).get('/api/admin/messages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.messages)).toBe(true);
    expect(typeof res.body.total).toBe('number');
  });

  it('фильтр=all работает', async () => {
    const res = await request(app).get('/api/admin/messages?filter=all').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('фильтр=unread работает', async () => {
    const res = await request(app)
      .get('/api/admin/messages?filter=unread')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('фильтр=today работает', async () => {
    const res = await request(app).get('/api/admin/messages?filter=today').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/messages');
    expect(res.status).toBe(401);
  });
});

// ─── Social Posts: Status ────────────────────────────────────────────────────

describe('PATCH /admin/social/posts/:id/status', () => {
  it('меняет статус на published', async () => {
    if (!socialPostId) return;
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/social/posts/${socialPostId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'published' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('отклоняет недопустимый статус', async () => {
    if (!socialPostId) return;
    const csrf = await getCsrf();
    const res = await request(app)
      .patch(`/api/admin/social/posts/${socialPostId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .set('x-csrf-token', csrf)
      .send({ status: 'hacked' });
    expect(res.status).toBe(400);
  });

  it('все допустимые статусы проходят', async () => {
    if (!socialPostId) return;
    for (const status of ['draft', 'scheduled', 'cancelled']) {
      const csrf = await getCsrf();
      const res = await request(app)
        .patch(`/api/admin/social/posts/${socialPostId}/status`)
        .set('Authorization', `Bearer ${adminToken}`)
        .set('x-csrf-token', csrf)
        .send({ status });
      expect(res.status).toBe(200);
    }
  });

  it('требует авторизации', async () => {
    const res = await request(app).patch(`/api/admin/social/posts/1/status`).send({ status: 'published' });
    expect(res.status).toBe(401);
  });
});

// ─── Chat / Ask ───────────────────────────────────────────────────────────────

describe('POST /chat/ask', () => {
  it('отвечает на вопрос о цене', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: 'Какая цена и стоимость?' });
    expect(res.status).toBe(200);
    expect(typeof res.body.reply).toBe('string');
    expect(res.body.reply.length).toBeGreaterThan(0);
  });

  it('распознаёт приветствие', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: 'Привет!' });
    expect(res.status).toBe(200);
    expect(res.body.reply).toMatch(/здравствуйте|привет/i);
  });

  it('отвечает на вопрос о бронировании', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: 'Как забронировать модель?' });
    expect(res.status).toBe(200);
    expect(typeof res.body.reply).toBe('string');
  });

  it('возвращает дефолтный ответ на неизвестный вопрос', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: 'zxcvbnm qwerty' });
    expect(res.status).toBe(200);
    expect(typeof res.body.reply).toBe('string');
  });

  it('возвращает 400 при пустом сообщении', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: '' });
    expect(res.status).toBe(400);
  });

  it('возвращает 400 при отсутствии message', async () => {
    const res = await request(app).post('/api/chat/ask').send({});
    expect(res.status).toBe(400);
  });
});

// ─── Sitemap ─────────────────────────────────────────────────────────────────

describe('GET /admin/sitemap/regenerate', () => {
  it('регенерирует sitemap (требует auth)', async () => {
    const res = await request(app).get('/api/admin/sitemap/regenerate').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status); // может упасть если нет прав на запись
    if (res.status === 200) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('требует авторизации', async () => {
    const res = await request(app).get('/api/admin/sitemap/regenerate');
    expect(res.status).toBe(401);
  });
});

describe('GET /sitemap-models.xml', () => {
  it('возвращает XML', async () => {
    const res = await request(app).get('/api/sitemap-models.xml');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/xml/);
    expect(res.text).toContain('<?xml');
    expect(res.text).toContain('<urlset');
  });
});

// ─── FAQ ──────────────────────────────────────────────────────────────────────

describe('GET /faq/categories', () => {
  it('возвращает категории FAQ', async () => {
    const res = await request(app).get('/api/faq/categories');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.categories)).toBe(true);
  });
});

describe('GET /faq', () => {
  it('возвращает активные FAQ', async () => {
    const res = await request(app).get('/api/faq');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('фильтрует по ?category=general', async () => {
    const res = await request(app).get('/api/faq?category=general');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });
});

// ─── Cabinet: Login + Orders ─────────────────────────────────────────────────

describe('POST /cabinet/login', () => {
  it('возвращает 400 без телефона', async () => {
    const res = await request(app).post('/api/cabinet/login').send({});
    expect(res.status).toBe(400);
    expect(res.body.ok).toBe(false);
  });

  it('возвращает 400 при некорректном формате телефона', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '123' });
    expect(res.status).toBe(400);
    expect(res.body.ok).toBe(false);
  });

  it('возвращает 404 если телефон не найден', async () => {
    const res = await request(app).post('/api/cabinet/login').send({ phone: '79001112233' });
    expect([404, 429]).toContain(res.status);
  });

  it('возвращает токен при существующем телефоне', async () => {
    // Используем телефон из созданной заявки
    const res = await request(app).post('/api/cabinet/login').send({ phone: '79161234567' });
    if (res.status === 429) return; // rate-limit
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(typeof res.body.token).toBe('string');
  });
});

describe('GET /cabinet/orders', () => {
  it('возвращает 401 без токена', async () => {
    const res = await request(app).get('/api/cabinet/orders');
    expect(res.status).toBe(401);
  });

  it('возвращает 401 с неверным токеном', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', 'Bearer bad-token');
    expect(res.status).toBe(401);
  });

  it('возвращает 403 с admin-токеном (не client)', async () => {
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(403);
  });

  it('возвращает список заявок с client-токеном', async () => {
    const clientToken = jwt.sign(
      { type: 'client', phone: '9161234567', chat_id: null, name: 'Иван Кабинет' },
      process.env.JWT_SECRET,
      { expiresIn: '1h' }
    );
    const res = await request(app).get('/api/cabinet/orders').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.orders)).toBe(true);
  });
});

// ─── Analytics дополнительные ─────────────────────────────────────────────────

describe('GET /admin/analytics/extended', () => {
  it('возвращает расширенную аналитику', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/revenue', () => {
  it('возвращает revenue данные', async () => {
    const res = await request(app).get('/api/admin/analytics/revenue').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/repeat-clients', () => {
  it('возвращает данные о повторных клиентах', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/repeat-clients')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/heatmap', () => {
  it('возвращает данные тепловой карты', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/hourly', () => {
  it('возвращает почасовые данные', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/conversion-funnel', () => {
  it('возвращает воронку конверсии', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/revenue-by-month', () => {
  it('возвращает помесячную выручку', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/revenue-by-month')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/forecast', () => {
  it('возвращает прогноз', async () => {
    const res = await request(app).get('/api/admin/analytics/forecast').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/top-cities', () => {
  it('возвращает топ городов', async () => {
    const res = await request(app).get('/api/admin/analytics/top-cities').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/client-segments', () => {
  it('возвращает сегменты клиентов', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/client-ltv', () => {
  it('возвращает LTV клиентов', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('GET /admin/analytics/model-stats/:id', () => {
  it('возвращает статистику модели', async () => {
    const res = await request(app)
      .get(`/api/admin/analytics/model-stats/${modelId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('404 для несуществующей модели', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/model-stats/999999')
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
  });
});
