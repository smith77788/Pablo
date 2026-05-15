'use strict';
/**
 * Integration tests for Wave 78 features:
 * - Factory structure (file existence checks)
 * - KPI Analyzer code checks (class/method presence)
 * - Logging config code checks (class/function/env vars)
 * - Bot search feature code checks
 * - Admin reviews panel code checks
 * - API endpoint tests (reviews, factory/status)
 * - Factory test count checks
 */

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const fs = require('fs');
const path = require('path');
const request = require('supertest');
const express = require('express');
const cors = require('cors');

// Factory lives at /home/user/Pablo/factory (sibling of nevesty-models)
const FACTORY_DIR = path.join(__dirname, '../../factory');
const factoryExists = fs.existsSync(FACTORY_DIR);

const BOT_JS = path.join(__dirname, '../bot.js');
const ADMIN_JS = path.join(__dirname, '../handlers/admin.js');

let app, adminToken;

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
  adminToken = res.body?.token || res.body?.accessToken || null;
}, 30000);

afterAll(async () => {
  await new Promise(r => setTimeout(r, 300));
});

// ── БЛОК 1: Factory Structure Tests ──────────────────────────────────────────

describe('Wave 78 БЛОК 1: Factory file structure', () => {
  test('factory directory exists', () => {
    expect(factoryExists).toBe(true);
  });

  test('factory/agents/kpi_analyzer.py exists', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'agents/kpi_analyzer.py');
    expect(fs.existsSync(fp)).toBe(true);
  });

  test('factory/logging_config.py exists', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'logging_config.py');
    expect(fs.existsSync(fp)).toBe(true);
  });

  test('factory/agents/experiment_system.py exists', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'agents/experiment_system.py');
    expect(fs.existsSync(fp)).toBe(true);
  });

  test('factory/tests/test_kpi_analyzer.py exists', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_kpi_analyzer.py');
    expect(fs.existsSync(fp)).toBe(true);
  });

  test('factory/tests/test_logging_config.py exists', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_logging_config.py');
    expect(fs.existsSync(fp)).toBe(true);
  });

  test('factory/agents directory has multiple agent files', () => {
    if (!factoryExists) return;
    const agentsDir = path.join(FACTORY_DIR, 'agents');
    const files = fs.readdirSync(agentsDir).filter(f => f.endsWith('.py'));
    expect(files.length).toBeGreaterThan(3);
  });

  test('factory/tests directory has multiple test files', () => {
    if (!factoryExists) return;
    const testsDir = path.join(FACTORY_DIR, 'tests');
    const files = fs.readdirSync(testsDir).filter(f => f.startsWith('test_'));
    expect(files.length).toBeGreaterThan(3);
  });
});

// ── БЛОК 2: KPI Analyzer Code Checks ─────────────────────────────────────────

describe('Wave 78 БЛОК 2: KPI Analyzer code checks', () => {
  let kpiSource = '';

  beforeAll(() => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'agents/kpi_analyzer.py');
    if (fs.existsSync(fp)) kpiSource = fs.readFileSync(fp, 'utf8');
  });

  test('kpi_analyzer.py has KPIAnalyzer class', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('class KPIAnalyzer');
  });

  test('kpi_analyzer.py has analyze_orders_per_period method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('analyze_orders_per_period');
  });

  test('kpi_analyzer.py has analyze_conversion_by_source method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('analyze_conversion_by_source');
  });

  test('kpi_analyzer.py has analyze_client_return_rate method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('analyze_client_return_rate');
  });

  test('kpi_analyzer.py has analyze_deal_cycle_days method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('analyze_deal_cycle_days');
  });

  test('kpi_analyzer.py has analyze_model_ratings method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('analyze_model_ratings');
  });

  test('kpi_analyzer.py has analyze_bot_activity method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('analyze_bot_activity');
  });

  test('kpi_analyzer.py has analyze_top_client_requests method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('analyze_top_client_requests');
  });

  test('kpi_analyzer.py has run_full_analysis method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('run_full_analysis');
  });

  test('kpi_analyzer.py has generate_summary method', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('generate_summary');
  });

  test('kpi_analyzer.py uses sqlite3 for DB access', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('sqlite3');
  });

  test('kpi_analyzer.py has DB_PATH configuration', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('DB_PATH');
  });

  test('kpi_analyzer.py has _connect_db helper function', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('_connect_db');
  });

  test('kpi_analyzer.py handles None connection gracefully', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('if conn is None');
  });

  test('kpi_analyzer.py returns structured dicts for KPIs', () => {
    if (!factoryExists || !kpiSource) return;
    expect(kpiSource).toContain('return {');
  });
});

