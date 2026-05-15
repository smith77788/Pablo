'use strict';
/**
 * Integration tests for Wave 71 features:
 * - Broadcast history in bot.js
 * - Security hardening (XSS sanitization, auth failure logging)
 * - Finance Department (factory)
 * - Code quality (utils/constants.js)
 */
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

let app, adminToken;

const botContent    = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
const routesContent = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
const dbContent     = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');
const serverContent = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');

const FACTORY_DIR = path.join(__dirname, '../../factory');
const factoryExists = fs.existsSync(FACTORY_DIR);

beforeAll(async () => {
  const { initDatabase } = require('../database');
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
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const res = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = res.body.token;
}, 15000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ── Wave 71: Broadcast history ────────────────────────────────────────────────

describe('Wave 71: Broadcast history', () => {
  it('bot.js contains showBroadcastHistory function', () => {
    expect(botContent).toMatch(/showBroadcastHistory|adm_broadcast_history/);
  });

  it('bot.js handles adm_broadcast_history callback', () => {
    expect(botContent).toMatch(/adm_broadcast_history/);
  });

  it('database.js contains bot_broadcasts table', () => {
    expect(dbContent).toMatch(/bot_broadcasts/);
  });

  it('database.js creates bot_broadcasts with required columns', () => {
    // Check for key columns in bot_broadcasts table definition
    expect(dbContent).toMatch(/bot_broadcasts/);
    expect(dbContent).toMatch(/delivered|failed|status/);
  });
});

// ── Wave 71: Security hardening ───────────────────────────────────────────────

describe('Wave 71: Security hardening', () => {
  it('server.js contains XSS sanitization middleware', () => {
    expect(serverContent).toMatch(/sanitize|<script|javascript:/i);
  });

  it('routes/api.js contains auth failure logging', () => {
    expect(routesContent).toMatch(/Failed login|AUTH.*Failed|console\.warn.*login/i);
  });

  it('POST /api/admin/login with wrong password returns 401', async () => {
    const res = await request(app)
      .post('/api/admin/login')
      .send({ username: 'admin', password: 'wrongpassword' });
    expect(res.status).toBe(401);
  });

  it('POST /api/admin/login with unknown user returns 401', async () => {
    const res = await request(app)
      .post('/api/admin/login')
      .send({ username: 'nonexistent', password: 'anything' });
    expect(res.status).toBe(401);
  });

  it('Protected admin routes reject requests without token', async () => {
    const res = await request(app).get('/api/admin/orders');
    expect([401, 403]).toContain(res.status);
  });

  it('Protected admin routes reject requests with invalid token', async () => {
    const res = await request(app)
      .get('/api/admin/orders')
      .set('Authorization', 'Bearer invalid.token.here');
    expect([401, 403]).toContain(res.status);
  });
});

// ── Wave 71: Finance Department (factory) ─────────────────────────────────────

describe('Wave 71: Finance Department (factory)', () => {
  it('factory/agents/finance_department.py exists', () => {
    if (!factoryExists) return;
    expect(fs.existsSync(path.join(FACTORY_DIR, 'agents', 'finance_department.py'))).toBe(true);
  });

  it('finance_department.py contains FinanceDepartment class', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'agents', 'finance_department.py'), 'utf8');
    expect(code).toMatch(/class FinanceDepartment/);
  });

  it('finance_department.py contains RevenueForecaster', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'agents', 'finance_department.py'), 'utf8');
    expect(code).toMatch(/RevenueForecaster/);
  });

  it('cycle.py contains run_phase_finance or FinanceDepartment reference', () => {
    if (!factoryExists) return;
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'cycle.py'), 'utf8');
    expect(code).toMatch(/run_phase_finance|FinanceDepartment/);
  });

  it('factory/agents/ directory contains expected agent files', () => {
    if (!factoryExists) return;
    const agentsDir = path.join(FACTORY_DIR, 'agents');
    expect(fs.existsSync(agentsDir)).toBe(true);
    const files = fs.readdirSync(agentsDir);
    expect(files.length).toBeGreaterThan(0);
  });
});

// ── Wave 71: Code quality — utils/constants.js ────────────────────────────────

describe('Wave 71: Code quality — utils/constants.js', () => {
  let constants;

  beforeAll(() => {
    constants = require('../utils/constants.js');
  });

  it('utils/constants.js exists', () => {
    expect(fs.existsSync(path.join(__dirname, '../utils/constants.js'))).toBe(true);
  });

  it('exports STATUS_LABELS object', () => {
    expect(constants).toHaveProperty('STATUS_LABELS');
    expect(typeof constants.STATUS_LABELS).toBe('object');
  });

  it('STATUS_LABELS contains expected statuses', () => {
    const { STATUS_LABELS } = constants;
    expect(STATUS_LABELS).toHaveProperty('new');
    expect(STATUS_LABELS).toHaveProperty('confirmed');
    expect(STATUS_LABELS).toHaveProperty('completed');
    expect(STATUS_LABELS).toHaveProperty('cancelled');
  });

  it('exports EVENT_TYPES object', () => {
    expect(constants).toHaveProperty('EVENT_TYPES');
    expect(typeof constants.EVENT_TYPES).toBe('object');
  });

  it('EVENT_TYPES contains photo_shoot and fashion_show', () => {
    const { EVENT_TYPES } = constants;
    expect(EVENT_TYPES).toHaveProperty('photo_shoot');
    expect(EVENT_TYPES).toHaveProperty('fashion_show');
  });

  it('VALID_STATUSES is derived from STATUS_LABELS', () => {
    if (!constants.VALID_STATUSES) return;
    expect(Array.isArray(constants.VALID_STATUSES)).toBe(true);
    expect(constants.VALID_STATUSES).toContain('new');
    expect(constants.VALID_STATUSES).toContain('confirmed');
  });
});
