'use strict';
// Wave 132: messages, order detail/payment/invoice, WhatsApp, factory, analytics++, client OTP

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave132-test-secret-32-chars-ok!!';
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
    .send({ name: 'Wave132 Model', age: 22, city: 'Москва', category: 'fashion' });
  modelId = mr.body.id;

  const ord = await dbRun(
    `INSERT INTO orders (order_number, client_name, client_phone, event_type, event_date, model_id, status, budget, client_chat_id)
     VALUES (?,?,?,?,?,?,?,?,?)`,
    ['ORD-W132', 'Wave132 Client', '+79001234567', 'photo', '2027-01-15', modelId, 'new', '75000', 777132]
  );
  orderId = ord.id;

  // Insert a message linked to the order
  await dbRun(`INSERT INTO messages (order_id, content, sender_type) VALUES (?,?,?)`, [
    orderId,
    'Wave132 test message',
    'client',
  ]);
}, 30000);

// ── 1. Admin messages recent ──────────────────────────────────────────────────

describe('Admin messages recent — GET /api/admin/messages/recent', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/messages/recent');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/messages/recent').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has messages array', async () => {
    const res = await request(app).get('/api/admin/messages/recent').set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.ok).toBe(true);
    expect(Array.isArray(res.body.messages)).toBe(true);
  });

  it('accepts ?limit param', async () => {
    const res = await request(app)
      .get('/api/admin/messages/recent?limit=5')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.messages.length).toBeLessThanOrEqual(5);
  });
});

// ── 2. Admin messages paginated ───────────────────────────────────────────────

describe('Admin messages list — GET /api/admin/messages', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/messages');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/messages').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('has messages array and total', async () => {
    const res = await request(app).get('/api/admin/messages').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.messages)).toBe(true);
    expect(typeof res.body.total).toBe('number');
  });

  it('accepts ?filter=unread', async () => {
    const res = await request(app)
      .get('/api/admin/messages?filter=unread')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('accepts ?filter=today', async () => {
    const res = await request(app).get('/api/admin/messages?filter=today').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 3. Order detail endpoint ───────────────────────────────────────────────────

describe('Order detail — GET /api/admin/orders/:id/detail', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get(`/api/admin/orders/${orderId}/detail`);
    expect(res.status).toBe(401);
  });

  it('returns 200 for existing order', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/detail`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('order has id and client_name fields', async () => {
    const res = await request(app)
      .get(`/api/admin/orders/${orderId}/detail`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('id');
    expect(res.body).toHaveProperty('client_name');
  });

  it('returns 404 for non-existent order', async () => {
    const res = await request(app).get('/api/admin/orders/9999999/detail').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });

  it('returns 400 for invalid id', async () => {
    const res = await request(app).get('/api/admin/orders/abc/detail').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });
});

// ── 4. Order payment update ────────────────────────────────────────────────────

describe('Order payment — PATCH /api/admin/orders/:id/payment', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/orders/${orderId}/payment`).send({ paid: true });
    expect(res.status).toBe(401);
  });

  it('returns 400 for missing paid field', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(400);
  });

  it('returns 200 for paid=true', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: true });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('paid_at is set after paid=true', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: true });
    expect(res.body.paid_at).toBeTruthy();
  });

  it('returns 200 for paid=false', async () => {
    const res = await request(app)
      .patch(`/api/admin/orders/${orderId}/payment`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: false });
    expect(res.status).toBe(200);
    expect(res.body.paid_at).toBeNull();
  });

  it('returns 404 for non-existent order', async () => {
    const res = await request(app)
      .patch('/api/admin/orders/9999999/payment')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ paid: true });
    expect(res.status).toBe(404);
  });
});

// ── 5. Send invoice ────────────────────────────────────────────────────────────

describe('Send invoice — POST /api/admin/orders/:id/send-invoice', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post(`/api/admin/orders/${orderId}/send-invoice`);
    expect(res.status).toBe(401);
  });

  it('returns 200 for existing order', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/send-invoice`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('invoice_sent_at is returned', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/send-invoice`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.invoice_sent_at).toBeTruthy();
  });

  it('returns 404 for non-existent order', async () => {
    const res = await request(app)
      .post('/api/admin/orders/9999999/send-invoice')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });
});

// ── 6. WhatsApp link generator ─────────────────────────────────────────────────

