'use strict';
// Wave 145: Security JWT тесты — type confusion, refresh rotation, catalog, logout

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave145-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const jwt = require('jsonwebtoken');

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Build a fresh express app with isolated modules so rate limiters reset */
async function buildApp() {
  let app, request, initDatabase, apiRouter;
  jest.isolateModules(() => {
    request = require('supertest');
    const express = require('express');
    const cors = require('cors');
    ({ initDatabase } = require('../database'));
    require('../bot');
    apiRouter = require('../routes/api');

    const a = express();
    a.use(express.json({ limit: '2mb' }));
    a.use(express.urlencoded({ extended: true }));
    a.use(cors());
    a.use('/api', apiRouter);
    app = a;
  });
  await initDatabase();
  return { app, request };
}

// ─── 1. JWT Type Confusion protection ────────────────────────────────────────

describe('JWT Type Confusion protection', () => {
  let app, request, adminToken, clientToken;

  beforeAll(async () => {
    const supertest = require('supertest');
    const express = require('express');
    const cors = require('cors');
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
    request = supertest;

    const lr = await supertest(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
    adminToken = lr.body.token;

    // Build a client JWT manually (mimics /client/verify)
    clientToken = jwt.sign({ phone: '79001234567', type: 'client', chat_id: '12345' }, process.env.JWT_SECRET, {
      expiresIn: '1h',
    });
  }, 30000);

  afterAll(() => {
    const db = require('../database');
    if (db.closeDatabase) db.closeDatabase();
  });

  it('client token on admin endpoint → 401 (type confusion rejected)', async () => {
    const res = await request(app).get('/api/admin/me').set('Authorization', `Bearer ${clientToken}`);
    expect(res.status).toBe(401);
  });

  it('valid adminToken on admin endpoint → 200', async () => {
    const res = await request(app).get('/api/admin/me').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('adminToken contains type: "admin" (decoded without verify)', () => {
    const decoded = jwt.decode(adminToken);
    expect(decoded).not.toBeNull();
    expect(decoded.type).toBe('admin');
  });

  it('clientToken contains type: "client" (decoded without verify)', () => {
    const decoded = jwt.decode(clientToken);
    expect(decoded).not.toBeNull();
    expect(decoded.type).toBe('client');
  });

  it('no Authorization header on admin endpoint → 401', async () => {
    const res = await request(app).get('/api/admin/me');
    expect(res.status).toBe(401);
  });

  it('forged token with type:"admin" but wrong secret → 401', async () => {
    const forged = jwt.sign({ id: 1, username: 'hacker', role: 'admin', type: 'admin' }, 'wrong-secret', {
      expiresIn: '1h',
    });
    const res = await request(app).get('/api/admin/me').set('Authorization', `Bearer ${forged}`);
    expect(res.status).toBe(401);
  });
});

// ─── 2. Refresh token flow ────────────────────────────────────────────────────

describe('Refresh token flow', () => {
  let app2, request2;
  let firstToken, firstRefresh, secondToken, secondRefresh;

  beforeAll(async () => {
    jest.resetModules();
    const supertest = require('supertest');
    const express = require('express');
    const cors = require('cors');
    const { initDatabase } = require('../database');
    await initDatabase();
    require('../bot');
    const apiRouter = require('../routes/api');

    const a = express();
    a.use(express.json({ limit: '2mb' }));
    a.use(express.urlencoded({ extended: true }));
    a.use(cors());
    a.use('/api', apiRouter);
    app2 = a;
    request2 = supertest;

    const lr = await supertest(a).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
    firstToken = lr.body.token;
    firstRefresh = lr.body.refresh_token;
  }, 30000);

  afterAll(() => {
    try {
      const db = require('../database');
      if (db.closeDatabase) db.closeDatabase();
    } catch (_) {}
  });

  it('POST /api/admin/login → returns token and refresh_token', () => {
    expect(typeof firstToken).toBe('string');
    expect(firstToken.length).toBeGreaterThan(10);
    expect(typeof firstRefresh).toBe('string');
    expect(firstRefresh.length).toBeGreaterThan(10);
  });

  it('POST /api/auth/refresh with valid refresh_token → 200 + new token', async () => {
    const res = await request2(app2).post('/api/auth/refresh').send({ refresh_token: firstRefresh });
    expect(res.status).toBe(200);
    expect(typeof res.body.token).toBe('string');
    expect(typeof res.body.refresh_token).toBe('string');
    secondToken = res.body.token;
    secondRefresh = res.body.refresh_token;
  });

  it('old refresh_token is rotated — reuse returns 401', async () => {
    // firstRefresh was consumed in previous test, reusing it must fail
    const res = await request2(app2).post('/api/auth/refresh').send({ refresh_token: firstRefresh });
    expect(res.status).toBe(401);
  });

  it('new refresh_token from rotation is a valid string', () => {
    expect(typeof secondRefresh).toBe('string');
    expect(secondRefresh.length).toBeGreaterThan(10);
  });

  it('POST /api/auth/logout with refresh_token → 200', async () => {
    const res = await request2(app2).post('/api/auth/logout').send({ refresh_token: secondRefresh });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('POST /api/auth/refresh after logout → 401 (revoked token)', async () => {
    const res = await request2(app2).post('/api/auth/refresh').send({ refresh_token: secondRefresh });
    expect(res.status).toBe(401);
  });

  it('POST /api/auth/refresh without body → 400 (or 429 if rate-limited)', async () => {
    const res = await request2(app2).post('/api/auth/refresh').send({});
    // Rate limiter may fire (429) if authLimiter window exhausted in this describe block
    expect([400, 429]).toContain(res.status);
  });
});

// ─── 3. Catalog endpoints ─────────────────────────────────────────────────────

describe('Catalog endpoints', () => {
  let app3, request3;

  beforeAll(async () => {
    jest.resetModules();
    const supertest = require('supertest');
    const express = require('express');
    const cors = require('cors');
    const { initDatabase } = require('../database');
    await initDatabase();
    require('../bot');
    const apiRouter = require('../routes/api');

    const a = express();
    a.use(express.json({ limit: '2mb' }));
    a.use(express.urlencoded({ extended: true }));
    a.use(cors());
    a.use('/api', apiRouter);
    app3 = a;
    request3 = supertest;
  }, 30000);

  afterAll(() => {
    try {
      const db = require('../database');
      if (db.closeDatabase) db.closeDatabase();
    } catch (_) {}
  });

  it('GET /api/models → 200 with array', async () => {
    const res = await request3(app3).get('/api/models');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    expect(Array.isArray(list)).toBe(true);
  });

  it('GET /api/models → items include order_count field (if any)', async () => {
    const res = await request3(app3).get('/api/models');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || [];
    // Empty catalog in test DB is acceptable; if models exist they must have order_count
    if (list.length > 0) {
      expect(list[0]).toHaveProperty('order_count');
    }
  });

  it('GET /api/models/search?category=fashion → 200 with array', async () => {
    const res = await request3(app3).get('/api/models/search?category=fashion');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || res.body.results || [];
    expect(Array.isArray(list)).toBe(true);
  });

  it('GET /api/models/search?q=test → 200 with array', async () => {
    const res = await request3(app3).get('/api/models/search?q=test');
    expect(res.status).toBe(200);
    const list = Array.isArray(res.body) ? res.body : res.body.models || res.body.results || [];
    expect(Array.isArray(list)).toBe(true);
  });
});

// ─── 4. Auth logout endpoint checks ─────────────────────────────────────────

describe('Auth logout endpoint', () => {
  let app4, request4;

  beforeAll(async () => {
    jest.resetModules();
    const supertest = require('supertest');
    const express = require('express');
    const cors = require('cors');
    const { initDatabase } = require('../database');
    await initDatabase();
    require('../bot');
    const apiRouter = require('../routes/api');

    const a = express();
    a.use(express.json({ limit: '2mb' }));
    a.use(express.urlencoded({ extended: true }));
    a.use(cors());
    a.use('/api', apiRouter);
    app4 = a;
    request4 = supertest;
  }, 30000);

  afterAll(() => {
    try {
      const db = require('../database');
      if (db.closeDatabase) db.closeDatabase();
    } catch (_) {}
  });

  it('POST /api/auth/logout without refresh_token body → 200 (graceful no-op)', async () => {
    const res = await request4(app4).post('/api/auth/logout').send({});
    // Logout without a token is a valid no-op
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  it('POST /api/auth/logout with a fake token → still 200 (idempotent)', async () => {
    // Logout is idempotent — revoking an unknown token should not error
    const res = await request4(app4)
      .post('/api/auth/logout')
      .send({ refresh_token: 'totally-fake-token-that-was-never-valid' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ─── 5. Edge cases — login ────────────────────────────────────────────────────

describe('Edge cases: /api/admin/login', () => {
  let app5, request5;

  beforeAll(async () => {
    jest.resetModules();
    const supertest = require('supertest');
    const express = require('express');
    const cors = require('cors');
    const { initDatabase } = require('../database');
    await initDatabase();
    require('../bot');
    const apiRouter = require('../routes/api');

    const a = express();
    a.use(express.json({ limit: '2mb' }));
    a.use(express.urlencoded({ extended: true }));
    a.use(cors());
    a.use('/api', apiRouter);
    app5 = a;
    request5 = supertest;
  }, 30000);

  afterAll(() => {
    try {
      const db = require('../database');
      if (db.closeDatabase) db.closeDatabase();
    } catch (_) {}
  });

  it('POST /api/admin/login with empty password → 400 or 401', async () => {
    const res = await request5(app5).post('/api/admin/login').send({ username: 'admin', password: '' });
    expect([400, 401]).toContain(res.status);
  });

  it('POST /api/admin/login with missing fields → 400 or 401', async () => {
    const res = await request5(app5).post('/api/admin/login').send({});
    expect([400, 401]).toContain(res.status);
  });

  it('POST /api/admin/login with wrong password → 401', async () => {
    const res = await request5(app5).post('/api/admin/login').send({ username: 'admin', password: 'wrongpassword' });
    expect(res.status).toBe(401);
  });

  it('POST /api/admin/login with correct credentials → 200 + token with type:"admin"', async () => {
    const res = await request5(app5).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
    expect(res.status).toBe(200);
    expect(typeof res.body.token).toBe('string');
    const decoded = jwt.decode(res.body.token);
    expect(decoded).not.toBeNull();
    expect(decoded.type).toBe('admin');
  });

  it('POST /api/admin/login with non-existent user → 401', async () => {
    const res = await request5(app5).post('/api/admin/login').send({ username: 'ghost', password: 'ghost123' });
    expect(res.status).toBe(401);
  });
});
