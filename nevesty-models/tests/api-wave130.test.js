'use strict';
// Wave 130: public model detail, model search, admin model detail/patch/archive/restore,
//           availability endpoints, busy-dates delete, order notes (plural), order detail,
//           PUT order, PATCH order status, bulk-status (POST + PATCH), order search, order history,
//           model stats, admin orders/search, order message

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave130-test-secret-32-chars-ok!!';
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

  // Login
  const lr = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = lr.body.token;

  // Create a model
  const mr = await request(app)
    .post('/api/admin/models/json')
    .set('Authorization', `Bearer ${adminToken}`)
    .send({ name: 'Wave130 Model', age: 25, city: 'Москва', category: 'fashion', available: 1 });
  modelId = mr.body.id;

  // Create an order directly via DB (POST /api/orders requires CSRF + email)
  const orderResult = await dbRun(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, model_id, status)
     VALUES (?,?,?,?,?,?,?)`,
    ['ORD-W130-001', 'Wave130 Client', '+79001234130', 'photo', '2026-08-01', modelId, 'new']
  );
  orderId = orderResult.id;
}, 30000);

// ── 1. Public model detail — GET /api/models/:id ──────────────────────────────

describe('Public model detail — GET /api/models/:id', () => {
  it('returns 200 with model data for valid id', async () => {
    const res = await request(app).get(`/api/models/${modelId}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id', modelId);
    expect(res.body).toHaveProperty('name', 'Wave130 Model');
  });

  it('returns photos as an array', async () => {
    const res = await request(app).get(`/api/models/${modelId}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.photos)).toBe(true);
  });

  it('returns busy_dates field', async () => {
    const res = await request(app).get(`/api/models/${modelId}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
  });

  it('returns 404 for non-existent model', async () => {
    const res = await request(app).get('/api/models/999999');
    expect(res.status).toBe(404);
  });

  it('returns 400 for non-numeric id', async () => {
    const res = await request(app).get('/api/models/abc');
    expect(res.status).toBe(400);
  });
});

// ── 2. Model search — GET /api/models/search ──────────────────────────────────

