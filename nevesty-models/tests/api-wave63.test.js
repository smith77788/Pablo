'use strict';
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

let app;

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
  if (bot) apiRouter.setBot(bot);
  a.use('/api', apiRouter);
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;
}, 15000);

afterAll(() => {
  if (app && app.close) app.close();
});

// ─── 1. Budget estimate API ────────────────────────────────────────────────────
describe('GET /api/budget-estimate', () => {
  it('returns 200', async () => {
    const res = await request(app).get('/api/budget-estimate');
    expect(res.status).toBe(200);
  });

  it('returns budget, currency, tips structure', async () => {
    const res = await request(app).get('/api/budget-estimate');
    expect(res.body).toHaveProperty('budget');
    expect(res.body.budget).toHaveProperty('min');
    expect(res.body.budget).toHaveProperty('max');
    expect(res.body.budget).toHaveProperty('recommended');
    expect(res.body).toHaveProperty('currency');
    expect(res.body).toHaveProperty('tips');
  });

  it('корпоратив event_type returns reasonable budget values', async () => {
    const res = await request(app)
      .get('/api/budget-estimate?event_type=%D0%BA%D0%BE%D1%80%D0%BF%D0%BE%D1%80%D0%B0%D1%82%D0%B8%D0%B2&model_count=2&duration_hours=6');
    expect(res.status).toBe(200);
    const { min, max, recommended } = res.body.budget;
    expect(typeof min).toBe('number');
    expect(typeof max).toBe('number');
    expect(typeof recommended).toBe('number');
    expect(min).toBeGreaterThan(0);
    expect(max).toBeGreaterThan(0);
    expect(recommended).toBeGreaterThan(0);
  });

  it('min < recommended < max for корпоратив event', async () => {
    const res = await request(app)
      .get('/api/budget-estimate?event_type=%D0%BA%D0%BE%D1%80%D0%BF%D0%BE%D1%80%D0%B0%D1%82%D0%B8%D0%B2&model_count=2&duration_hours=6');
    const { min, max, recommended } = res.body.budget;
    expect(min).toBeLessThan(recommended);
    expect(recommended).toBeLessThan(max);
  });

  it('currency is RUB', async () => {
    const res = await request(app).get('/api/budget-estimate');
    expect(res.body.currency).toBe('RUB');
  });

  it('tips is an array', async () => {
    const res = await request(app).get('/api/budget-estimate');
    expect(Array.isArray(res.body.tips)).toBe(true);
  });

  it('фотосессия returns different range than показ', async () => {
    const foto = await request(app)
      .get('/api/budget-estimate?event_type=%D1%84%D0%BE%D1%82%D0%BE%D1%81%D0%B5%D1%81%D1%81%D0%B8%D1%8F');
    const pokaz = await request(app)
      .get('/api/budget-estimate?event_type=%D0%BF%D0%BE%D0%BA%D0%B0%D0%B7');
    expect(foto.body.budget.min).not.toBe(pokaz.body.budget.min);
    expect(foto.body.budget.max).not.toBe(pokaz.body.budget.max);
  });

  it('without event_type returns defaults (200 and valid budget)', async () => {
    const res = await request(app).get('/api/budget-estimate');
    expect(res.status).toBe(200);
    const { min, max, recommended } = res.body.budget;
    expect(min).toBeGreaterThan(0);
    expect(max).toBeGreaterThan(min);
    expect(recommended).toBeGreaterThan(min);
    expect(recommended).toBeLessThan(max);
  });
});

// ─── 2. Budget estimate in booking.html ───────────────────────────────────────
describe('booking.html budget estimate UI', () => {
  const bookingHtmlPath = path.join(__dirname, '../public/booking.html');

  it('booking.html file exists', () => {
    expect(fs.existsSync(bookingHtmlPath)).toBe(true);
  });

  it('Contains budget-estimate-hint element', () => {
    const content = fs.readFileSync(bookingHtmlPath, 'utf8');
    expect(content).toContain('budget-estimate-hint');
  });

  it('Contains clearBudgetEstimate function reference', () => {
    const content = fs.readFileSync(bookingHtmlPath, 'utf8');
    expect(content).toContain('clearBudgetEstimate');
  });
});

describe('public/js/booking.js budget estimate API call', () => {
  const bookingJsPath = path.join(__dirname, '../public/js/booking.js');

  it('booking.js file exists', () => {
    expect(fs.existsSync(bookingJsPath)).toBe(true);
  });

  it('Contains /budget-estimate API call', () => {
    const content = fs.readFileSync(bookingJsPath, 'utf8');
    expect(content).toContain('/budget-estimate');
  });

  it('Contains clearBudgetEstimate function definition', () => {
    const content = fs.readFileSync(bookingJsPath, 'utf8');
    expect(content).toContain('clearBudgetEstimate');
  });
});

// ─── 3. Decision tracker (factory) ────────────────────────────────────────────
describe('factory/agents/decision_tracker.py', () => {
  const dtPath = path.join(__dirname, '../../factory/agents/decision_tracker.py');

  it('decision_tracker.py file exists', () => {
    expect(fs.existsSync(dtPath)).toBe(true);
  });

  it('Contains DecisionTracker class', () => {
    const content = fs.readFileSync(dtPath, 'utf8');
    expect(content).toContain('class DecisionTracker');
  });

  it('Contains get_execution_summary method', () => {
    const content = fs.readFileSync(dtPath, 'utf8');
    expect(content).toContain('get_execution_summary');
  });

  it('Contains generate_accountability_report method', () => {
    const content = fs.readFileSync(dtPath, 'utf8');
    expect(content).toContain('generate_accountability_report');
  });
});

// ─── 4. Bot search improvements ───────────────────────────────────────────────
describe('bot.js search improvements', () => {
  const botPath = path.join(__dirname, '../bot.js');

  it('bot.js file exists', () => {
    expect(fs.existsSync(botPath)).toBe(true);
  });

  it('Contains "← Изменить поиск" text', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('← Изменить поиск');
  });

  it('Contains search_go callback reference', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('search_go');
  });

  it('Contains srch_view_ prefix', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('srch_view_');
  });
});

// ─── 5. Admin analytics page ──────────────────────────────────────────────────
describe('public/admin/analytics.html', () => {
  const analyticsPath = path.join(__dirname, '../public/admin/analytics.html');

  it('analytics.html file exists', () => {
    expect(fs.existsSync(analyticsPath)).toBe(true);
  });

  it('Contains model performance or top models section', () => {
    const content = fs.readFileSync(analyticsPath, 'utf8');
    expect(content).toMatch(/model performance|top.models|top-models|topModels|Model performance/i);
  });
});