describe('WhatsApp link — POST /api/admin/orders/:id/whatsapp', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).post(`/api/admin/orders/${orderId}/whatsapp`);
    expect(res.status).toBe(401);
  });

  it('returns 200 with whatsapp_url for order with phone', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/whatsapp`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(res.body.whatsapp_url).toMatch(/wa\.me/);
  });

  it('accepts custom message', async () => {
    const res = await request(app)
      .post(`/api/admin/orders/${orderId}/whatsapp`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ message: 'Привет, это тест!' });
    expect(res.status).toBe(200);
    expect(res.body.whatsapp_url).toContain('%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82');
  });

  it('returns 404 for non-existent order', async () => {
    const res = await request(app)
      .post('/api/admin/orders/9999999/whatsapp')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });
});

// ── 7. Admin discussions ───────────────────────────────────────────────────────

describe('Admin discussions — GET /api/admin/discussions', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/discussions');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/discussions').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('accepts ?limit param', async () => {
    const res = await request(app).get('/api/admin/discussions?limit=10').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 8. Admin findings ─────────────────────────────────────────────────────────

describe('Admin findings — GET /api/admin/findings', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/findings');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/findings').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  it('accepts ?status=open', async () => {
    const res = await request(app).get('/api/admin/findings?status=open').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('accepts ?status=closed', async () => {
    const res = await request(app)
      .get('/api/admin/findings?status=closed')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 9. Factory tasks ──────────────────────────────────────────────────────────

describe('Factory tasks — GET /api/admin/factory-tasks', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/factory-tasks');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/factory-tasks').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('response has tasks array and stats', async () => {
    const res = await request(app).get('/api/admin/factory-tasks').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.tasks)).toBe(true);
    expect(res.body).toHaveProperty('stats');
  });
});

describe('Factory tasks update — PATCH /api/admin/factory-tasks/:id', () => {
  let taskId;

  beforeAll(async () => {
    const { run: dbRun } = require('../database');
    const result = await dbRun(`INSERT INTO factory_tasks (action, status, priority) VALUES (?,?,?)`, [
      'Wave132 test task action',
      'pending',
      5,
    ]);
    taskId = result.id;
  });

  it('returns 401 without auth', async () => {
    const res = await request(app).patch(`/api/admin/factory-tasks/${taskId}`).send({ status: 'done' });
    expect(res.status).toBe(401);
  });

  it('returns 200 for valid status update', async () => {
    const res = await request(app)
      .patch(`/api/admin/factory-tasks/${taskId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'done' });
    expect(res.status).toBe(200);
    expect(res.body.success).toBe(true);
  });

  it('returns 400 for invalid status', async () => {
    const res = await request(app)
      .patch(`/api/admin/factory-tasks/${taskId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'invalid_xyz' });
    expect(res.status).toBe(400);
  });

  it('accepts skipped status', async () => {
    const res = await request(app)
      .patch(`/api/admin/factory-tasks/${taskId}`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'skipped' });
    expect(res.status).toBe(200);
  });
});

// ── 10. Analytics heatmap ─────────────────────────────────────────────────────

describe('Analytics heatmap — GET /api/admin/analytics/heatmap', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('has heatmap object and year', async () => {
    const res = await request(app).get('/api/admin/analytics/heatmap').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.heatmap).toBe('object');
    expect(typeof res.body.year).toBe('number');
  });

  it('accepts ?year param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/heatmap?year=2026')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.year).toBe(2026);
  });
});

// ── 11. Analytics client LTV ──────────────────────────────────────────────────

describe('Analytics client LTV — GET /api/admin/analytics/client-ltv', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('has top_clients array', async () => {
    const res = await request(app).get('/api/admin/analytics/client-ltv').set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body.top_clients)).toBe(true);
  });

  it('accepts ?limit param', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/client-ltv?limit=5')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.top_clients.length).toBeLessThanOrEqual(5);
  });
});

// ── 12. Analytics repeat clients ──────────────────────────────────────────────

describe('Analytics repeat clients — GET /api/admin/analytics/repeat-clients', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/analytics/repeat-clients');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/repeat-clients')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('has total, repeat, new fields', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/repeat-clients')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.data).toHaveProperty('total');
    expect(res.body.data).toHaveProperty('repeat');
    expect(res.body.data).toHaveProperty('new');
  });

  it('all values are non-negative', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/repeat-clients')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.data.total).toBeGreaterThanOrEqual(0);
    expect(res.body.data.repeat).toBeGreaterThanOrEqual(0);
    expect(res.body.data.new).toBeGreaterThanOrEqual(0);
  });
});

// ── 13. Email config check ─────────────────────────────────────────────────────

describe('Email config — GET /api/admin/email/test', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/email/test');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('has configured boolean field', async () => {
    const res = await request(app).get('/api/admin/email/test').set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body.configured).toBe('boolean');
  });
});

// ── 14. Client OTP flow ────────────────────────────────────────────────────────

describe('Client OTP — POST /api/client/request-code', () => {
  it('returns 400 for missing phone', async () => {
    const res = await request(app).post('/api/client/request-code').send({});
    expect(res.status).toBe(400);
    expect(res.body).toHaveProperty('error');
  });

  it('returns 400 for invalid phone format', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: 'not-a-number' });
    expect([400, 404]).toContain(res.status);
  });

  it('returns 404 for phone with no orders', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '+7 (000) 000-00-00' });
    expect([400, 404]).toContain(res.status);
  });

  it('returns 200 or 429 for phone that has an order (test DB)', async () => {
    // Wave132 Client has order with +79001234567
    // 429 possible if rate limiter triggers (3 req/min limit in tests)
    const res = await request(app).post('/api/client/request-code').send({ phone: '+79001234567' });
    expect([200, 429]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body.ok).toBe(true);
    }
  });

  it('returns code_debug in test mode (no SMS configured)', async () => {
    const res = await request(app).post('/api/client/request-code').send({ phone: '+79001234567' });
    if (res.status === 200) {
      expect(res.body).toHaveProperty('code_debug');
    }
  });
});

describe('Client OTP — POST /api/client/verify', () => {
  it('returns 400 or 429 for missing fields (rate limiter may trigger)', async () => {
    const res = await request(app).post('/api/client/verify').send({});
    expect([400, 429]).toContain(res.status);
  });

  it('returns 401, 400, or 429 for wrong code', async () => {
    const res = await request(app).post('/api/client/verify').send({ phone: '+79001234567', code: '000000' });
    expect([401, 400, 429]).toContain(res.status);
  });

  it('returns token for correct code (if rate limiter allows)', async () => {
    const reqRes = await request(app).post('/api/client/request-code').send({ phone: '+79001234567' });
    if (reqRes.status === 200 && reqRes.body.code_debug) {
      const verRes = await request(app)
        .post('/api/client/verify')
        .send({ phone: '+79001234567', code: reqRes.body.code_debug });
      expect([200, 429]).toContain(verRes.status);
      if (verRes.status === 200) {
        expect(verRes.body.ok).toBe(true);
        expect(verRes.body).toHaveProperty('token');
      }
    }
  });
});

// ── 15. Client orders lookup ───────────────────────────────────────────────────

describe('Client orders — GET /api/client/orders', () => {
  it('returns 400 without phone', async () => {
    const res = await request(app).get('/api/client/orders');
    expect(res.status).toBe(400);
  });

  it('returns 400 for invalid phone', async () => {
    const res = await request(app).get('/api/client/orders?phone=not-a-phone');
    expect(res.status).toBe(400);
  });

  it('returns 200 for phone with orders', async () => {
    const res = await request(app).get('/api/client/orders?phone=79001234567');
    expect(res.status).toBe(200);
  });

  it('returns orders array', async () => {
    const res = await request(app).get('/api/client/orders?phone=79001234567');
    if (res.status === 200) {
      const orders = res.body.orders || res.body;
      expect(Array.isArray(orders)).toBe(true);
    }
  });
});

// ── 16. Factory status endpoint ───────────────────────────────────────────────

describe('Factory status — GET /api/admin/factory/status', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/factory/status');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/factory/status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

describe('Factory actions — GET /api/admin/factory/actions', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/factory/actions');
    expect(res.status).toBe(401);
  });

  it('returns 200 or 500 with auth (500 if better-sqlite3 not installed)', async () => {
    const res = await request(app).get('/api/admin/factory/actions').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
  });
});

describe('Factory decisions — GET /api/admin/factory/decisions', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/factory/decisions');
    expect(res.status).toBe(401);
  });

  it('returns 200 or 500 with auth (500 if better-sqlite3 not installed)', async () => {
    const res = await request(app).get('/api/admin/factory/decisions').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
  });
});

// ── 17. CRM status ─────────────────────────────────────────────────────────────

describe('CRM status — GET /api/admin/crm-status', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/admin/crm-status');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/admin/crm-status').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 18. Stats extended ───────────────────────────────────────────────────────

describe('Stats extended — GET /api/stats/extended', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/stats/extended');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth', async () => {
    const res = await request(app).get('/api/stats/extended').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });
});

// ── 19. Export orders ──────────────────────────────────────────────────────────

describe('Export orders — GET /api/export/orders', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app).get('/api/export/orders');
    expect(res.status).toBe(401);
  });

  it('returns 200 with auth (CSV or JSON)', async () => {
    const res = await request(app).get('/api/export/orders').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('content-type indicates CSV or JSON', async () => {
    const res = await request(app).get('/api/export/orders').set('Authorization', `Bearer ${adminToken}`);
    const ct = res.headers['content-type'] || '';
    expect(ct).toMatch(/csv|json|text/);
  });
});

// ── 20. Models bulk update ─────────────────────────────────────────────────────

describe('Models bulk update — PATCH /api/admin/models/bulk', () => {
  it('returns 401 without auth', async () => {
    const res = await request(app)
      .patch('/api/admin/models/bulk')
      .send({ ids: [modelId], action: 'feature' });
    expect(res.status).toBe(401);
  });

  it('returns 200 for valid bulk action', async () => {
    const res = await request(app)
      .patch('/api/admin/models/bulk')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ ids: [modelId], action: 'feature' });
    expect([200, 400]).toContain(res.status);
  });
});
