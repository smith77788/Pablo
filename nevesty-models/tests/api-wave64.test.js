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

// ─── 1. POST /api/chat/ask — rule-based chatbot ────────────────────────────────
describe('POST /api/chat/ask', () => {
  it('returns 200 with reply for a valid message', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'Расскажи об агентстве' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('reply');
  });

  it('reply is a non-empty string', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'Расскажи об агентстве' });
    expect(typeof res.body.reply).toBe('string');
    expect(res.body.reply.length).toBeGreaterThan(0);
  });

  it('returns 400 when message field is missing', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({});
    expect(res.status).toBe(400);
  });

  it('returns 400 when message is empty string', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: '' });
    expect(res.status).toBe(400);
  });

  it('truncates message > 500 chars and still returns a reply', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'x'.repeat(501) });
    // sanitize() truncates to 500 chars, so message remains valid — expect a reply
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('reply');
  });

  it('responds to "цена" keyword with pricing info', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'какая цена на услуги?' });
    expect(res.status).toBe(200);
    expect(res.body.reply).toMatch(/бюджет|стоимость|₽|бронирован/i);
  });

  it('responds to "стоимость" keyword with pricing info', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'стоимость работы модели?' });
    expect(res.status).toBe(200);
    expect(res.body.reply).toMatch(/бюджет|стоимость|₽|бронирован/i);
  });

  it('responds to "привет" greeting', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'привет' });
    expect(res.status).toBe(200);
    expect(res.body.reply).toMatch(/здравствуйт|помог|цен|бронирован/i);
  });

  it('responds to "как забронировать" booking question', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'как забронировать модель?' });
    expect(res.status).toBe(200);
    // DB FAQ or rule-based: both contain booking-related words
    expect(res.body.reply).toMatch(/Забронировать|бронирован|Заказ/i);
  });

  it('returns JSON content-type', async () => {
    const res = await request(app)
      .post('/api/chat/ask')
      .send({ message: 'вопрос' });
    expect(res.headers['content-type']).toMatch(/application\/json/);
  });
});

// ─── 2. compare.html static file checks ───────────────────────────────────────
describe('compare.html improvements', () => {
  const comparePath = path.join(__dirname, '../public/compare.html');
  let content;

  beforeAll(() => {
    content = fs.readFileSync(comparePath, 'utf8');
  });

  it('file exists at public/compare.html', () => {
    expect(fs.existsSync(comparePath)).toBe(true);
  });

  it('contains highlight-green CSS class reference', () => {
    expect(content).toMatch(/highlight-green/);
  });

  it('contains similarity score section', () => {
    expect(content).toMatch(/similarity|Схожест/i);
  });

  it('contains window.print for print button support', () => {
    expect(content).toMatch(/window\.print/);
  });

  it('contains /booking.html link for booking button', () => {
    expect(content).toMatch(/\/booking\.html/);
  });

  it('contains "Назад" text for back link', () => {
    expect(content).toMatch(/Назад/);
  });

  it('contains @media print CSS rule', () => {
    expect(content).toMatch(/@media\s+print/);
  });
});

// ─── 3. chat-widget.js static file checks ─────────────────────────────────────
describe('chat-widget.js', () => {
  const widgetPath = path.join(__dirname, '../public/js/chat-widget.js');
  let content;

  beforeAll(() => {
    content = fs.readFileSync(widgetPath, 'utf8');
  });

  it('file exists at public/js/chat-widget.js', () => {
    expect(fs.existsSync(widgetPath)).toBe(true);
  });

  it('contains /api/chat/ask API call', () => {
    expect(content).toMatch(/\/api\/chat\/ask/);
  });

  it('contains sessionStorage for chat history', () => {
    expect(content).toMatch(/sessionStorage/);
  });

  it('contains ARIA attributes for accessibility', () => {
    expect(content).toMatch(/role=|aria-/);
  });

  it('contains dark mode support via prefers-color-scheme', () => {
    expect(content).toMatch(/prefers-color-scheme/);
  });
});

// ─── 4. Pages include chat widget ─────────────────────────────────────────────
describe('Pages include chat-widget.js', () => {
  const publicDir = path.join(__dirname, '../public');

  it('public/index.html includes chat-widget.js', () => {
    const content = fs.readFileSync(path.join(publicDir, 'index.html'), 'utf8');
    expect(content).toMatch(/chat-widget\.js/);
  });

  it('public/catalog.html includes chat-widget.js', () => {
    const content = fs.readFileSync(path.join(publicDir, 'catalog.html'), 'utf8');
    expect(content).toMatch(/chat-widget\.js/);
  });

  it('public/model.html includes chat-widget.js', () => {
    const content = fs.readFileSync(path.join(publicDir, 'model.html'), 'utf8');
    expect(content).toMatch(/chat-widget\.js/);
  });

  it('public/booking.html includes chat-widget.js', () => {
    const content = fs.readFileSync(path.join(publicDir, 'booking.html'), 'utf8');
    expect(content).toMatch(/chat-widget\.js/);
  });
});
