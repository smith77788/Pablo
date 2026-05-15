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

// ─── 1. GET /api/models/my-orders ─────────────────────────────────────────────
describe('GET /api/models/my-orders', () => {
  it('Returns 200 with orders array for a valid name', async () => {
    const res = await request(app).get('/api/models/my-orders?name=Анна');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('orders');
    expect(Array.isArray(res.body.orders)).toBe(true);
  });

  it('Returns orders array structure when name provided', async () => {
    const res = await request(app).get('/api/models/my-orders?name=Тест');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('orders');
  });

  it('Returns empty orders array and message for empty name param', async () => {
    const res = await request(app).get('/api/models/my-orders?name=');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(res.body.orders.length).toBe(0);
    expect(res.body).toHaveProperty('message');
  });

  it('Returns empty orders array and message when name param is missing', async () => {
    const res = await request(app).get('/api/models/my-orders');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(res.body.orders.length).toBe(0);
    expect(res.body).toHaveProperty('message');
  });

  it('Returns empty orders array for unknown name', async () => {
    const res = await request(app).get('/api/models/my-orders?name=НесуществующаяМодель99999');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(res.body.orders.length).toBe(0);
  });

  it('Short name (1 char) returns empty orders with message', async () => {
    const res = await request(app).get('/api/models/my-orders?name=А');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.orders)).toBe(true);
    expect(res.body.orders.length).toBe(0);
    expect(res.body).toHaveProperty('message');
  });
});

// ─── 2. Model Personal Cabinet Page ───────────────────────────────────────────
describe('Model personal cabinet page', () => {
  const cabinetPath = path.join(__dirname, '../public/model-cabinet.html');

  it('File /public/model-cabinet.html exists', () => {
    expect(fs.existsSync(cabinetPath)).toBe(true);
  });

  it('Contains a name input field', () => {
    const html = fs.readFileSync(cabinetPath, 'utf8');
    expect(html).toMatch(/type=["']text["']|modelNameInput/i);
  });

  it('Contains a results container element', () => {
    const html = fs.readFileSync(cabinetPath, 'utf8');
    expect(html).toMatch(/id=["']results["']/i);
  });

  it('Page title mentions "кабинет" or "модел"', () => {
    const html = fs.readFileSync(cabinetPath, 'utf8');
    expect(html.toLowerCase()).toMatch(/кабинет|модел/);
  });

  it('References /api/models/my-orders endpoint', () => {
    const html = fs.readFileSync(cabinetPath, 'utf8');
    expect(html).toContain('my-orders');
  });
});

// ─── 3. Factory CEO Telegram Report ───────────────────────────────────────────
describe('Factory CEO Telegram report in cycle.py', () => {
  const cyclePath = path.join(__dirname, '../../factory/cycle.py');

  it('cycle.py exists', () => {
    expect(fs.existsSync(cyclePath)).toBe(true);
  });

  it('Contains _send_telegram_to_admins function', () => {
    const content = fs.readFileSync(cyclePath, 'utf8');
    expect(content).toContain('_send_telegram_to_admins');
  });

  it('Reads TELEGRAM_BOT_TOKEN from env', () => {
    const content = fs.readFileSync(cyclePath, 'utf8');
    expect(content).toContain('TELEGRAM_BOT_TOKEN');
  });

  it('Reads ADMIN_TELEGRAM_IDS from env', () => {
    const content = fs.readFileSync(cyclePath, 'utf8');
    expect(content).toContain('ADMIN_TELEGRAM_IDS');
  });

  it('Phase 5.2 calls _send_telegram_to_admins after saving report', () => {
    const content = fs.readFileSync(cyclePath, 'utf8');
    // Find Phase 5.2 block and confirm it calls the notification function
    const phase52Index = content.indexOf('PHASE 5.2');
    expect(phase52Index).toBeGreaterThan(-1);
    const phase52Block = content.slice(phase52Index, phase52Index + 2000);
    expect(phase52Block).toContain('_send_telegram_to_admins');
  });
});

// ─── 4. Factory test_agents.py ────────────────────────────────────────────────
describe('Factory test_agents.py', () => {
  // Prefer factory/tests/test_agents.py, fall back to factory/test_agents.py
  const testAgentsPath = fs.existsSync(path.join(__dirname, '../../factory/tests/test_agents.py'))
    ? path.join(__dirname, '../../factory/tests/test_agents.py')
    : path.join(__dirname, '../../factory/test_agents.py');
  const fileExists = fs.existsSync(testAgentsPath);

  it('test_agents.py exists', () => {
    expect(fileExists).toBe(true);
  });

  it('TestTelegramNotification class exists', () => {
    expect(fileExists).toBe(true);
    const content = fs.readFileSync(testAgentsPath, 'utf8');
    expect(content).toContain('TestTelegramNotification');
  });

  it('Has at least 5 test methods inside TestTelegramNotification', () => {
    expect(fileExists).toBe(true);
    const content = fs.readFileSync(testAgentsPath, 'utf8');
    // Extract the TestTelegramNotification class block
    const classStart = content.indexOf('class TestTelegramNotification');
    expect(classStart).toBeGreaterThan(-1);
    // Count def test_ occurrences after the class declaration
    const classBlock = content.slice(classStart);
    const nextClassMatch = classBlock.slice(1).match(/\nclass /);
    const classEnd = nextClassMatch ? nextClassMatch.index + 1 : classBlock.length;
    const methodBlock = classBlock.slice(0, classEnd);
    const testMethods = methodBlock.match(/def test_/g) || [];
    expect(testMethods.length).toBeGreaterThanOrEqual(5);
  });
});

// ─── 5. Accessibility Improvements ────────────────────────────────────────────
describe('Accessibility improvements', () => {
  it('reviews.html has aria-label attributes', () => {
    const reviewsPath = path.join(__dirname, '../public/reviews.html');
    const html = fs.readFileSync(reviewsPath, 'utf8');
    expect(html).toContain('aria-label');
  });

  it('reviews.html has role attributes', () => {
    const reviewsPath = path.join(__dirname, '../public/reviews.html');
    const html = fs.readFileSync(reviewsPath, 'utf8');
    expect(html).toContain('role=');
  });

  it('reviews.html has aria-required on form inputs', () => {
    const reviewsPath = path.join(__dirname, '../public/reviews.html');
    const html = fs.readFileSync(reviewsPath, 'utf8');
    expect(html).toContain('aria-required');
  });

  it('faq.html has aria-expanded attributes', () => {
    const faqPath = path.join(__dirname, '../public/faq.html');
    const html = fs.readFileSync(faqPath, 'utf8');
    expect(html).toContain('aria-expanded');
  });

  it('faq.html has aria-label attributes', () => {
    const faqPath = path.join(__dirname, '../public/faq.html');
    const html = fs.readFileSync(faqPath, 'utf8');
    expect(html).toContain('aria-label');
  });
});
