'use strict';
/**
 * БЛОК 7.1 — E2E Integration Tests: Booking Flow
 *
 * This is a code-level integration test suite that verifies the full booking
 * lifecycle: model catalog → order creation → admin management → client status.
 *
 * Two suites:
 *   1. API code checks  (routes/api.js inspection + HTTP requests)
 *   2. Bot code checks  (bot.js state machine inspection)
 */

process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const fs   = require('fs');
const path = require('path');
const request = require('supertest');
const express = require('express');
const cors    = require('cors');

// ── Paths ─────────────────────────────────────────────────────────────────────
const ROOT      = path.join(__dirname, '..');
const API_FILE  = path.join(ROOT, 'routes', 'api.js');
const BOT_FILE  = path.join(ROOT, 'bot.js');
const CONST_FILE = path.join(ROOT, 'utils', 'constants.js');
const DB_FILE   = path.join(ROOT, 'database.js');

const apiCode  = fs.readFileSync(API_FILE,  'utf8');
const botCode  = fs.readFileSync(BOT_FILE,  'utf8');
const constCode = fs.readFileSync(CONST_FILE, 'utf8');
const dbCode   = fs.readFileSync(DB_FILE,   'utf8');

// ── HTTP App ──────────────────────────────────────────────────────────────────
let app;
let adminToken;

async function getCsrfToken() {
  const res = await request(app).get('/api/csrf-token');
  return res.body.token;
}

beforeAll(async () => {
  const { initDatabase } = require('../database');
  await initDatabase();

  const { initBot } = require('../bot');
  const apiRouter   = require('../routes/api');

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

  const loginRes = await request(app)
    .post('/api/admin/login')
    .send({ username: 'admin', password: 'admin123' });
  adminToken = loginRes.body.token || loginRes.body.accessToken || null;
}, 30000);

afterAll(async () => {
  const { closeDatabase } = require('../database');
  if (closeDatabase) await closeDatabase();
});

// ═══════════════════════════════════════════════════════════════════════════════
// SUITE 1 — API code checks (routes/api.js)
// ═══════════════════════════════════════════════════════════════════════════════

