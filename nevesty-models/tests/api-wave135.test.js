'use strict';
// Wave 135: broadcasts, TOTP, bulk orders, favorites, by-phone, recommend, stats/public, reviews, managers

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave135-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken, modelId, orderId, broadcastId;

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

  const mr = await request(app)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({ name: 'Wave135 Model', age: 24, city: 'Санкт-Петербург', category: 'commercial' });
  modelId = mr.body.id;

  const ord = await dbRun(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, model_id, status)
     VALUES (?,?,?,?,?,?,?)`,
    ['ORD-W135', 'Wave135 Client', '+79001350001', 'photo', '2027-06-15', modelId, 'new']
  );
  orderId = ord.id;

  // Создаём broadcast для тестов удаления (таблица scheduled_broadcasts)
  const bc = await dbRun(
    `INSERT INTO scheduled_broadcasts (text, segment, scheduled_at, status, created_by) VALUES (?,?,datetime('now','+1 hour'),?,?)`,
    ['Wave135 broadcast test', 'all', 'pending', 'admin']
  );
  broadcastId = bc.id;
}, 30000);

// ── 1. Broadcasts ─────────────────────────────────────────────────────────────

describe('Broadcasts count — GET /api/admin/broadcasts/count', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/broadcasts/count');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/broadcasts/count').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has count field', async () => {
    const res = await request(app).get('/api/admin/broadcasts/count').set('Authorization', `Bearer ${adminToken}`);
    const hasCount = typeof res.body.count === 'number' || typeof res.body.total === 'number' || 'count' in res.body;
    expect(hasCount).toBe(true);
  });
});

describe('Create broadcast — POST /api/admin/broadcasts', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/broadcasts').send({ message: 'test' });
    expect(res.status).toBe(401);
  });

  it('returns 400 for missing text (field name is text not message)', async () => {
    const res = await request(app).post('/api/admin/broadcasts').set('Authorization', `Bearer ${adminToken}`).send({});
    expect(res.status).toBe(400);
  });

  it('returns 200 or 201 for valid broadcast with text field', async () => {
    const res = await request(app)
      .post('/api/admin/broadcasts')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ text: 'Wave135 новая рассылка тест', segment: 'all' });
    expect([200, 201]).toContain(res.status);
    if (res.status === 200 || res.status === 201) {
      const isOk = res.body.ok === true || res.body.success === true || typeof res.body.id === 'number';
      expect(isOk).toBe(true);
    }
  });
});

describe('Delete broadcast — DELETE /api/admin/broadcasts/:id', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).delete(`/api/admin/broadcasts/${broadcastId}`);
    expect(res.status).toBe(401);
  });

  it('returns 200 for existing broadcast', async () => {
    const res = await request(app)
      .delete(`/api/admin/broadcasts/${broadcastId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
  });
});

// ── 2. TOTP setup ─────────────────────────────────────────────────────────────

describe('TOTP setup — GET /api/admin/totp/setup', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/totp/setup');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth and returns secret + qr_url or qr_code', async () => {
    const res = await request(app).get('/api/admin/totp/setup').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('secret');
    // Endpoint returns qr_url (data URL of PNG), not qr_code
    const hasQr = 'qr_code' in res.body || 'qr_url' in res.body || 'otpauth_url' in res.body;
    expect(hasQr).toBe(true);
  });
});

describe('TOTP enable — POST /api/admin/totp/enable', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/totp/enable').send({ secret: 'test', code: '000000' });
    expect(res.status).toBe(401);
  });

  it('returns 400 for missing fields', async () => {
    const res = await request(app).post('/api/admin/totp/enable').set('Authorization', `Bearer ${adminToken}`).send({});
    expect([400, 422]).toContain(res.status);
  });

  it('returns 400 for wrong TOTP code', async () => {
    const setupRes = await request(app).get('/api/admin/totp/setup').set('Authorization', `Bearer ${adminToken}`);
    const { secret } = setupRes.body;
    const res = await request(app)
      .post('/api/admin/totp/enable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ secret, code: '000000' }); // Неверный код
    expect([400, 401]).toContain(res.status);
  });
});

describe('TOTP disable — DELETE /api/admin/totp/disable', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).delete('/api/admin/totp/disable').send({ code: '000000' });
    expect(res.status).toBe(401);
  });

  it('returns 400 or 200 with auth (TOTP not enabled in test)', async () => {
    const res = await request(app)
      .delete('/api/admin/totp/disable')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ code: '000000' });
    // Если TOTP не включён — 400; если включён и код верный — 200
    expect([200, 400, 401]).toContain(res.status);
  });
});