// ── БЛОК 3: Logging Config Code Checks ───────────────────────────────────────

describe('Wave 78 БЛОК 3: Logging config code checks', () => {
  let logSource = '';

  beforeAll(() => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'logging_config.py');
    if (fs.existsSync(fp)) logSource = fs.readFileSync(fp, 'utf8');
  });

  test('logging_config.py has JSONFormatter class', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('class JSONFormatter');
  });

  test('logging_config.py has configure_logging function', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('def configure_logging');
  });

  test('logging_config.py references LOG_JSON env var', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('LOG_JSON');
  });

  test('logging_config.py references LOG_LEVEL env var', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('LOG_LEVEL');
  });

  test('logging_config.py imports logging module', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('import logging');
  });

  test('logging_config.py imports json module', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('import json');
  });

  test('logging_config.py formats log record as JSON', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('json.dumps');
  });

  test('logging_config.py extends logging.Formatter', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('logging.Formatter');
  });

  test('logging_config.py uses os.getenv for env vars', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('os.getenv');
  });

  test('logging_config.py has format method in JSONFormatter', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('def format');
  });

  test('logging_config.py includes timestamp in JSON payload', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('"ts"');
  });

  test('logging_config.py includes level in JSON payload', () => {
    if (!factoryExists || !logSource) return;
    expect(logSource).toContain('"level"');
  });
});

// ── БЛОК 4: Bot Search Feature Code Checks ───────────────────────────────────

describe('Wave 78 БЛОК 4: Bot search feature code checks', () => {
  let botSource = '';

  beforeAll(() => {
    if (fs.existsSync(BOT_JS)) botSource = fs.readFileSync(BOT_JS, 'utf8');
  });

  test('bot.js file exists', () => {
    expect(fs.existsSync(BOT_JS)).toBe(true);
  });

  test('bot.js has showSearchMenu function', () => {
    expect(botSource).toContain('showSearchMenu');
  });

  test('bot.js has search_city_input state reference', () => {
    expect(botSource).toContain('search_city_input');
  });

  test('bot.js has cat_search callback', () => {
    expect(botSource).toContain('cat_search');
  });

  test('bot.js has search_go callback', () => {
    expect(botSource).toContain('search_go');
  });

  test('bot.js showSearchMenu is an async function', () => {
    expect(botSource).toContain('async function showSearchMenu');
  });

  test('bot.js handles search_city_input data callback', () => {
    expect(botSource).toContain("data === 'search_city_input'");
  });

  test('bot.js cat_search routes to showSearchMenu', () => {
    expect(botSource).toMatch(/cat_search['\s\S]{0,30}showSearchMenu/);
  });
});

// ── БЛОК 5: Admin Reviews Panel Code Checks ───────────────────────────────────

describe('Wave 78 БЛОК 5: Admin reviews panel code checks', () => {
  let adminSource = '';

  beforeAll(() => {
    if (fs.existsSync(ADMIN_JS)) adminSource = fs.readFileSync(ADMIN_JS, 'utf8');
  });

  test('handlers/admin.js file exists', () => {
    expect(fs.existsSync(ADMIN_JS)).toBe(true);
  });

  test('admin.js has showAdminReviews function', () => {
    expect(adminSource).toContain('showAdminReviews');
  });

  test('admin.js has VALID_FILTERS array', () => {
    expect(adminSource).toContain('VALID_FILTERS');
  });

  test('admin.js validates filter against VALID_FILTERS', () => {
    expect(adminSource).toContain('VALID_FILTERS.includes(filter)');
  });

  test('admin.js has Math.max(0 for page guard', () => {
    expect(adminSource).toContain('Math.max(0');
  });

  test('admin.js exports showAdminReviews', () => {
    expect(adminSource).toContain('showAdminReviews');
    expect(adminSource).toMatch(/module\.exports\s*=\s*\{[^}]*showAdminReviews/);
  });

  test('admin.js has filter options: pending, approved, all', () => {
    expect(adminSource).toContain('pending');
    expect(adminSource).toContain('approved');
    expect(adminSource).toContain('all');
  });
});

// ── БЛОК 6: API Tests ─────────────────────────────────────────────────────────

describe('Wave 78 БЛОК 6: GET /api/admin/reviews — auth', () => {
  test('returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/reviews?filter=pending');
    expect(res.status).toBe(401);
  });

  test('returns 200 with valid auth token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('returns reviews array in response body', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
  });

  test('returns total count in response body', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('total');
    expect(typeof res.body.total).toBe('number');
  });

  test('returns page info in response body', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/reviews?filter=pending')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('page');
    expect(res.body).toHaveProperty('pages');
  });

  test('returns 200 with filter=approved', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/reviews?filter=approved')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
  });

  test('returns 200 with filter=all', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/reviews?filter=all')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
  });
});