describe('Model search — GET /api/models/search', () => {
  it('returns 200 with models and total', async () => {
    const res = await request(app).get('/api/models/search');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('models');
    expect(res.body).toHaveProperty('total');
  });

  it('filters by name', async () => {
    const res = await request(app).get('/api/models/search?name=Wave130');
    expect(res.status).toBe(200);
    expect(res.body.models.length).toBeGreaterThan(0);
    expect(res.body.models[0].name).toContain('Wave130');
  });

  it('filters by city', async () => {
    const res = await request(app).get('/api/models/search?city=Москва');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('filters by category', async () => {
    const res = await request(app).get('/api/models/search?category=fashion');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  it('filters by height range', async () => {
    const res = await request(app).get('/api/models/search?min_height=150&max_height=185');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('models');
  });

  it('returns page field', async () => {
    const res = await request(app).get('/api/models/search?page=0');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('page', 0);
  });
});

// ── 3. Admin model detail (via models list with search) — GET /api/admin/models ──

describe('Admin models list — GET /api/admin/models', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/models');
    expect(res.status).toBe(401);
  });

  it('returns 200 with models list', async () => {
    const res = await request(app).get('/api/admin/models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('models');
    expect(res.body).toHaveProperty('total');
  });

  it('supports search filter', async () => {
    const res = await request(app).get('/api/admin/models?search=Wave130').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.models.length).toBeGreaterThan(0);
  });

  it('supports sort by name', async () => {
    const res = await request(app).get('/api/admin/models?sort=name').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 4. Admin model availability — GET /api/admin/models/:id/availability ────

describe('Admin model availability — GET /api/admin/models/:id/availability', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/models/${modelId}/availability`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with month and busy_dates for valid model', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('month');
    expect(Array.isArray(res.body.busy_dates)).toBe(true);
  });

  it('accepts ?month=YYYY-MM parameter', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=2026-08`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.month).toBe('2026-08');
  });

  it('returns 400 for invalid month format', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=2026-99`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  it('returns 400 for bad month string format', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=not-a-month`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  it('returns 404 for non-existent model', async () => {
    const res = await request(app)
      .get('/api/admin/models/999999/availability')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });
});

// ── 5. POST availability + DELETE busy-dates/:date ───────────────────────────

describe('Admin model availability — POST + DELETE', () => {
  const testDate = '2026-09-15';

  it('POST /api/admin/models/:id/availability adds a busy date', async () => {
    const res = await request(app)
      .post(`/api/admin/models/${modelId}/availability`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ date: testDate, note: 'Shooting day' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('success', true);
  });

  it('busy date appears in GET availability', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=2026-09`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.busy_dates).toContain(testDate);
  });

  it('POST availability returns 400 without date', async () => {
    const res = await request(app)
      .post(`/api/admin/models/${modelId}/availability`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'No date' });
    expect(res.status).toBe(400);
  });

  it('DELETE /api/admin/models/:id/busy-dates/:date removes the date', async () => {
    const res = await request(app)
      .delete(`/api/admin/models/${modelId}/busy-dates/${testDate}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
  });

  it('date no longer appears after delete', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/availability?month=2026-09`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.busy_dates).not.toContain(testDate);
  });

  it('DELETE without auth returns 401', async () => {
    const res = await request(app).delete(`/api/admin/models/${modelId}/busy-dates/2026-09-15`);
    expect(res.status).toBe(401);
  });
});

// ── 6. Order notes (plural) — GET/POST /api/admin/orders/:id/notes ───────────

describe('Order notes — GET/POST /api/admin/orders/:id/notes', () => {
  it('GET returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}/notes`);
    expect(res.status).toBe(401);
  });

  it('GET returns notes array for valid order', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('notes');
    expect(Array.isArray(res.body.notes)).toBe(true);
  });

  it('POST adds a note successfully', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'Wave130 internal note' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('success', true);
  });

  it('POST returns 400 when note is empty', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: '' });
    expect(res.status).toBe(400);
  });

  it('POST returns 401 without auth', async () => {
    const res = await request(app).post(`/api/admin/orders/${orderId}/notes`).send({ note: 'unauthorized' });
    expect(res.status).toBe(401);
  });

  it('added note appears in GET notes list', async () => {
    await request(app)
      .post(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ note: 'Visible note for wave130' });
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/notes`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const texts = res.body.notes.map(n => n.admin_note);
    expect(texts.some(t => t.includes('wave130'))).toBe(true);
  });
});

// ── 7. Admin single order detail — GET /api/admin/orders/:id ─────────────────

describe('Admin order detail — GET /api/admin/orders/:id', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with order data', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}`).set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id', orderId);
    expect(res.body).toHaveProperty('messages');
  });

  it('returns has_unread field', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}`).set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('has_unread');
  });

  it('returns 404 for non-existent order', async () => {
    const res = await request(app).get('/api/admin/orders/999999').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });

  it('returns 400 for invalid order id', async () => {
    const res = await request(app).get('/api/admin/orders/abc').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });
});

// ── 8. PUT /api/admin/orders/:id — update order ───────────────────────────────

describe('PUT /api/admin/orders/:id — update order', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).put(`/api/admin/orders/${orderId}`).send({ status: 'reviewing' });
    expect(res.status).toBe(401);
  });

  it('updates order status successfully', async () => {
    const res = await request(app)
      .put(`/api/admin/orders/${orderId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'reviewing' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
  });

  it('returns 400 for invalid status', async () => {
    const res = await request(app)
      .put(`/api/admin/orders/${orderId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'invalid_status_xyz' });
    expect(res.status).toBe(400);
  });

  it('updates admin_notes field', async () => {
    const res = await request(app)
      .put(`/api/admin/orders/${orderId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ admin_notes: 'Note from wave130 test' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
  });

  it('returns 404 for non-existent order', async () => {
    const res = await request(app)
      .put('/api/admin/orders/999999')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect(res.status).toBe(404);
  });
});

// ── 9. PATCH /api/admin/orders/:id/status ────────────────────────────────────

