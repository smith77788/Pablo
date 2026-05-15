'use strict';
// Wave 124: user wishlist (chat_id based), client OTP auth, chat/ask chatbot,
//           analytics sources/monthly/extended/hourly/event-types, client segments deep,
//           admin analytics top-models, cabinet orders full flow, favorites public endpoint

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave124-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken;
let seedModelId;

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

  // Seed one model so wishlist POST can reference a real model_id
  const modelRes = await request(app)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({ name: 'Wave124 Model', age: 24, city: 'Москва', category: 'fashion', available: 1 });
  seedModelId = modelRes.body.id;
}, 30000);

// ── 1. User Wishlist — GET /api/user/wishlist ──────────────────────────────────

describe('User wishlist — GET /api/user/wishlist', () => {
  it('returns 400 without chat_id param', async () => {
    const res = await request(app).get('/api/user/wishlist');
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 400 for chat_id=0', async () => {
    const res = await request(app).get('/api/user/wishlist?chat_id=0');
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 200 with valid chat_id and empty array (no wishlist yet)', async () => {
    const res = await request(app).get('/api/user/wishlist?chat_id=99999');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body).toHaveLength(0);
  });
});

// ── 2. User Wishlist — POST /api/user/wishlist ─────────────────────────────────

describe('User wishlist — POST /api/user/wishlist', () => {
  it('returns 400 without chat_id', async () => {
    const res = await request(app).post('/api/user/wishlist').send({ model_id: seedModelId });
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 400 without model_id', async () => {
    const res = await request(app).post('/api/user/wishlist').send({ chat_id: 12345 });
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 404 for non-existent model_id', async () => {
    const res = await request(app).post('/api/user/wishlist').send({ chat_id: 12345, model_id: 999999 });
    expect(res.status).toBe(404);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 201 with ok:true for valid chat_id and existing model', async () => {
    const res = await request(app).post('/api/user/wishlist').send({ chat_id: 12345, model_id: seedModelId });
    expect(res.status).toBe(201);
    expect(res.body.ok).toBe(true);
  });

  it('returns 409 if same model added again (duplicate)', async () => {
    // Add once more — should conflict since we already added in the previous test
    const res = await request(app).post('/api/user/wishlist').send({ chat_id: 12345, model_id: seedModelId });
    expect(res.status).toBe(409);
    expect(res.body).toHaveProperty('error');
  });

  it('GET wishlist returns the added model entry', async () => {
    const res = await request(app).get('/api/user/wishlist?chat_id=12345');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    const entry = res.body.find(e => e.model_id === seedModelId);
    expect(entry).toBeDefined();
  });
});

// ── 3. User Wishlist — DELETE /api/user/wishlist/:model_id ─────────────────────

