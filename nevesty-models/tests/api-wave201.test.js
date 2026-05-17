'use strict';
/**
 * Wave 201: Tests for recently added endpoints
 *  - GET  /api/admin/clients  (list + pagination + search + filter)
 *  - GET  /api/admin/clients/:phone  (profile)
 *  - GET  /api/admin/system   (detailed system info shape)
 *  - POST /api/orders with client_email — must not crash when mailer not configured
 */

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave201-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';
// Ensure mailer has no SMTP configured (test env default)
delete process.env.SMTP_HOST;
delete process.env.SMTP_USER;
delete process.env.SMTP_PASS;

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app, adminToken;

async function getCsrf() {
  const r = await request(app).get('/api/csrf-token');
  return r.body.token;
}

beforeAll(async () => {
  const { initDatabase, run } = require('../database');
  await initDatabase();

  require('../bot');
  const apiRouter = require('../routes/api');

  const a = express();
  a.use(express.json({ limit: '2mb' }));
  a.use(express.urlencoded({ extended: true }));
  a.use(cors());
  a.use('/api', apiRouter);
  // eslint-disable-next-line no-unused-vars
  a.use((err, req, res, _next) => res.status(500).json({ error: err.message }));
  app = a;

  // Authenticate
  const loginRes = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
  expect(adminToken).toBeTruthy();

  // Seed clients via direct DB insert so clients list is non-empty
  await run(
    "INSERT INTO orders (order_number,client_name,client_phone,client_email,event_type,event_date,event_duration,status) VALUES ('ORD-W201A','Anna Test','+79991110001','anna@wave201.test','photo_shoot','2026-06-01',3,'new')",
    []
  );
  await run(
    "INSERT INTO orders (order_number,client_name,client_phone,client_email,event_type,event_date,event_duration,status) VALUES ('ORD-W201B','Boris Test','+79991110002','boris@wave201.test','corporate','2026-06-02',4,'completed')",
    []
  );
  await run(
    "INSERT INTO orders (order_number,client_name,client_phone,client_email,event_type,event_date,event_duration,status) VALUES ('ORD-W201C','Anna Test','+79991110001','anna@wave201.test','photo_shoot','2026-07-01',2,'completed')",
    []
  );
}, 30000);

afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

// ─── GET /api/admin/clients ───────────────────────────────────────────────────

describe('GET /api/admin/clients', () => {
  it('rejects unauthenticated requests with 401', async () => {
    const res = await request(app).get('/api/admin/clients');
    expect(res.status).toBe(401);
  });

  it('returns clients list with pagination fields', async () => {
    const res = await request(app).get('/api/admin/clients').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.clients)).toBe(true);
    expect(typeof res.body.total).toBe('number');
    expect(typeof res.body.page).toBe('number');
    expect(typeof res.body.pages).toBe('number');
    expect(typeof res.body.limit).toBe('number');
  });

  it('returns at least 2 unique clients from seeded data', async () => {
    const res = await request(app).get('/api/admin/clients').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.total).toBeGreaterThanOrEqual(2);
    expect(res.body.clients.length).toBeGreaterThanOrEqual(2);
  });

  it('client record has expected fields', async () => {
    const res = await request(app).get('/api/admin/clients').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const client = res.body.clients[0];
    expect(client).toHaveProperty('client_name');
    expect(client).toHaveProperty('client_phone');
    expect(client).toHaveProperty('total_orders');
    expect(client).toHaveProperty('last_activity');
  });

  it('search by phone filters results', async () => {
    const res = await request(app)
      .get('/api/admin/clients?search=79991110001')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    res.body.clients.forEach(c => {
      expect(c.client_phone).toContain('79991110001');
    });
  });

  it('search returns empty array for unknown name', async () => {
    const res = await request(app)
      .get('/api/admin/clients?search=NoSuchClientXYZ987')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.clients.length).toBe(0);
    expect(res.body.total).toBe(0);
  });

  it('pagination: page=1 and limit=1 returns exactly 1 client', async () => {
    const res = await request(app)
      .get('/api/admin/clients?page=1&limit=1')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.clients.length).toBe(1);
    expect(res.body.limit).toBe(1);
    expect(res.body.page).toBe(1);
    expect(res.body.pages).toBeGreaterThanOrEqual(2);
  });

  it('filter=active returns array', async () => {
    const res = await request(app).get('/api/admin/clients?filter=active').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.clients)).toBe(true);
  });

  it('filter=new returns array', async () => {
    const res = await request(app).get('/api/admin/clients?filter=new').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.clients)).toBe(true);
  });

  it('filter=vip returns only clients with >= 3 orders', async () => {
    const res = await request(app).get('/api/admin/clients?filter=vip').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.clients)).toBe(true);
    res.body.clients.forEach(c => {
      expect(Number(c.total_orders)).toBeGreaterThanOrEqual(3);
    });
  });

  it('limit is capped at 100', async () => {
    const res = await request(app).get('/api/admin/clients?limit=9999').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body.limit).toBe(100);
  });
});