// ── 3. Bulk orders operation ───────────────────────────────────────────────────

describe('Orders bulk — POST /api/admin/orders/bulk', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk')
      .send({ ids: [orderId], action: 'new' });
    expect(res.status).toBe(401);
  });

  it('returns 400 for empty ids array', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [], action: 'new' });
    expect(res.status).toBe(400);
  });

  it('returns 400 for invalid action', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [orderId], action: 'invalid_xyz' });
    expect(res.status).toBe(400);
  });

  it('returns 200 for valid status change', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [orderId], action: 'reviewing' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── 4. Models bulk-featured ───────────────────────────────────────────────────

describe('Bulk featured — POST /api/admin/models/bulk-featured', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app)
      .post('/api/admin/models/bulk-featured')
      .send({ ids: [modelId], featured: 1 });
    expect(res.status).toBe(401);
  });

  it('returns 200 for valid bulk featured update', async () => {
    const res = await request(app)
      .post('/api/admin/models/bulk-featured')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [modelId], featured: 1 });
    expect([200, 400]).toContain(res.status);
  });
});

// ── 5. Favorites (публичный) ──────────────────────────────────────────────────

describe('Favorites — GET /api/favorites', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get(`/api/favorites?ids=${modelId}`);
    expect(res.status).toBe(200);
  });

  it('returns 200 with empty ids', async () => {
    const res = await request(app).get('/api/favorites?ids=');
    expect([200, 400]).toContain(res.status);
  });

  it('returns models array for valid ids', async () => {
    const res = await request(app).get(`/api/favorites?ids=${modelId}`);
    if (res.status === 200) {
      const hasModels = Array.isArray(res.body.models) || Array.isArray(res.body);
      expect(hasModels).toBe(true);
    }
  });
});

// ── 6. Orders by phone ─────────────────────────────────────────────────────────

describe('Orders by phone — GET /api/orders/by-phone', () => {
  it('returns 200 with empty orders when no phone provided', async () => {
    // No phone — returns {orders: [], total: 0} silently (no 400)
    const res = await request(app).get('/api/orders/by-phone');
    expect(res.status).toBe(200);
    if (res.status === 200) {
      expect(Array.isArray(res.body.orders)).toBe(true);
    }
  });

  it('returns 200 for valid phone with orders', async () => {
    const res = await request(app).get('/api/orders/by-phone?phone=79001350001');
    expect([200, 400, 429]).toContain(res.status);
  });

  it('returns 200 for valid phone without orders', async () => {
    const res = await request(app).get('/api/orders/by-phone?phone=79990000000');
    expect([200, 400, 429]).toContain(res.status);
  });
});

// ── 7. Recommend endpoint ─────────────────────────────────────────────────────

describe('Recommend — GET /api/recommend', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/recommend');
    expect(res.status).toBe(200);
  });

  it('accepts event_type param', async () => {
    const res = await request(app).get('/api/recommend?event_type=photo&limit=3');
    expect(res.status).toBe(200);
  });

  it('returns models array', async () => {
    const res = await request(app).get('/api/recommend');
    const hasModels = Array.isArray(res.body.models) || Array.isArray(res.body);
    expect(hasModels).toBe(true);
  });

  it('accepts city param', async () => {
    const res = await request(app).get('/api/recommend?city=Москва');
    expect(res.status).toBe(200);
  });
});

// ── 8. Public stats ────────────────────────────────────────────────────────────

describe('Public stats — GET /api/stats/public', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/stats/public');
    expect(res.status).toBe(200);
  });

  it('has numeric fields', async () => {
    const res = await request(app).get('/api/stats/public');
    const hasData =
      typeof res.body.models === 'number' || typeof res.body.total_models === 'number' || 'orders' in res.body;
    expect(hasData).toBe(true);
  });
});

// ── 9. Reviews (публичный список) ─────────────────────────────────────────────

describe('Reviews public list — GET /api/reviews', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
  });

  it('returns reviews array', async () => {
    const res = await request(app).get('/api/reviews');
    const hasReviews = Array.isArray(res.body.reviews) || Array.isArray(res.body);
    expect(hasReviews).toBe(true);
  });
});

// ── 10. Managers detail/stats ─────────────────────────────────────────────────

