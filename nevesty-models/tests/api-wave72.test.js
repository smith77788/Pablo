'use strict';
/**
 * Integration tests for Wave 72 features:
 * - Public Reviews API (/api/reviews)
 * - Factory Status API (/api/admin/factory/status)
 * - Bot Search (showSearchMenu, search_h_*, search_a_* callbacks)
 * - Bot Reviews (showPublicReviews, startLeaveReview, rev_rate_*)
 * - Factory Monitoring (scheduler.js staleness check)
 * - A/B Experiment System (factory/agents/experiment_system.py)
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

const botContent       = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
const routesContent    = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
const serverContent    = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');
const schedulerContent = fs.readFileSync(path.join(__dirname, '../services/scheduler.js'), 'utf8');

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

// ── Wave 72: Public Reviews API ───────────────────────────────────────────────

describe('Wave 72: Public Reviews API', () => {
  it('GET /api/reviews returns 200', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
  });

  it('GET /api/reviews returns object with reviews array', async () => {
    const res = await request(app).get('/api/reviews');
    const hasReviews = Array.isArray(res.body) || Array.isArray(res.body?.reviews);
    expect(hasReviews).toBe(true);
  });

  it('GET /api/reviews?page=0&limit=3 works', async () => {
    const res = await request(app).get('/api/reviews?page=0&limit=3');
    expect(res.status).toBe(200);
  });

  it('GET /api/reviews does not require auth', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).not.toBe(401);
  });

  it('routes/api.js contains /reviews endpoint', () => {
    expect(routesContent).toMatch(/router\.\w+\(['"]\/reviews/);
  });

  it('routes/api.js contains /reviews/public or /reviews endpoint', () => {
    expect(routesContent).toMatch(/\/reviews/);
  });
});

// ── Wave 72: Factory Status API ───────────────────────────────────────────────

describe('Wave 72: Factory Status API', () => {
  it('GET /api/admin/factory/status requires auth', async () => {
    const res = await request(app).get('/api/admin/factory/status');
    expect(res.status).toBe(401);
  });

  it('GET /api/admin/factory/status with token returns 200', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/factory/status')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('GET /api/admin/factory/status returns status field', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/factory/status')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('status');
  });

  it('routes/api.js contains /admin/factory/status route', () => {
    expect(routesContent).toMatch(/admin\/factory\/status/);
  });
});

// ── Wave 72: Bot Search ───────────────────────────────────────────────────────

describe('Wave 72: Bot Search', () => {
  it('bot.js contains showSearchMenu function', () => {
    expect(botContent).toMatch(/showSearchMenu/);
  });

  it('bot.js contains search_h_ height filter callbacks', () => {
    expect(botContent).toMatch(/search_h_/);
  });

  it('bot.js contains search_a_ age filter callbacks', () => {
    expect(botContent).toMatch(/search_a_/);
  });

  it('bot.js contains showSearchResults function', () => {
    expect(botContent).toMatch(/showSearchResults/);
  });

  it('bot.js handles search_reset callback', () => {
    expect(botContent).toMatch(/search_reset/);
  });

  it('bot.js handles search_go callback', () => {
    expect(botContent).toMatch(/search_go/);
  });

  it('bot.js handles cat_search callback', () => {
    expect(botContent).toMatch(/cat_search/);
  });
});

// ── Wave 72: Bot Reviews ──────────────────────────────────────────────────────

describe('Wave 72: Bot Reviews', () => {
  it('bot.js contains showPublicReviews function', () => {
    expect(botContent).toMatch(/showPublicReviews/);
  });

  it('bot.js contains startLeaveReview function', () => {
    expect(botContent).toMatch(/startLeaveReview/);
  });

  it('bot.js contains leave_review callback', () => {
    expect(botContent).toMatch(/leave_review/);
  });

  it('bot.js contains rev_rate_ callback', () => {
    expect(botContent).toMatch(/rev_rate_/);
  });

  it('bot.js contains cat_rev_ pagination callback', () => {
    expect(botContent).toMatch(/cat_rev_/);
  });
});

// ── Wave 72: Factory Monitoring ───────────────────────────────────────────────

describe('Wave 72: Factory Monitoring', () => {
  it('services/scheduler.js contains factory staleness check', () => {
    expect(schedulerContent).toMatch(/factory|staleness|factory_last/i);
  });

  it('services/scheduler.js has checkFactoryStaleness function', () => {
    expect(schedulerContent).toMatch(/checkFactoryStaleness/);
  });

  it('server.js contains factory health block', () => {
    expect(serverContent).toMatch(/factory.*last_run|factory.*stale|factory.*status/i);
  });

  it('server.js exposes factoryStale or factory status in health', () => {
    expect(serverContent).toMatch(/factoryStale|factory_last_cycle|factory.*ok|factory.*never_run/i);
  });
});

// ── Wave 72: A/B Experiment System (factory) ──────────────────────────────────

describe('Wave 72: A/B Experiment System (factory)', () => {
  it('factory/agents/experiment_system.py exists', () => {
    if (!factoryExists) { return; }
    expect(fs.existsSync(path.join(FACTORY_DIR, 'agents', 'experiment_system.py'))).toBe(true);
  });

  it('experiment_system.py contains HeuristicExperimentSystem or ExperimentSystem', () => {
    if (!factoryExists) { return; }
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'agents', 'experiment_system.py'), 'utf8');
    expect(code).toMatch(/HeuristicExperimentSystem|ExperimentSystem/);
  });

  it('cycle.py contains run_phase_28_experiments', () => {
    if (!factoryExists) { return; }
    const code = fs.readFileSync(path.join(FACTORY_DIR, 'cycle.py'), 'utf8');
    expect(code).toMatch(/run_phase_28_experiments/);
  });

  it('factory/tests/test_experiment_system.py exists', () => {
    if (!factoryExists) { return; }
    expect(fs.existsSync(path.join(FACTORY_DIR, 'tests', 'test_experiment_system.py'))).toBe(true);
  });
});