describe('PATCH /api/admin/orders/:id/status', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/orders/${orderId}/status`).send({ status: 'confirmed' });
    expect(res.status).toBe(401);
  });

  it('updates status to confirmed', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect(res.status).toBe(200);
    // route returns { success: true }
    expect(res.body.success || res.body.ok).toBeTruthy();
  });

  it('returns 400 for disallowed status', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'foobar' });
    expect(res.status).toBe(400);
  });

  it('returns 404 for missing order', async () => {
    const res = await request(app)
      .patch('/api/admin/orders/999999/status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'completed' });
    expect(res.status).toBe(404);
  });
});

// ── 10. POST /api/admin/orders/bulk-status ────────────────────────────────────

describe('POST /api/admin/orders/bulk-status', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .send({ order_ids: [orderId], status: 'new' });
    expect(res.status).toBe(401);
  });

  it('updates multiple orders in bulk', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ order_ids: [orderId], status: 'in_progress' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
    expect(res.body.affected).toBeGreaterThan(0);
  });

  it('returns 400 for empty order_ids', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ order_ids: [], status: 'new' });
    expect(res.status).toBe(400);
  });

  it('returns 400 for invalid status', async () => {
    const res = await request(app)
      .post('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ order_ids: [orderId], status: 'bad_status' });
    expect(res.status).toBe(400);
  });
});

// ── 11. PATCH /api/admin/orders/bulk-status ───────────────────────────────────

describe('PATCH /api/admin/orders/bulk-status (REST alias)', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .send({ ids: [orderId], status: 'new' });
    expect(res.status).toBe(401);
  });

  it('bulk updates via PATCH', async () => {
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [orderId], status: 'new' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('updated');
  });

  it('returns 400 for empty ids array', async () => {
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [], status: 'new' });
    expect(res.status).toBe(400);
  });

  it('returns 400 for invalid status', async () => {
    const res = await request(app)
      .patch('/api/admin/orders/bulk-status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [orderId], status: 'not_valid' });
    expect(res.status).toBe(400);
  });
});

// ── 12. Order history — GET /api/admin/orders/:id/history ────────────────────

describe('Order history — GET /api/admin/orders/:id/history', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}/history`);
    expect(res.status).toBe(401);
  });

  it('returns history array', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/history`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('returns 400 for invalid id', async () => {
    const res = await request(app).get('/api/admin/orders/xyz/history').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });
});

// ── 13. Admin orders search — GET /api/admin/orders/search ───────────────────
// Note: this route is registered AFTER /admin/orders/:id so it is reachable directly

describe('Admin orders search — GET /api/admin/orders/search', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/orders/search?q=Wave130');
    expect(res.status).toBe(401);
  });

  it('returns 400 without q param', async () => {
    const res = await request(app).get('/api/admin/orders/search').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  it('returns 200 with orders array for known client name', async () => {
    const res = await request(app)
      .get('/api/admin/orders/search?q=Wave130+Client')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('orders');
    expect(Array.isArray(res.body.orders)).toBe(true);
  });

  it('returns 200 for phone-based search', async () => {
    const res = await request(app)
      .get('/api/admin/orders/search?q=%2B79001234130')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('orders');
  });

  it('returns total field', async () => {
    const res = await request(app)
      .get('/api/admin/orders/search?q=ORD-W130')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('total');
    expect(typeof res.body.total).toBe('number');
  });
});

// ── 14. Model stats — GET /api/admin/models/:id/stats ────────────────────────

describe('Model stats — GET /api/admin/models/:id/stats', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/models/${modelId}/stats`);
    expect(res.status).toBe(401);
  });

  it('returns stats object for valid model', async () => {
    const res = await request(app)
      .get(`/api/admin/models/${modelId}/stats`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('total_orders');
    expect(res.body).toHaveProperty('completed_orders');
    expect(res.body).toHaveProperty('view_count');
    expect(res.body).toHaveProperty('review_count');
  });

  it('returns 404 for non-existent model', async () => {
    const res = await request(app).get('/api/admin/models/999999/stats').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });
});
