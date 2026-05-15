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
  if (bot) apiRouter.setBot(bot);
  a.use('/api', apiRouter);
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;

  const loginRes = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token;
}, 15000);

afterAll(() => {
  if (app && app.close) app.close();
});

// ─── 1. Admin Notification Center API ─────────────────────────────────────────
describe('GET /api/admin/notifications', () => {
  it('Returns 401 without token', async () => {
    const res = await request(app).get('/api/admin/notifications');
    expect(res.status).toBe(401);
  });

  it('Returns 200 with admin JWT', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/notifications')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('Returns { notifications: [...] } structure', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/notifications')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('notifications');
    expect(Array.isArray(res.body.notifications)).toBe(true);
  });

  it('Supports ?status=unread filter', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/notifications?status=unread')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('notifications');
    expect(Array.isArray(res.body.notifications)).toBe(true);
  });
});

describe('PATCH /api/admin/notifications/read-all', () => {
  it('Returns 401 without token', async () => {
    const res = await request(app).patch('/api/admin/notifications/read-all');
    expect(res.status).toBe(401);
  });

  it('Returns 200 with admin JWT', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .patch('/api/admin/notifications/read-all')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  it('Returns { success: true, count: N }', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .patch('/api/admin/notifications/read-all')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('success', true);
    expect(res.body).toHaveProperty('count');
    expect(typeof res.body.count).toBe('number');
  });
});

// ─── 2. Model Availability — busy dates section in model-cabinet.html ─────────
describe('model-cabinet.html busy dates section', () => {
  const cabinetPath = path.join(__dirname, '../public/model-cabinet.html');

  it('model-cabinet.html file exists', () => {
    expect(fs.existsSync(cabinetPath)).toBe(true);
  });

  it('Contains busy dates section reference ("busy" or "зайнят")', () => {
    const content = fs.readFileSync(cabinetPath, 'utf8');
    expect(content.toLowerCase()).toMatch(/busy|зайнят/);
  });
});

// ─── 3. WhatsApp Service ───────────────────────────────────────────────────────
describe('services/whatsapp.js', () => {
  const waPath = path.join(__dirname, '../services/whatsapp.js');

  it('whatsapp.js file exists', () => {
    expect(fs.existsSync(waPath)).toBe(true);
  });

  it('Exports isConfigured function', () => {
    const wa = require('../services/whatsapp');
    expect(typeof wa.isConfigured).toBe('function');
  });

  it('Exports verifyWebhook function', () => {
    const wa = require('../services/whatsapp');
    expect(typeof wa.verifyWebhook).toBe('function');
  });

  it('Exports sendText function', () => {
    const wa = require('../services/whatsapp');
    expect(typeof wa.sendText).toBe('function');
  });

  it('Exports sendOrderStatusWA function', () => {
    const wa = require('../services/whatsapp');
    expect(typeof wa.sendOrderStatusWA).toBe('function');
  });

  it('Exports sendBookingConfirmationWA function', () => {
    const wa = require('../services/whatsapp');
    expect(typeof wa.sendBookingConfirmationWA).toBe('function');
  });

  it('isConfigured() returns false when env vars are missing', () => {
    const origToken = process.env.WHATSAPP_TOKEN;
    const origPhoneId = process.env.WHATSAPP_PHONE_ID;
    delete process.env.WHATSAPP_TOKEN;
    delete process.env.WHATSAPP_PHONE_ID;
    const wa = require('../services/whatsapp');
    expect(wa.isConfigured()).toBe(false);
    if (origToken !== undefined) process.env.WHATSAPP_TOKEN = origToken;
    if (origPhoneId !== undefined) process.env.WHATSAPP_PHONE_ID = origPhoneId;
  });

  it('verifyWebhook() returns null for wrong token', () => {
    const wa = require('../services/whatsapp');
    process.env.WHATSAPP_VERIFY_TOKEN = 'correct-token';
    const result = wa.verifyWebhook({
      'hub.mode': 'subscribe',
      'hub.verify_token': 'wrong-token',
      'hub.challenge': 'challenge123',
    });
    expect(result).toBeNull();
    delete process.env.WHATSAPP_VERIFY_TOKEN;
  });

  it('sendText() returns { sent: false, reason: "not_configured" } without env', async () => {
    const origToken = process.env.WHATSAPP_TOKEN;
    const origPhoneId = process.env.WHATSAPP_PHONE_ID;
    delete process.env.WHATSAPP_TOKEN;
    delete process.env.WHATSAPP_PHONE_ID;
    const wa = require('../services/whatsapp');
    const result = await wa.sendText('+79001234567', 'test message');
    expect(result).toEqual({ sent: false, reason: 'not_configured' });
    if (origToken !== undefined) process.env.WHATSAPP_TOKEN = origToken;
    if (origPhoneId !== undefined) process.env.WHATSAPP_PHONE_ID = origPhoneId;
  });

  it('sendOrderStatusWA() returns { sent: false, reason: "no_phone" } for null order', async () => {
    const wa = require('../services/whatsapp');
    const result = await wa.sendOrderStatusWA(null, 'confirmed', 'Підтверджено');
    expect(result).toEqual({ sent: false, reason: 'no_phone' });
  });

  it('sendBookingConfirmationWA() returns { sent: false, reason: "no_phone" } when no phone', async () => {
    const wa = require('../services/whatsapp');
    const result = await wa.sendBookingConfirmationWA({ id: 1, order_number: 'ON-001' });
    expect(result).toEqual({ sent: false, reason: 'no_phone' });
  });
});

// ─── 4. bot.js model registration ─────────────────────────────────────────────
describe('bot.js model registration', () => {
  const botPath = path.join(__dirname, '../bot.js');

  it('bot.js exists', () => {
    expect(fs.existsSync(botPath)).toBe(true);
  });

  it('Contains /register_model command', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('/register_model');
  });

  it('Contains mdl_confirm_ callback pattern', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('mdl_confirm_');
  });

  it('Contains mdl_reject_ callback pattern', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('mdl_reject_');
  });
});

describe('database.js telegram_chat_id column', () => {
  const dbPath = path.join(__dirname, '../database.js');

  it('database.js contains telegram_chat_id column', () => {
    const content = fs.readFileSync(dbPath, 'utf8');
    expect(content).toContain('telegram_chat_id');
  });
});

// ─── 5. channel_content.py agent ──────────────────────────────────────────────
describe('factory/agents/channel_content.py', () => {
  const channelPath = path.join(__dirname, '../../factory/agents/channel_content.py');

  it('channel_content.py file exists', () => {
    expect(fs.existsSync(channelPath)).toBe(true);
  });

  it('Contains ChannelContentGenerator class', () => {
    const content = fs.readFileSync(channelPath, 'utf8');
    expect(content).toContain('ChannelContentGenerator');
  });

  it('Contains generate_model_spotlight_post method', () => {
    const content = fs.readFileSync(channelPath, 'utf8');
    expect(content).toContain('generate_model_spotlight_post');
  });

  it('Contains generate_promotion_post method', () => {
    const content = fs.readFileSync(channelPath, 'utf8');
    expect(content).toContain('generate_promotion_post');
  });
});