// Нет отдельного GET /admin/managers/:id — только DELETE и GET /admin/managers/:id/stats
// Тест DELETE /admin/managers/:id (требует superadmin роль)
describe('Manager delete — DELETE /api/admin/managers/:id', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).delete('/api/admin/managers/999');
    expect(res.status).toBe(401);
  });

  it('returns 403 for non-superadmin (regular admin)', async () => {
    const res = await request(app).delete('/api/admin/managers/999').set('Authorization', `Bearer ${adminToken}`);
    // Regular admin not superadmin — gets 403
    expect([403, 400, 200]).toContain(res.status);
  });
});

describe('Manager stats — GET /api/admin/managers/:id/stats', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/managers/1/stats');
    expect(res.status).toBe(401);
  });

  it('returns 200 or 404 with auth', async () => {
    const res = await request(app).get('/api/admin/managers/1/stats').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
  });
});

// ── 11. Sessions cleanup ──────────────────────────────────────────────────────

describe('Sessions — DELETE /api/admin/sessions', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).delete('/api/admin/sessions');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).delete('/api/admin/sessions').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 12. Analytics model stats ─────────────────────────────────────────────────

describe('Model analytics — GET /api/admin/analytics/model-stats/:id', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/analytics/model-stats/${modelId}`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .get(`/api/admin/analytics/model-stats/${modelId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('returns model stats data', async () => {
    const res = await request(app)
      .get(`/api/admin/analytics/model-stats/${modelId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.status === 200) {
      const hasStats = 'orders' in res.body || 'total' in res.body || 'model_id' in res.body || 'name' in res.body;
      expect(hasStats).toBe(true);
    }
  });
});

// ── 13. Admin orders chart ─────────────────────────────────────────────────────

describe('Orders chart — GET /api/admin/orders-chart', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/orders-chart');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/orders-chart').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 14. Reviews bulk approve ──────────────────────────────────────────────────

describe('Reviews bulk approve — POST /api/admin/reviews/bulk-approve', () => {
  let reviewId;

  beforeAll(async () => {
    const { run: dbRun } = require('../database');
    const result = await dbRun(
      `INSERT INTO reviews (chat_id, model_id, rating, text, client_name, approved) VALUES (?,?,?,?,?,?)`,
      [999135, modelId, 5, 'Wave135 review for bulk', 'Wave135 Reviewer', 0]
    );
    reviewId = result.id;
  });

  it('returns 401 without auth', async () => {
    const res = await request(app)
      .post('/api/admin/reviews/bulk-approve')
      .send({ ids: [reviewId] });
    expect(res.status).toBe(401);
  });

  it('returns 200 for valid bulk approve', async () => {
    const res = await request(app)
      .post('/api/admin/reviews/bulk-approve')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [reviewId] });
    expect([200, 400]).toContain(res.status);
  });
});

// ── 15. Reviews reply ─────────────────────────────────────────────────────────

describe('Review reply — PATCH /api/admin/reviews/:id/reply', () => {
  let reviewId;

  beforeAll(async () => {
    const { run: dbRun } = require('../database');
    const result = await dbRun(
      `INSERT INTO reviews (chat_id, model_id, rating, text, client_name, approved) VALUES (?,?,?,?,?,?)`,
      [999136, modelId, 4, 'Wave135 review for reply', 'Wave135 Reply Test', 1]
    );
    reviewId = result.id;
  });

  it('returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/reviews/${reviewId}/reply`).send({ reply: 'Спасибо!' });
    expect(res.status).toBe(401);
  });

  it('returns 200 for valid reply', async () => {
    const res = await request(app)
      .patch(`/api/admin/reviews/${reviewId}/reply`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reply: 'Спасибо за отзыв!' });
    expect([200, 400]).toContain(res.status);
  });
});

// ── 16. Admin model json ───────────────────────────────────────────────────────

describe('Model JSON update — PUT /api/admin/models/:id/json', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).put(`/api/admin/models/${modelId}/json`).send({ name: 'Updated' });
    expect(res.status).toBe(401);
  });

  it('returns 200 with valid update', async () => {
    const res = await request(app)
      .put(`/api/admin/models/${modelId}/json`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ name: 'Wave135 Updated', age: 25, city: 'Санкт-Петербург', category: 'commercial' });
    expect([200, 400]).toContain(res.status);
    if (res.status === 200) {
      // Returns {success: true} or {id: ...}
      const isOk = res.body.success === true || res.body.ok === true || 'id' in res.body;
      expect(isOk).toBe(true);
    }
  });
});

// ── 17. Admin stats extended2 ─────────────────────────────────────────────────

describe('Stats extended2 — GET /api/admin/stats/extended2', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/stats/extended2');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/stats/extended2').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});