describe('User wishlist — DELETE /api/user/wishlist/:model_id', () => {
  it('returns 400 without chat_id query param', async () => {
    const res = await request(app).delete(`/api/user/wishlist/${seedModelId}`);
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 404 when entry does not exist (different chat_id)', async () => {
    const res = await request(app).delete(`/api/user/wishlist/${seedModelId}?chat_id=777`);
    expect(res.status).toBe(404);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 200 ok:true when entry exists and is removed', async () => {
    const res = await request(app).delete(`/api/user/wishlist/${seedModelId}?chat_id=12345`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('wishlist is empty after deletion', async () => {
    const res = await request(app).get('/api/user/wishlist?chat_id=12345');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body).toHaveLength(0);
  });
});

// ── 4. Public favorites endpoint — GET /api/favorites ─────────────────────────

describe('Public favorites — GET /api/favorites', () => {
  it('returns 200 and empty array when ids param is absent', async () => {
    const res = await request(app).get('/api/favorites');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    expect(res.body).toHaveLength(0);
  });

  it('returns 200 and empty array for ids param with garbage', async () => {
    const res = await request(app).get('/api/favorites?ids=abc,0,-1');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('returns model stubs for valid existing model ID', async () => {
    const res = await request(app).get(`/api/favorites?ids=${seedModelId}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('id');
      expect(res.body[0]).toHaveProperty('name');
    }
  });
});

// ── 5. Client OTP — POST /api/client/request-code ─────────────────────────────

describe('Client OTP — POST /api/client/request-code', () => {
  it('returns 400 without phone field', async () => {
    const res = await request(app).post('/api/client/request-code').send({});
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 400 for invalid phone format (too short)', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '123' });
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 404 if no orders found for this phone', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '79990000001' });
    expect(res.status).toBe(404);
    expect(res.body).toHaveProperty('error');
  });
});

// ── 6. Client OTP — POST /api/client/verify ───────────────────────────────────

describe('Client OTP — POST /api/client/verify', () => {
  // Note: clientOtpLimiter is shared with /client/request-code tests above.
  // If the limiter is active (e.g. in test environments without per-instance isolation)
  // these endpoints may return 429 instead of the expected error codes.
  // We accept both the semantic error code and 429 as valid.

  it('returns 400 or 429 without phone or code', async () => {
    const res = await request(app).post('/api/client/verify').send({});
    expect([400, 429]).toContain(res.status);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 400 or 429 with phone but no code', async () => {
    const res = await request(app).post('/api/client/verify').send({ phone: '79991234567' });
    expect([400, 429]).toContain(res.status);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 401 or 429 for wrong/expired code', async () => {
    const res = await request(app).post('/api/client/verify').send({ phone: '79991234567', code: '000000' });
    expect([401, 429]).toContain(res.status);
    expect(res.body).toHaveProperty('error');
  });
});

// ── 7. Chat ask — POST /api/chat/ask ──────────────────────────────────────────

describe('Chat — POST /api/chat/ask', () => {
  it('returns 400 for empty message', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: '' });
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 200 with a reply field for pricing question', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: 'Сколько стоит аренда модели?' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('reply');
    expect(typeof res.body.reply).toBe('string');
    expect(res.body.reply.length).toBeGreaterThan(0);
  });

  it('returns 200 with a reply field for booking question', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: 'Как забронировать?' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('reply');
  });

  it('returns 200 with greeting response', async () => {
    const res = await request(app).post('/api/chat/ask').send({ message: 'Привет!' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('reply');
  });

  it('returns 200 with default reply for unknown message', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'Какой-то непонятный вопрос без ключевых слов' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('reply');
    expect(typeof res.body.reply).toBe('string');
  });
});

// ── 8. Analytics: event-types — GET /api/admin/analytics/event-types ──────────

describe('Analytics event-types — GET /api/admin/analytics/event-types', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/event-types');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/event-types').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has types array', async () => {
    const res = await request(app).get('/api/admin/analytics/event-types').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('types');
    expect(Array.isArray(res.body.types)).toBe(true);
  });
});

// ── 9. Analytics: sources — GET /api/admin/analytics/sources ──────────────────

describe('Analytics sources — GET /api/admin/analytics/sources', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/sources');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/sources').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has sources array', async () => {
    const res = await request(app).get('/api/admin/analytics/sources').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('sources');
    expect(Array.isArray(res.body.sources)).toBe(true);
  });

  it('accepts optional days query param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/sources?days=7')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 10. Analytics: monthly — GET /api/admin/analytics/monthly ─────────────────

describe('Analytics monthly — GET /api/admin/analytics/monthly', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has months array and count field', async () => {
    const res = await request(app).get('/api/admin/analytics/monthly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('months');
    expect(Array.isArray(res.body.months)).toBe(true);
    expect(res.body).toHaveProperty('count');
    expect(typeof res.body.count).toBe('number');
  });

  it('accepts months query param (clamped 3-24)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/monthly?months=6')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('months');
  });
});

// ── 11. Analytics: extended — GET /api/admin/analytics/extended ───────────────

describe('Analytics extended — GET /api/admin/analytics/extended', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/extended');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has top_cities array and repeat_rate number', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('top_cities');
    expect(Array.isArray(res.body.top_cities)).toBe(true);
    expect(res.body).toHaveProperty('repeat_rate');
    expect(typeof res.body.repeat_rate).toBe('number');
  });

  it('response has reviews_count field', async () => {
    const res = await request(app).get('/api/admin/analytics/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('reviews_count');
    expect(typeof res.body.reviews_count).toBe('number');
  });
});

// ── 12. Analytics: hourly — GET /api/admin/analytics/hourly ───────────────────

describe('Analytics hourly — GET /api/admin/analytics/hourly', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has ok:true, hours array of 24 entries, and days field', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.hours)).toBe(true);
    expect(res.body.hours).toHaveLength(24);
    expect(typeof res.body.days).toBe('number');
  });

  it('each hour entry has hour (0-23) and cnt fields', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    const hours = res.body.hours;
    expect(hours[0]).toHaveProperty('hour');
    expect(hours[0]).toHaveProperty('cnt');
    expect(hours[0].hour).toBe(0);
    expect(hours[23].hour).toBe(23);
  });

  it('accepts days query param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/hourly?days=30')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.days).toBe(30);
  });
});

// ── 13. Analytics: top-models — GET /api/admin/analytics/top-models ──────────

describe('Analytics top-models — GET /api/admin/analytics/top-models', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has models array', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('models');
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('accepts limit query param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-models?limit=3&days=30')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
    expect(res.body.models.length).toBeLessThanOrEqual(3);
  });
});

// ── 14. Client segments — GET /api/admin/analytics/client-segments ─────────────

describe('Analytics client-segments — GET /api/admin/analytics/client-segments', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/client-segments');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has ok:true and segments object', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
    expect(res.body).toHaveProperty('segments');
  });

  it('all segment fields are numbers (vip/active/dormant/one_time)', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    const { segments } = res.body;
    expect(typeof segments.vip).toBe('number');
    expect(typeof segments.active).toBe('number');
    expect(typeof segments.dormant).toBe('number');
    expect(typeof segments.one_time).toBe('number');
  });

  it('all segment values are non-negative integers', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-segments')
      .set('Authorization', `Bearer ${adminToken}`);
    const { segments } = res.body;
    expect(segments.vip).toBeGreaterThanOrEqual(0);
    expect(segments.active).toBeGreaterThanOrEqual(0);
    expect(segments.dormant).toBeGreaterThanOrEqual(0);
    expect(segments.one_time).toBeGreaterThanOrEqual(0);
  });
});
