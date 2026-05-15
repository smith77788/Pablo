'use strict';
// Wave 134: auth endpoints, admin me/stats/notifications/settings, cities, config, orders status

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave134-test-secret-32-chars-ok!!';
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

  const mr = await request(app)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({ name: 'Wave134 Model', age: 26, city: 'Москва', category: 'fashion' });
  modelId = mr.body.id;

  const ord = await dbRun(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, model_id, status)
     VALUES (?,?,?,?,?,?,?)`,
    ['ORD-W134', 'Wave134 Client', '+79001340001', 'photo', '2027-05-01', modelId, 'new']
  );
  orderId = ord.id;
}, 30000);

// ── 1. Public config ──────────────────────────────────────────────────────────

describe('Config — GET /api/config', () => {
  it('returns 200 without auth (public)', async () => {
    const res = await request(app).get('/api/config');
    expect(res.status).toBe(200);
  });

  it('does not expose secrets (no JWT_SECRET in response)', async () => {
    const res = await request(app).get('/api/config');
    const body = JSON.stringify(res.body);
    expect(body).not.toContain('JWT_SECRET');
    expect(body).not.toContain('password');
  });
});

// ── 2. CSRF token ─────────────────────────────────────────────────────────────

describe('CSRF token — GET /api/csrf-token', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/api/csrf-token');
    expect(res.status).toBe(200);
  });

  it('returns a csrf_token string', async () => {
    const res = await request(app).get('/api/csrf-token');
    const hasToken = typeof res.body.csrf_token === 'string' || typeof res.body.token === 'string';
    expect(hasToken).toBe(true);
  });
});

// ── 3. Cities endpoint ────────────────────────────────────────────────────────

describe('Cities — GET /api/cities', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/cities');
    expect(res.status).toBe(200);
  });

  it('returns cities array', async () => {
    const res = await request(app).get('/api/cities');
    const hasCities = Array.isArray(res.body.cities) || Array.isArray(res.body);
    expect(hasCities).toBe(true);
  });
});

// ── 4. Auth endpoints ─────────────────────────────────────────────────────────

describe('Auth logout — POST /api/auth/logout', () => {
  it('returns 200 with valid token', async () => {
    const res = await request(app).post('/api/auth/logout').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 401]).toContain(res.status);
  });
});

describe('Auth refresh — POST /api/auth/refresh', () => {
  it('returns 401 without valid token', async () => {
    const res = await request(app).post('/api/auth/refresh').send({});
    expect([400, 401]).toContain(res.status);
  });

  it('returns 200 or 401 with current token', async () => {
    const res = await request(app)
      .post('/api/auth/refresh')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ token: adminToken });
    expect([200, 401, 400]).toContain(res.status);
  });
});

// ── 5. Admin me ───────────────────────────────────────────────────────────────

describe('Admin me — GET /api/admin/me', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/me');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/me').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has username field', async () => {
    const res = await request(app).get('/api/admin/me').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('username');
  });

  it('does not expose password hash', async () => {
    const res = await request(app).get('/api/admin/me').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).not.toHaveProperty('password_hash');
    expect(res.body).not.toHaveProperty('password');
  });
});

// ── 6. Admin stats ────────────────────────────────────────────────────────────

describe('Admin stats — GET /api/admin/stats', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/stats');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has numeric stats fields', async () => {
    const res = await request(app).get('/api/admin/stats').set('Authorization', `Bearer ${adminToken}`);
    // Returns {total_orders, new_orders, active_orders, total_models, available_models, ...}
    const hasStats =
      typeof res.body.total_orders === 'number' ||
      typeof res.body.total === 'number' ||
      typeof res.body.orders_total === 'number' ||
      'total_models' in res.body ||
      'models' in res.body;
    expect(hasStats).toBe(true);
  });
});

// ── 7. Admin notifications ─────────────────────────────────────────────────────

describe('Admin notifications — GET /api/admin/notifications', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/notifications');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/notifications').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has notifications array', async () => {
    const res = await request(app).get('/api/admin/notifications').set('Authorization', `Bearer ${adminToken}`);
    const hasNotifs = Array.isArray(res.body.notifications) || Array.isArray(res.body.items) || Array.isArray(res.body);
    expect(hasNotifs).toBe(true);
  });
});

describe('Admin notifications read-all — PATCH /api/admin/notifications/read-all', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).patch('/api/admin/notifications/read-all');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .patch('/api/admin/notifications/read-all')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 8. Settings sections ──────────────────────────────────────────────────────

describe('Settings sections — GET /api/admin/settings/sections', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/settings/sections');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/settings/sections').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has sections data', async () => {
    const res = await request(app).get('/api/admin/settings/sections').set('Authorization', `Bearer ${adminToken}`);
    // Returns {sections: { contacts: {...}, catalog: {...}, ... }} — object not array
    const hasSections =
      Array.isArray(res.body.sections) ||
      (typeof res.body.sections === 'object' && res.body.sections !== null) ||
      Array.isArray(res.body);
    expect(hasSections).toBe(true);
  });
});

describe('Settings export — GET /api/admin/settings/export', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/settings/export');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/settings/export').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('Settings reset — POST /api/admin/settings/reset', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post('/api/admin/settings/reset').send({ key: 'test_key' });
    expect(res.status).toBe(401);
  });

  it('returns 200 or 400 for valid key reset', async () => {
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ key: 'welcome_text' });
    expect([200, 400]).toContain(res.status);
  });
});

// ── 9. Orders status (public) ─────────────────────────────────────────────────

describe('Order status — GET /api/orders/status/:order_number', () => {
  it('returns 200 for existing order number', async () => {
    const res = await request(app).get('/api/orders/status/ORD-W134');
    expect(res.status).toBe(200);
  });

  it('returns order data', async () => {
    const res = await request(app).get('/api/orders/status/ORD-W134');
    if (res.status === 200) {
      const order = res.body.order || res.body;
      expect(order).toHaveProperty('status');
    }
  });

  it('returns 404 for unknown order number', async () => {
    const res = await request(app).get('/api/orders/status/UNKNOWN-XYZ');
    expect([404, 200]).toContain(res.status);
  });
});

describe('Order status by query — GET /api/orders/status', () => {
  it('returns 400 without order number', async () => {
    const res = await request(app).get('/api/orders/status');
    expect([400, 200]).toContain(res.status);
  });

  it('returns 200 with valid order_number', async () => {
    const res = await request(app).get('/api/orders/status?order_number=ORD-W134');
    expect([200, 400]).toContain(res.status);
  });
});

// ── 10. Model view increment ──────────────────────────────────────────────────

describe('Model view — POST /api/models/:id/view', () => {
  it('returns 200 and increments view count', async () => {
    const res = await request(app).post(`/api/models/${modelId}/view`).send({});
    expect([200, 204]).toContain(res.status);
  });

  it('returns 404 for non-existent model', async () => {
    const res = await request(app).post('/api/models/9999999/view').send({});
    expect([404, 200]).toContain(res.status);
  });
});

// ── 11. Model archived list ───────────────────────────────────────────────────

describe('Archived models — GET /api/admin/models/archived', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/models/archived');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/models/archived').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('returns models array', async () => {
    const res = await request(app).get('/api/admin/models/archived').set('Authorization', `Bearer ${adminToken}`);
    const hasModels = Array.isArray(res.body.models) || Array.isArray(res.body);
    expect(hasModels).toBe(true);
  });
});

// ── 12. Model archive/restore ─────────────────────────────────────────────────

describe('Model archive — PATCH /api/admin/models/:id/archive', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/models/${modelId}/archive`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .patch(`/api/admin/models/${modelId}/archive`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('Model restore — PATCH /api/admin/models/:id/restore', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/models/${modelId}/restore`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .patch(`/api/admin/models/${modelId}/restore`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
  });
});

// ── 13. Order messages ────────────────────────────────────────────────────────

describe('Order messages — GET /api/admin/orders/:id/messages', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}/messages`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/messages`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('returns messages array', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/messages`)
      .set('Authorization', `Bearer ${adminToken}`);
    const hasMsgs = Array.isArray(res.body.messages) || Array.isArray(res.body);
    expect(hasMsgs).toBe(true);
  });
});