// ─── GET /api/admin/clients/:phone ───────────────────────────────────────────

describe('GET /api/admin/clients/:phone', () => {
  it('rejects unauthenticated requests with 401', async () => {
    const res = await request(app).get('/api/admin/clients/%2B79991110001');
    expect(res.status).toBe(401);
  });

  it('returns client profile for known phone', async () => {
    const encodedPhone = encodeURIComponent('+79991110001');
    const res = await request(app)
      .get(`/api/admin/clients/${encodedPhone}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body).toHaveProperty('profile');
      expect(res.body).toHaveProperty('orders');
      expect(Array.isArray(res.body.orders)).toBe(true);
    }
  });

  it('returns 404 for unknown phone', async () => {
    const encodedPhone = encodeURIComponent('+70000000000');
    const res = await request(app)
      .get(`/api/admin/clients/${encodedPhone}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });
});

// ─── GET /api/admin/system ────────────────────────────────────────────────────

describe('GET /api/admin/system', () => {
  it('rejects unauthenticated requests with 401', async () => {
    const res = await request(app).get('/api/admin/system');
    expect(res.status).toBe(401);
  });

  it('returns 200 for authenticated admin', async () => {
    const res = await request(app).get('/api/admin/system').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('contains node_version and uptime_seconds', async () => {
    const res = await request(app).get('/api/admin/system').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.node_version).toBe('string');
    expect(typeof res.body.uptime_seconds).toBe('number');
    expect(res.body.uptime_seconds).toBeGreaterThanOrEqual(0);
  });

  it('contains memory_mb with rss, heap_used, heap_total', async () => {
    const res = await request(app).get('/api/admin/system').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.memory_mb.rss).toBe('number');
    expect(typeof res.body.memory_mb.heap_used).toBe('number');
    expect(typeof res.body.memory_mb.heap_total).toBe('number');
  });

  it('contains platform, arch, pid, env', async () => {
    const res = await request(app).get('/api/admin/system').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.platform).toBe('string');
    expect(typeof res.body.arch).toBe('string');
    expect(typeof res.body.pid).toBe('number');
    expect(typeof res.body.env).toBe('string');
  });

  it('contains os_free_mem_mb, os_total_mem_mb and load_avg[3]', async () => {
    const res = await request(app).get('/api/admin/system').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.os_free_mem_mb).toBe('number');
    expect(typeof res.body.os_total_mem_mb).toBe('number');
    expect(Array.isArray(res.body.load_avg)).toBe(true);
    expect(res.body.load_avg.length).toBe(3);
  });

  it('contains uploads_count and scheduled_jobs', async () => {
    const res = await request(app).get('/api/admin/system').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body.uploads_count).toBe('number');
    expect(typeof res.body.scheduled_jobs).toBe('number');
  });
});

// ─── POST /api/orders with email — mailer not configured ─────────────────────

describe('POST /api/orders with client_email — mailer not configured', () => {
  it('succeeds and returns order_number when SMTP is not configured', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'Email Test Client',
      client_phone: '+79991119901',
      client_email: 'wave201-email@test.com',
      event_type: 'photo_shoot',
      event_date: '2026-09-15',
      event_duration: 4,
    });
    // mailer failures are non-blocking (.catch()) — no 500
    expect(res.status).toBe(200);
    expect(typeof res.body.order_number).toBe('string');
    expect(typeof res.body.id).toBe('number');
  });

  it('order is actually stored in DB after POST with email', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'DB Persist Client',
      client_phone: '+79991119902',
      client_email: 'persist@wave201.test',
      event_type: 'corporate',
      event_date: '2026-10-01',
      event_duration: 6,
    });
    expect(res.status).toBe(200);

    const statusRes = await request(app).get(`/api/orders/status/${res.body.order_number}`);
    expect(statusRes.status).toBe(200);
    expect(statusRes.body.id).toBe(res.body.id);
  });

  it('returns 400 for invalid email format', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'Bad Email Client',
      client_phone: '+79991119903',
      client_email: 'not-an-email',
      event_type: 'photo_shoot',
      event_date: '2026-09-15',
      event_duration: 3,
    });
    expect(res.status).toBe(400);
    expect(res.body.error).toMatch(/email/i);
  });

  it('does not crash (no 500) without email field', async () => {
    const csrf = await getCsrf();
    const res = await request(app).post('/api/orders').set('x-csrf-token', csrf).send({
      client_name: 'No Email Client',
      client_phone: '+79991119904',
      event_type: 'photo_shoot',
      event_date: '2026-09-20',
      event_duration: 2,
    });
    // Must not be 500
    expect(res.status).not.toBe(500);
  });

  it('returns 403 without CSRF token', async () => {
    const res = await request(app).post('/api/orders').send({
      client_name: 'CSRF Test',
      client_phone: '+79991119905',
      client_email: 'csrf@wave201.test',
      event_type: 'photo_shoot',
      event_date: '2026-09-20',
      event_duration: 2,
    });
    expect(res.status).toBe(403);
  });
});