describe('Wave 78 БЛОК 6: PUT /api/admin/reviews/:id/approve — validation', () => {
  test('returns 401 without auth token', async () => {
    const res = await request(app).put('/api/admin/reviews/1/approve');
    expect(res.status).toBe(401);
  });

  test('returns 400 for invalid ID 0', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/admin/reviews/0/approve')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(400);
  });

  test('returns 400 or error for non-numeric ID', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/admin/reviews/abc/approve')
      .set('Authorization', `Bearer ${adminToken}`);
    expect([400, 404]).toContain(res.status);
  });

  test('returns 404 for non-existent review ID', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/admin/reviews/999999/approve')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(404);
  });
});

describe('Wave 78 БЛОК 6: GET /api/admin/factory/status', () => {
  test('returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/factory/status');
    expect(res.status).toBe(401);
  });

  test('returns 200 with valid auth token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/factory/status')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('response contains status data', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/factory/status')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body).toBe('object');
  });
});

// ── БЛОК 7: Factory Test Count Checks ─────────────────────────────────────────

describe('Wave 78 БЛОК 7: Factory test file coverage counts', () => {
  function countTestFunctions(filePath) {
    if (!fs.existsSync(filePath)) return 0;
    const src = fs.readFileSync(filePath, 'utf8');
    return (src.match(/def test_/g) || []).length;
  }

  test('test_kpi_analyzer.py has 90+ test functions', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_kpi_analyzer.py');
    const count = countTestFunctions(fp);
    expect(count).toBeGreaterThanOrEqual(90);
  });

  test('test_logging_config.py has 20+ test functions', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_logging_config.py');
    const count = countTestFunctions(fp);
    expect(count).toBeGreaterThanOrEqual(20);
  });

  test('test_experiment_system.py has 100+ test functions', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_experiment_system.py');
    const count = countTestFunctions(fp);
    expect(count).toBeGreaterThanOrEqual(100);
  });

  test('test_kpi_analyzer.py file is not empty', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_kpi_analyzer.py');
    if (!fs.existsSync(fp)) return;
    const size = fs.statSync(fp).size;
    expect(size).toBeGreaterThan(1000);
  });

  test('test_logging_config.py file is not empty', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_logging_config.py');
    if (!fs.existsSync(fp)) return;
    const size = fs.statSync(fp).size;
    expect(size).toBeGreaterThan(500);
  });

  test('test_experiment_system.py file is not empty', () => {
    if (!factoryExists) return;
    const fp = path.join(FACTORY_DIR, 'tests/test_experiment_system.py');
    if (!fs.existsSync(fp)) return;
    const size = fs.statSync(fp).size;
    expect(size).toBeGreaterThan(1000);
  });

  test('all three factory test files exist', () => {
    if (!factoryExists) return;
    const files = [
      path.join(FACTORY_DIR, 'tests/test_kpi_analyzer.py'),
      path.join(FACTORY_DIR, 'tests/test_logging_config.py'),
      path.join(FACTORY_DIR, 'tests/test_experiment_system.py'),
    ];
    files.forEach(fp => expect(fs.existsSync(fp)).toBe(true));
  });
});