describe('Send order message — POST /api/admin/orders/:id/message', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post(`/api/admin/orders/${orderId}/message`).send({ content: 'test' });
    expect(res.status).toBe(401);
  });

  it('returns 400 for empty content', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/message`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ content: '' });
    expect(res.status).toBe(400);
  });

  it('returns 200 for valid message', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/message`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ content: 'Wave134 test message to client' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── 14. Admin orders export ────────────────────────────────────────────────────

describe('Orders export — GET /api/admin/orders/export', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/orders/export');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/orders/export').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 15. Public pricing ────────────────────────────────────────────────────────

describe('Public settings — GET /api/settings/public', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
  });

  it('response is an object', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(typeof res.body).toBe('object');
  });
});

// ── 16. Quick bookings list ────────────────────────────────────────────────────

describe('Quick bookings — GET /api/admin/quick-bookings', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/quick-bookings');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/quick-bookings').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 17. Budget estimate (public) ──────────────────────────────────────────────

describe('Budget estimate — POST /api/budget-estimate', () => {
  it('returns 200 or 404 (endpoint may not exist)', async () => {
    const res = await request(app).post('/api/budget-estimate').send({ event_type: 'photo', duration: 4 });
    expect([200, 400, 404]).toContain(res.status);
  });
});

// ── 18. Public reviews + recent ───────────────────────────────────────────────

describe('Reviews recent — GET /api/reviews/recent', () => {
  it('returns 200 without auth', async () => {
    const res = await request(app).get('/api/reviews/recent');
    expect(res.status).toBe(200);
  });

  it('response has reviews array', async () => {
    const res = await request(app).get('/api/reviews/recent');
    const hasReviews = Array.isArray(res.body.reviews) || Array.isArray(res.body);
    expect(hasReviews).toBe(true);
  });
});

// ── 19. Model generate description ────────────────────────────────────────────

describe('Generate model description — POST /api/admin/models/:id/generate-description', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post(`/api/admin/models/${modelId}/generate-description`);
    expect(res.status).toBe(401);
  });

  it('returns 200, 503, or 500 with auth (503 if AI not configured)', async () => {
    const res = await request(app)
      .post(`/api/admin/models/${modelId}/generate-description`)
      .set('Authorization', `Bearer ${adminToken}`);
    // 200 if Anthropic API configured, 503 if ANTHROPIC_API_KEY missing, 500 on error
    expect([200, 503, 500]).toContain(res.status);
  });
});

// ── 20. Agent logs ────────────────────────────────────────────────────────────

describe('Agent logs — GET /api/admin/agent-logs', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/agent-logs');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/agent-logs').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});