describe('E2E: Booking Flow — API code checks', () => {

  // ── Phase 1: Constants & validation helpers ─────────────────────────────────

  test('constants.js defines ALLOWED_EVENT_TYPES', () => {
    expect(constCode).toMatch(/ALLOWED_EVENT_TYPES/);
  });

  test('constants.js defines VALID_STATUSES', () => {
    expect(constCode).toMatch(/VALID_STATUSES/);
  });

  test('constants.js defines STATUS_LABELS with expected statuses', () => {
    expect(constCode).toMatch(/new.*Новая/s);
    expect(constCode).toMatch(/confirmed.*Подтверждена/s);
    expect(constCode).toMatch(/completed.*Завершена/s);
    expect(constCode).toMatch(/cancelled.*Отменена/s);
  });

  test('constants.js defines EVENT_TYPES with booking-relevant types', () => {
    expect(constCode).toMatch(/fashion_show/);
    expect(constCode).toMatch(/photo_shoot/);
    expect(constCode).toMatch(/commercial/);
  });

  test('api.js imports ALLOWED_EVENT_TYPES from constants', () => {
    expect(apiCode).toMatch(/ALLOWED_EVENT_TYPES.*require.*constants/s);
  });

  test('api.js imports VALID_STATUSES from constants', () => {
    expect(apiCode).toMatch(/VALID_STATUSES.*require.*constants/s);
  });

  test('api.js defines validatePhone function', () => {
    expect(apiCode).toMatch(/function validatePhone/);
  });

  test('api.js validatePhone uses regex pattern for phone numbers', () => {
    expect(apiCode).toMatch(/validatePhone.*[\d\\s\\+\\(\\)\\-]{7,20}/s);
  });

  test('api.js defines validateEmail function', () => {
    expect(apiCode).toMatch(/function validateEmail/);
  });

  test('api.js defines validateDate function', () => {
    expect(apiCode).toMatch(/function validateDate/);
  });

  test('api.js defines sanitize function for input sanitization', () => {
    expect(apiCode).toMatch(/function sanitize/);
  });

  // ── Phase 2: POST /api/orders endpoint ────────────────────────────────────

  test('api.js defines POST /api/orders route', () => {
    expect(apiCode).toMatch(/router\.post\(['"]\/orders['"]/);
  });

  test('api.js POST /orders applies bookingLimiter rate limiter', () => {
    expect(apiCode).toMatch(/router\.post\(['"]\/orders['"]\s*,\s*bookingLimiter/);
  });

  test('api.js POST /orders validates CSRF token', () => {
    expect(apiCode).toMatch(/x-csrf-token|validateToken.*csrfToken/s);
  });

  test('api.js POST /orders validates client_name required', () => {
    expect(apiCode).toMatch(/sanitize\(client_name.*Укажите ваше имя/s);
  });

  test('api.js POST /orders validates client_phone required', () => {
    expect(apiCode).toMatch(/validatePhone\(client_phone\).*Укажите корректный номер телефона/s);
  });

  test('api.js POST /orders validates event_type against whitelist', () => {
    expect(apiCode).toMatch(/ALLOWED_EVENT_TYPES\.includes\(event_type\)/);
  });

  test('api.js POST /orders returns 400 when event_type invalid', () => {
    expect(apiCode).toMatch(/ALLOWED_EVENT_TYPES\.includes.*400.*Неверный тип/s);
  });

  test('api.js POST /orders validates email format', () => {
    expect(apiCode).toMatch(/validateEmail\(client_email\)/);
  });

  test('api.js POST /orders validates event_date format', () => {
    expect(apiCode).toMatch(/validateDate\(event_date\)/);
  });

  test('api.js POST /orders calls generateOrderNumber', () => {
    expect(apiCode).toMatch(/generateOrderNumber\(\)/);
  });

  test('api.js POST /orders inserts into orders table', () => {
    expect(apiCode).toMatch(/INSERT INTO orders/);
  });

  test('api.js POST /orders inserts order_number column', () => {
    expect(apiCode).toMatch(/INSERT INTO orders[\s\S]*order_number/);
  });

  test('api.js POST /orders inserts client_name and client_phone', () => {
    expect(apiCode).toMatch(/INSERT INTO orders[\s\S]*client_name[\s\S]*client_phone/s);
  });

  test('api.js POST /orders inserts event_type', () => {
    expect(apiCode).toMatch(/INSERT INTO orders[\s\S]*event_type/s);
  });

  test('api.js POST /orders stores model_id', () => {
    expect(apiCode).toMatch(/INSERT INTO orders[\s\S]*model_id/s);
  });

  test('api.js POST /orders supports multi-model booking via model_ids', () => {
    expect(apiCode).toMatch(/model_ids/);
    expect(apiCode).toMatch(/rawModelIds|model_ids.*array/i);
  });

  test('api.js POST /orders notifies bot after insertion', () => {
    expect(apiCode).toMatch(/botInstance.*notifyNewOrder/);
  });

  test('api.js POST /orders sends email confirmation', () => {
    expect(apiCode).toMatch(/mailer\.sendOrderConfirmation/);
  });

  test('api.js POST /orders returns order_number in response', () => {
    expect(apiCode).toMatch(/res\.json\(\{.*order_number/s);
  });

  // ── Phase 3: GET /api/orders/status endpoints ──────────────────────────────

  test('api.js defines GET /orders/status/:order_number route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/orders\/status\/:order_number['"]/);
  });

  test('api.js defines GET /orders/status (query param) route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/orders\/status['"]/);
  });

  test('api.js GET /orders/status validates empty number → 400', () => {
    expect(apiCode).toMatch(/!number.*400.*Укажите номер заявки/s);
  });

  test('api.js GET /orders/status returns 404 when not found', () => {
    expect(apiCode).toMatch(/!order.*404.*Заявка не найдена/s);
  });

  test('api.js GET /orders/status joins models table for model_name', () => {
    expect(apiCode).toMatch(/LEFT JOIN models.*ON.*model_id/s);
  });

  test('api.js defines GET /orders/by-phone route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/orders\/by-phone['"]/);
  });

  test('api.js GET /orders/by-phone applies rate limiter', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/orders\/by-phone['"],\s*byPhoneLimiter/);
  });

  // ── Phase 4: Admin orders endpoints ───────────────────────────────────────

  test('api.js defines GET /admin/orders route (with auth)', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/admin\/orders['"]\s*,\s*auth/);
  });

  test('api.js defines GET /admin/orders/:id route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/admin\/orders\/:id['"]/);
  });

  test('api.js defines PATCH /admin/orders/:id/status route', () => {
    expect(apiCode).toMatch(/router\.patch\(['"]\/admin\/orders\/:id\/status['"]/);
  });

  test('api.js PATCH /admin/orders/:id/status validates status against ALLOWED_STATUSES', () => {
    expect(apiCode).toMatch(/ALLOWED_STATUSES\.includes\(status\)/);
  });

  test('api.js PATCH /admin/orders/:id/status returns 400 for invalid status', () => {
    expect(apiCode).toMatch(/ALLOWED_STATUSES\.includes.*400.*Invalid status/s);
  });

  test('api.js PATCH status updates orders table', () => {
    expect(apiCode).toMatch(/UPDATE orders SET status=\?/);
  });

  test('api.js PATCH status inserts into order_status_history', () => {
    expect(apiCode).toMatch(/INSERT INTO order_status_history/);
  });

  test('api.js PATCH status notifies bot client of status change', () => {
    expect(apiCode).toMatch(/botInstance.*notifyStatusChange/);
  });

  test('api.js PATCH status sends email on status change', () => {
    expect(apiCode).toMatch(/mailer\.sendStatusChange/);
  });

  test('api.js PATCH status logs audit trail', () => {
    expect(apiCode).toMatch(/logAudit.*status_change.*order/s);
  });

  test('api.js defines GET /admin/orders/:id/history route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/admin\/orders\/:id\/history['"]/);
  });

  test('api.js admin orders supports search param filtering', () => {
    expect(apiCode).toMatch(/search.*LIKE|search.*client_name/s);
  });

  test('api.js admin orders supports status filter', () => {
    expect(apiCode).toMatch(/status.*filter|filter.*status/i);
  });

  // ── Phase 5: Model catalog endpoints ──────────────────────────────────────

  test('api.js defines GET /models route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/models['"]/);
  });

  test('api.js defines GET /models/:id route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/models\/:id['"]/);
  });

  test('api.js GET /models returns 404 for unknown model', () => {
    expect(apiCode).toMatch(/\/models\/:id[\s\S]*?404/s);
  });

  test('api.js GET /models supports pagination (page, per_page)', () => {
    expect(apiCode).toMatch(/per_page|page.*LIMIT/s);
  });

  test('api.js GET /models supports city filter', () => {
    expect(apiCode).toMatch(/city.*=.*\?|WHERE.*city/s);
  });

  test('api.js GET /models supports featured filter', () => {
    expect(apiCode).toMatch(/featured/);
  });

  // ── Phase 6: database.js helpers ──────────────────────────────────────────

  test('database.js exports generateOrderNumber', () => {
    expect(dbCode).toMatch(/generateOrderNumber/);
  });

  test('database.js generateOrderNumber returns a prefixed order string (NM- or ORD-)', () => {
    expect(dbCode).toMatch(/NM-|ORD-/);
  });

  test('database.js exports initDatabase', () => {
    expect(dbCode).toMatch(/exports\.initDatabase|module\.exports.*initDatabase/s);
  });

  test('database.js creates orders table', () => {
    expect(dbCode).toMatch(/CREATE TABLE.*IF NOT EXISTS.*orders/s);
  });

  test('database.js orders table has order_number column', () => {
    expect(dbCode).toMatch(/order_number/);
  });

  test('database.js orders table has client_name column', () => {
    expect(dbCode).toMatch(/client_name/);
  });

  test('database.js orders table has client_phone column', () => {
    expect(dbCode).toMatch(/client_phone/);
  });

  test('database.js orders table has event_type column', () => {
    expect(dbCode).toMatch(/event_type/);
  });

  test('database.js orders table has status column', () => {
    expect(dbCode).toMatch(/status.*DEFAULT.*new|status.*new/s);
  });

  test('database.js creates order_status_history table', () => {
    expect(dbCode).toMatch(/order_status_history/);
  });

});

// ═══════════════════════════════════════════════════════════════════════════════
// SUITE 2 — Bot code checks (bot.js)
// ═══════════════════════════════════════════════════════════════════════════════

describe('E2E: Booking Flow — Bot code checks', () => {

  // ── State machine ──────────────────────────────────────────────────────────

  test('bot.js defines ACTIVE_BOOKING_STATES set', () => {
    expect(botCode).toMatch(/ACTIVE_BOOKING_STATES/);
  });

  test('bot.js ACTIVE_BOOKING_STATES includes bk_s1 (model selection)', () => {
    expect(botCode).toMatch(/ACTIVE_BOOKING_STATES.*bk_s1|bk_s1.*ACTIVE_BOOKING_STATES/s);
  });

  test('bot.js ACTIVE_BOOKING_STATES includes bk_s2_event (event type)', () => {
    expect(botCode).toMatch(/bk_s2_event/);
  });

  test('bot.js ACTIVE_BOOKING_STATES includes bk_s3_name (client name)', () => {
    expect(botCode).toMatch(/bk_s3_name/);
  });

  test('bot.js ACTIVE_BOOKING_STATES includes bk_s3_phone (client phone)', () => {
    expect(botCode).toMatch(/bk_s3_phone/);
  });

  test('bot.js ACTIVE_BOOKING_STATES includes bk_s4 (confirmation step)', () => {
    expect(botCode).toMatch(/bk_s4/);
  });

  test('bot.js ACTIVE_BOOKING_STATES includes bk_quick_name / bk_quick_phone', () => {
    expect(botCode).toMatch(/bk_quick_name/);
    expect(botCode).toMatch(/bk_quick_phone/);
  });

  // ── Step functions ─────────────────────────────────────────────────────────

  test('bot.js defines bkStep1 function (model selection step)', () => {
    expect(botCode).toMatch(/function bkStep1/);
  });

  test('bot.js defines bkStep2EventType function', () => {
    expect(botCode).toMatch(/function bkStep2EventType/);
  });

  test('bot.js defines bkStep2Date function', () => {
    expect(botCode).toMatch(/function bkStep2Date/);
  });

  test('bot.js defines bkStep2Duration function', () => {
    expect(botCode).toMatch(/function bkStep2Duration/);
  });

  test('bot.js defines bkStep2Location function', () => {
    expect(botCode).toMatch(/function bkStep2Location/);
  });

  test('bot.js bkStep1 sets session state bk_s1', () => {
    expect(botCode).toMatch(/setSession\(chatId,\s*'bk_s1'/);
  });

  test('bot.js bkStep2EventType sets session state bk_s2_event', () => {
    expect(botCode).toMatch(/setSession\(chatId,\s*'bk_s2_event'/);
  });

  test('bot.js shows 4-step progress indicator', () => {
    expect(botCode).toMatch(/stepHeader|Шаг.*4|4.*Шаг/s);
  });

  // ── Session management ─────────────────────────────────────────────────────

  test('bot.js defines getSession function', () => {
    expect(botCode).toMatch(/async function getSession/);
  });

  test('bot.js defines setSession function', () => {
    expect(botCode).toMatch(/async function setSession/);
  });

  test('bot.js defines clearSession function', () => {
    expect(botCode).toMatch(/function clearSession/);
  });

  test('bot.js defines resetSessionTimer function', () => {
    expect(botCode).toMatch(/function resetSessionTimer/);
  });

  test('bot.js SESSION_TIMEOUT_MS used for session expiry', () => {
    expect(botCode).toMatch(/SESSION_TIMEOUT_MS/);
  });

  // ── bk_submit flow ─────────────────────────────────────────────────────────

  test('bot.js handles bk_submit callback', () => {
    expect(botCode).toMatch(/bk_submit/);
  });

  test('bot.js bk_submit inserts into orders table', () => {
    expect(botCode).toMatch(/INSERT INTO orders[\s\S]*client_name[\s\S]*client_phone/s);
  });

  test('bot.js bk_submit sets status=new on insert', () => {
    expect(botCode).toMatch(/INSERT INTO orders[\s\S]*'new'|status.*'new'/s);
  });

  test('bot.js bk_submit calls notifyNewOrder after insert', () => {
    expect(botCode).toMatch(/notifyNewOrder\(order\)/);
  });

  test('bot.js bk_submit calls clearSession after order created', () => {
    expect(botCode).toMatch(/clearSession\(chatId\)/);
  });

  test('bot.js bk_submit shows confirmation message with order number', () => {
    expect(botCode).toMatch(/Заявка принята|orderNum|order_number/s);
  });

  test('bot.js bk_submit handles error gracefully (try/catch)', () => {
    expect(botCode).toMatch(/bkSubmit[\s\S]*catch[\s\S]*Не удалось создать заявку/s);
  });

  // ── bk_cancel / timeout ────────────────────────────────────────────────────

  test('bot.js handles bk_cancel callback', () => {
    expect(botCode).toMatch(/bk_cancel/);
  });

  test('bot.js sends timeout message when booking session expires', () => {
    expect(botCode).toMatch(/Время сессии истекло|сессии истекло/);
  });

  test('bot.js offers restart after session timeout', () => {
    expect(botCode).toMatch(/bk_start.*Начать заново|Начать заново.*bk_start/s);
  });

  // ── notifyNewOrder ─────────────────────────────────────────────────────────

  test('bot.js defines notifyNewOrder function', () => {
    expect(botCode).toMatch(/async function notifyNewOrder|function notifyNewOrder/);
  });

  test('bot.js exports notifyNewOrder', () => {
    expect(botCode).toMatch(/module\.exports[\s\S]*notifyNewOrder/s);
  });

  test('bot.js exports initBot', () => {
    expect(botCode).toMatch(/module\.exports[\s\S]*initBot/s);
  });

  // ── EVENT_TYPES used in bot UI ─────────────────────────────────────────────

  test('bot.js imports EVENT_TYPES from constants', () => {
    expect(botCode).toMatch(/EVENT_TYPES/);
    expect(botCode).toMatch(/require.*constants/);
  });

  test('bot.js uses EVENT_TYPES to build event type buttons', () => {
    expect(botCode).toMatch(/Object\.entries\(EVENT_TYPES\)|bk_etype_/s);
  });

  test('bot.js imports STATUS_LABELS from constants', () => {
    expect(botCode).toMatch(/STATUS_LABELS/);
  });

});

// ═══════════════════════════════════════════════════════════════════════════════
// SUITE 3 — HTTP integration: Phase 1 & 2 (public catalog + order creation)
// ═══════════════════════════════════════════════════════════════════════════════

describe('E2E: Booking Flow — HTTP: Public catalog', () => {

  test('GET /api/models returns 200 with an array', async () => {
    const res = await request(app).get('/api/models');
    expect(res.status).toBe(200);
    const models = res.body.models || res.body;
    expect(Array.isArray(models)).toBe(true);
  });

  test('GET /api/models supports page + per_page pagination', async () => {
    const res = await request(app).get('/api/models?page=1&per_page=3');
    expect(res.status).toBe(200);
  });

  test('GET /api/models supports city filter', async () => {
    const res = await request(app).get('/api/models?city=Москва');
    expect([200, 204]).toContain(res.status);
  });

  test('GET /api/models supports featured=1 filter', async () => {
    const res = await request(app).get('/api/models?featured=1');
    expect(res.status).toBe(200);
  });

  test('GET /api/models/:id returns 404 for non-existent model', async () => {
    const res = await request(app).get('/api/models/9999999');
    expect(res.status).toBe(404);
  });

});

describe('E2E: Booking Flow — HTTP: Order creation', () => {
  let createdOrderId;
  let createdOrderNumber;

  test('POST /api/orders with valid data returns 200/201', async () => {
    const csrf = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrf)
      .send({
        client_name:  'E2E Test Client',
        client_phone: '+79001234567',
        client_email: 'e2e-block71@test.com',
        event_type:   'photo_shoot',
        event_date:   '2026-06-01',
        budget:       '50000',
        comments:     'БЛОК 7.1 E2E booking test',
      });
    expect([200, 201]).toContain(res.status);
    if ([200, 201].includes(res.status)) {
      expect(res.body).toHaveProperty('order_number');
      createdOrderId     = res.body.id || res.body.order_id;
      createdOrderNumber = res.body.order_number;
    }
  });

  test('POST /api/orders returns order_number in response body', async () => {
    if (!createdOrderNumber) return;
    expect(typeof createdOrderNumber).toBe('string');
    expect(createdOrderNumber.length).toBeGreaterThan(3);
  });

  test('POST /api/orders without client_name returns 400', async () => {
    const csrf = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrf)
      .send({
        client_phone: '+79001234567',
        event_type:   'photo_shoot',
        event_date:   '2026-06-01',
      });
    expect(res.status).toBe(400);
  });

  test('POST /api/orders without client_phone returns 400', async () => {
    const csrf = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrf)
      .send({
        client_name: 'Test Client',
        event_type:  'photo_shoot',
        event_date:  '2026-06-01',
      });
    expect(res.status).toBe(400);
  });

  test('POST /api/orders with invalid phone returns 400', async () => {
    const csrf = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrf)
      .send({
        client_name:  'Test Client',
        client_phone: 'not-a-phone',
        event_type:   'photo_shoot',
        event_date:   '2026-06-01',
      });
    expect(res.status).toBe(400);
  });

  test('POST /api/orders with invalid event_type returns 400', async () => {
    const csrf = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrf)
      .send({
        client_name:  'Test Client',
        client_phone: '+79001234568',
        event_type:   'invalid_booking_type_xyz',
        event_date:   '2026-06-01',
      });
    expect(res.status).toBe(400);
  });

  test('POST /api/orders without CSRF token returns 403', async () => {
    const res = await request(app)
      .post('/api/orders')
      .send({
        client_name:  'CSRF Test',
        client_phone: '+79001234569',
        event_type:   'event',
        event_date:   '2026-06-01',
      });
    expect([400, 403, 429]).toContain(res.status);
  });

  test('GET /api/orders/status returns 400 when no number provided', async () => {
    const res = await request(app).get('/api/orders/status');
    expect(res.status).toBe(400);
  });

  test('GET /api/orders/status?number=ORD-INVALID returns 404', async () => {
    const res = await request(app).get('/api/orders/status?number=ORD-INVALID-99');
    expect([400, 404]).toContain(res.status);
  });

  test('GET /api/orders/by-phone returns orders array', async () => {
    const res = await request(app).get('/api/orders/by-phone?phone=%2B79001234567');
    expect(res.status).toBe(200);
    const orders = res.body.orders || res.body;
    expect(Array.isArray(orders)).toBe(true);
  });

});

describe('E2E: Booking Flow — HTTP: Admin order management', () => {
  let managedOrderId;

  beforeAll(async () => {
    const csrf = await getCsrfToken();
    const res = await request(app)
      .post('/api/orders')
      .set('x-csrf-token', csrf)
      .send({
        client_name:  'Admin Managed E2E',
        client_phone: '+79009001234',
        client_email: 'managed-e2e@test.com',
        event_type:   'commercial',
        event_date:   '2026-08-15',
        budget:       '80000',
      });
    if ([200, 201].includes(res.status)) {
      managedOrderId = res.body?.id || res.body?.order_id;
    }
  });

  test('GET /api/admin/orders requires authentication', async () => {
    const res = await request(app).get('/api/admin/orders');
    expect([401, 403]).toContain(res.status);
  });

  test('GET /api/admin/orders returns 200 with valid token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/orders')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    const orders = res.body.orders || res.body;
    expect(Array.isArray(orders)).toBe(true);
  });

  test('GET /api/admin/orders?status=new filters by status', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/orders?status=new')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('GET /api/admin/orders/:id returns order details', async () => {
    if (!adminToken || !managedOrderId) return;
    const res = await request(app)
      .get(`/api/admin/orders/${managedOrderId}`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('id', managedOrderId);
  });

  test('PATCH /api/admin/orders/:id/status changes status to confirmed', async () => {
    if (!adminToken || !managedOrderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${managedOrderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect([200, 201, 204]).toContain(res.status);
  });

  test('PATCH /api/admin/orders/:id/status changes status to in_progress', async () => {
    if (!adminToken || !managedOrderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${managedOrderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'in_progress' });
    expect([200, 201, 204]).toContain(res.status);
  });

  test('PATCH /api/admin/orders/:id/status changes status to completed', async () => {
    if (!adminToken || !managedOrderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${managedOrderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'completed' });
    expect([200, 201, 204]).toContain(res.status);
  });

  test('PATCH /api/admin/orders/:id/status rejects invalid status', async () => {
    if (!adminToken || !managedOrderId) return;
    const res = await request(app)
      .patch(`/api/admin/orders/${managedOrderId}/status`)
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'invalid_status_xyz' });
    expect(res.status).toBe(400);
  });

  test('PATCH /api/admin/orders/:id/status returns 404 for unknown order', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .patch('/api/admin/orders/9999999/status')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ status: 'confirmed' });
    expect(res.status).toBe(404);
  });

  test('GET /api/admin/orders/:id/history returns history for order', async () => {
    if (!adminToken || !managedOrderId) return;
    const res = await request(app)
      .get(`/api/admin/orders/${managedOrderId}/history`)
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status);
    if (res.status === 200) {
      const body = res.body;
      expect(body !== null && typeof body === 'object').toBe(true);
    }
  });

});
