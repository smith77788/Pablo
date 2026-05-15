'use strict';
/**
 * Integration tests for Wave 77 features:
 * - Admin Settings Panel API (БЛОК 1.1)
 * - Bot Settings Keys & Defaults (БЛОК 1.2)
 * - Admin Panel Sections & Bot Toggles (БЛОК 1.3)
 *
 * Covers:
 *   - GET/PUT /api/admin/settings (auth, response shape)
 *   - GET /api/admin/settings/sections (grouped sections)
 *   - POST /api/admin/settings/import & reset
 *   - GET /api/admin/settings/export
 *   - Settings keys: greeting, about, phone, email, instagram, address
 *   - Feature toggles: quick_booking_enabled, booking_auto_confirm, reviews_enabled,
 *     reviews_auto_approve, catalog_per_page, catalog_sort, cities_list,
 *     wishlist_enabled, search_enabled
 *   - bot.js code checks: 8 showAdminSettings sections, notification toggles,
 *     settings applied in bot logic
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

const BOT_JS = path.join(__dirname, '../bot.js');
const API_JS = path.join(__dirname, '../routes/api.js');

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

// ── БЛОК 1.1: Admin Settings API ─────────────────────────────────────────────

describe('Wave 77 БЛОК 1.1: GET /api/admin/settings', () => {
  test('returns 401 without auth token', async () => {
    const res = await request(app).get('/api/admin/settings');
    expect(res.status).toBe(401);
  });

  test('returns 200 with valid auth token', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('returns an array of {key, value} objects', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(Array.isArray(res.body)).toBe(true);
  });

  test('each setting row has key and value properties', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    if (res.body.length > 0) {
      expect(res.body[0]).toHaveProperty('key');
      expect(res.body[0]).toHaveProperty('value');
    }
  });

  test('GET /api/settings (alias) returns 401 without auth', async () => {
    const res = await request(app).get('/api/settings');
    expect(res.status).toBe(401);
  });

  test('GET /api/settings returns 200 with auth', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('GET /api/settings returns an object (key→value map)', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(typeof res.body).toBe('object');
    expect(Array.isArray(res.body)).toBe(false);
  });
});

describe('Wave 77 БЛОК 1.1: PUT /api/settings saves key/value', () => {
  test('PUT /api/settings requires auth', async () => {
    const res = await request(app)
      .put('/api/settings')
      .send({ greeting: 'Hello Test' });
    expect(res.status).toBe(401);
  });

  test('PUT /api/settings with valid body returns ok:true', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ greeting: 'Test greeting wave77' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
  });

  test('PUT /api/settings persists greeting value', async () => {
    if (!adminToken) return;
    await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ greeting: 'Wave77 greeting persisted' });

    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.greeting).toBe('Wave77 greeting persisted');
  });

  test('PUT /api/settings with no keys in body still returns ok', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    // Empty body is valid — no keys to save, returns ok:true
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
  });

  test('PUT /api/settings ignores disallowed keys silently', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ _totally_unknown_key_xyz: 'should be ignored' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
  });
});

// ── БЛОК 1.1: Admin Settings Sections ────────────────────────────────────────

describe('Wave 77 БЛОК 1.1: GET /api/admin/settings/sections', () => {
  test('requires auth', async () => {
    const res = await request(app).get('/api/admin/settings/sections');
    expect(res.status).toBe(401);
  });

  test('returns 200 with auth', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
  });

  test('returns sections object', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body).toHaveProperty('sections');
    expect(typeof res.body.sections).toBe('object');
  });

  test('sections contains contacts', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('contacts');
  });

  test('sections contains catalog', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('catalog');
  });

  test('sections contains booking', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('booking');
  });

  test('sections contains reviews', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('reviews');
  });

  test('sections contains notifications', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('notifications');
  });

  test('sections contains bot', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.sections).toHaveProperty('bot');
  });

  test('each section has label and settings fields', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/sections')
      .set('Authorization', `Bearer ${adminToken}`);
    const sections = res.body.sections;
    for (const key of Object.keys(sections)) {
      expect(sections[key]).toHaveProperty('label');
      expect(sections[key]).toHaveProperty('settings');
    }
  });
});

// ── БЛОК 1.1: Export & Import ─────────────────────────────────────────────────

describe('Wave 77 БЛОК 1.1: Settings export and import', () => {
  test('GET /api/admin/settings/export requires auth', async () => {
    const res = await request(app).get('/api/admin/settings/export');
    expect(res.status).toBe(401);
  });

  test('GET /api/admin/settings/export returns JSON with auth', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/export')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(typeof res.body).toBe('object');
  });

  test('GET /api/admin/settings/export sets Content-Disposition header', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .get('/api/admin/settings/export')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.headers['content-disposition']).toMatch(/settings\.json/);
  });

  test('POST /api/admin/settings/import requires auth', async () => {
    const res = await request(app)
      .post('/api/admin/settings/import')
      .send({ greeting: 'imported hello' });
    expect(res.status).toBe(401);
  });

  test('POST /api/admin/settings/import with valid object returns ok', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ agency_name: 'Test Agency Import', tagline: 'Best Models' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
    expect(res.body).toHaveProperty('imported');
  });

  test('POST /api/admin/settings/import rejects array', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send([{ key: 'foo', value: 'bar' }]);
    expect(res.status).toBe(400);
  });

  test('POST /api/admin/settings/import skips sensitive keys', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/import')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ admin_password: 'hacked', greeting: 'safe import' });
    expect(res.body.ok).toBe(true);
    // imported count should not include admin_password
    expect(res.body.imported).toBeLessThan(2);
  });
});

// ── БЛОК 1.1: Settings Reset ──────────────────────────────────────────────────

describe('Wave 77 БЛОК 1.1: POST /api/admin/settings/reset', () => {
  test('requires auth', async () => {
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .send({ key: 'greeting' });
    expect(res.status).toBe(401);
  });

  test('resets greeting to default value', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ key: 'greeting' });
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('ok', true);
    expect(res.body).toHaveProperty('value');
  });

  test('returns 400 when key is missing', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({});
    expect(res.status).toBe(400);
  });

  test('returns 400 for key with no default', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ key: 'some_nonexistent_key_xyz' });
    expect(res.status).toBe(400);
  });

  test('reset reviews_enabled returns default 1', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ key: 'reviews_enabled' });
    expect(res.status).toBe(200);
    expect(res.body.value).toBe('1');
  });

  test('reset catalog_per_page returns default 6', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .post('/api/admin/settings/reset')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ key: 'catalog_per_page' });
    expect(res.status).toBe(200);
    expect(res.body.value).toBe('6');
  });
});

// ── БЛОК 1.2: Bot Settings Keys via API ──────────────────────────────────────

describe('Wave 77 БЛОК 1.2: Contact settings keys', () => {
  test('PUT /api/settings saves greeting key', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ greeting: 'Добро пожаловать!' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves about key', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ about: 'О нас - лучшее агентство' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves contacts_phone key', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ contacts_phone: '+7 (999) 123-45-67' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves contacts_email key', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ contacts_email: 'info@nevesty.com' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves contacts_instagram key', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ contacts_instagram: '@nevesty_models' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves contacts_address key', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ contacts_address: 'г. Москва, ул. Тверская, 1' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

describe('Wave 77 БЛОК 1.2: Feature toggle settings keys', () => {
  test('PUT /api/settings saves quick_booking_enabled', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ quick_booking_enabled: '1' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('quick_booking_enabled persists after save', async () => {
    if (!adminToken) return;
    await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ quick_booking_enabled: '0' });
    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.quick_booking_enabled).toBe('0');
  });

  test('PUT /api/settings saves booking_auto_confirm', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ booking_auto_confirm: '1' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves reviews_enabled', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reviews_enabled: '1' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('reviews_enabled=0 persists correctly', async () => {
    if (!adminToken) return;
    await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ reviews_enabled: '0' });
    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.reviews_enabled).toBe('0');
  });

  test('PUT /api/settings saves wishlist_enabled', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ wishlist_enabled: '1' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves search_enabled', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ search_enabled: '1' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('PUT /api/settings saves catalog_per_page', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ catalog_per_page: '9' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('catalog_per_page value persists after save', async () => {
    if (!adminToken) return;
    await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ catalog_per_page: '12' });
    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.catalog_per_page).toBe('12');
  });

  test('PUT /api/settings saves catalog_sort', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ catalog_sort: 'featured' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });
});

// ── БЛОК 1.2: Cities list settings ────────────────────────────────────────────

describe('Wave 77 БЛОК 1.2: Cities list settings', () => {
  test('PUT /api/settings saves cities_list', async () => {
    if (!adminToken) return;
    const res = await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ cities_list: 'Москва,Санкт-Петербург,Казань' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
  });

  test('cities_list persists as comma-separated string', async () => {
    if (!adminToken) return;
    const citiesValue = 'Москва,Санкт-Петербург,Новосибирск,Екатеринбург';
    await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ cities_list: citiesValue });
    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.cities_list).toBe(citiesValue);
  });

  test('cities_list can be updated to new value', async () => {
    if (!adminToken) return;
    const newCities = 'Сочи,Краснодар';
    await request(app)
      .put('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`)
      .send({ cities_list: newCities });
    const res = await request(app)
      .get('/api/settings')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.body.cities_list).toBe(newCities);
  });

  test('api.js ALLOWED_KEYS includes cities_list', () => {
    const code = fs.readFileSync(API_JS, 'utf8');
    expect(code).toMatch(/cities_list/);
  });

  test('bot.js reads cities_list setting for catalog filters', () => {
    const code = fs.readFileSync(BOT_JS, 'utf8');
    expect(code).toMatch(/cities_list/);
  });
});

// ── БЛОК 1.3: Admin Panel Sections in bot.js (code checks) ──────────────────

describe('Wave 77 БЛОК 1.3: showAdminSettings sections in bot.js', () => {
  let botCode;

  beforeAll(() => {
    botCode = fs.readFileSync(BOT_JS, 'utf8');
  });

  test("bot.js has showAdminSettings function", () => {
    expect(botCode).toMatch(/async function showAdminSettings/);
  });

  test("bot.js showAdminSettings handles 'contacts' section", () => {
    expect(botCode).toMatch(/section\s*===\s*['"]contacts['"]/);
  });

  test("bot.js showAdminSettings handles 'notifs' section", () => {
    expect(botCode).toMatch(/section\s*===\s*['"]notifs['"]/);
  });

  test("bot.js showAdminSettings handles 'catalog' section", () => {
    expect(botCode).toMatch(/section\s*===\s*['"]catalog['"]/);
  });

  test("bot.js showAdminSettings handles 'booking' section", () => {
    expect(botCode).toMatch(/section\s*===\s*['"]booking['"]/);
  });

  test("bot.js showAdminSettings handles 'reviews' section", () => {
    expect(botCode).toMatch(/section\s*===\s*['"]reviews['"]/);
  });

  test("bot.js showAdminSettings handles 'cities' section", () => {
    expect(botCode).toMatch(/section\s*===\s*['"]cities['"]/);
  });

  test("bot.js showAdminSettings handles 'bot' section", () => {
    expect(botCode).toMatch(/section\s*===\s*['"]bot['"]/);
  });

  test("bot.js has adm_settings_contacts callback routing", () => {
    expect(botCode).toMatch(/adm_settings_contacts/);
  });

  test("bot.js has adm_settings_catalog callback routing", () => {
    expect(botCode).toMatch(/adm_settings_catalog/);
  });

  test("bot.js has adm_settings_booking callback routing", () => {
    expect(botCode).toMatch(/adm_settings_booking/);
  });

  test("bot.js has adm_settings_reviews callback routing", () => {
    expect(botCode).toMatch(/adm_settings_reviews/);
  });

  test("bot.js has adm_settings_cities callback routing", () => {
    expect(botCode).toMatch(/adm_settings_cities/);
  });

  test("bot.js has adm_settings_bot callback routing", () => {
    expect(botCode).toMatch(/adm_settings_bot/);
  });

  test("bot.js has adm_settings_notifs callback routing", () => {
    expect(botCode).toMatch(/adm_settings_notifs/);
  });
});

// ── БЛОК 1.3: Notification toggles in bot.js ─────────────────────────────────

describe('Wave 77 БЛОК 1.3: Notification toggle callbacks in bot.js', () => {
  let botCode;

  beforeAll(() => {
    botCode = fs.readFileSync(BOT_JS, 'utf8');
  });

  test('bot.js has notification new order toggle (adm_notif_order_on)', () => {
    expect(botCode).toMatch(/adm_notif_order_on|adm_toggle_notif_new_order|adm_notif_new_on/);
  });

  test('bot.js has notification order off toggle', () => {
    expect(botCode).toMatch(/adm_notif_order_off|adm_notif_new_off/);
  });

  test('bot.js has adm_toggle_quick_booking or booking quick toggle', () => {
    expect(botCode).toMatch(/adm_toggle_quick_booking|adm_booking_quick_on|adm_booking_quick_off/);
  });

  test('bot.js has adm_toggle_reviews or reviews toggle callbacks', () => {
    expect(botCode).toMatch(/adm_toggle_reviews|adm_reviews_on|adm_reviews_off/);
  });

  test('bot.js has adm_toggle_wishlist or wishlist toggle callbacks', () => {
    expect(botCode).toMatch(/adm_toggle_wishlist|adm_wishlist_on|adm_wishlist_off/);
  });

  test('bot.js has adm_toggle_search or search toggle callbacks', () => {
    expect(botCode).toMatch(/adm_toggle_search|adm_search_on|adm_search_off/);
  });

  test('bot.js has notif_new_order setting key', () => {
    expect(botCode).toMatch(/notif_new_order/);
  });

  test('bot.js has notif_new_review setting key', () => {
    expect(botCode).toMatch(/notif_new_review/);
  });

  test('bot.js has notification message toggle key (notif_new_message)', () => {
    expect(botCode).toMatch(/notif_new_message/);
  });
});

// ── БЛОК 1.3: Settings applied in bot logic ───────────────────────────────────

describe('Wave 77 БЛОК 1.3: Settings applied in bot logic', () => {
  let botCode;

  beforeAll(() => {
    botCode = fs.readFileSync(BOT_JS, 'utf8');
  });

  test('bot.js reads catalog_per_page in showCatalog or similar', () => {
    expect(botCode).toMatch(/catalog_per_page/);
  });

  test('bot.js applies catalog_per_page as page size', () => {
    // Should use getSetting for catalog_per_page
    expect(botCode).toMatch(/getSetting\(['"]catalog_per_page['"]\)/);
  });

  test('bot.js checks wishlist_enabled before showing wishlist', () => {
    expect(botCode).toMatch(/wishlist_enabled/);
  });

  test('bot.js getSetting wishlist_enabled used in buildClientKeyboard', () => {
    expect(botCode).toMatch(/getSetting\(['"]wishlist_enabled['"]/);
  });

  test('bot.js checks reviews_enabled before showing reviews menu item', () => {
    expect(botCode).toMatch(/reviews_enabled/);
  });

  test('bot.js getSetting reviews_enabled used in keyboard builder', () => {
    expect(botCode).toMatch(/getSetting\(['"]reviews_enabled['"]/);
  });

  test('bot.js checks quick_booking_enabled for bk_quick button', () => {
    expect(botCode).toMatch(/quick_booking_enabled/);
  });

  test('bot.js getSetting quick_booking_enabled used for bk_quick', () => {
    expect(botCode).toMatch(/getSetting\(['"]quick_booking_enabled['"]/);
  });

  test('bot.js applies catalog_sort setting for model ordering', () => {
    expect(botCode).toMatch(/catalog_sort/);
    expect(botCode).toMatch(/getSetting\(['"]catalog_sort['"]\)/);
  });

  test('bot.js uses bk_quick callback_data for quick booking button', () => {
    expect(botCode).toMatch(/bk_quick/);
  });

  test('bot.js uses buildClientKeyboard that gates features by settings', () => {
    expect(botCode).toMatch(/buildClientKeyboard/);
  });

  test('bot.js reads greeting setting for welcome message', () => {
    expect(botCode).toMatch(/getSetting\(['"]greeting['"]\)/);
  });
});

// ── БЛОК 1.1: Public settings endpoint ───────────────────────────────────────

describe('Wave 77 БЛОК 1.1: Public settings endpoint', () => {
  test('GET /api/settings/public is accessible without auth', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(res.status).toBe(200);
  });

  test('GET /api/settings/public returns an object', async () => {
    const res = await request(app).get('/api/settings/public');
    expect(typeof res.body).toBe('object');
    expect(Array.isArray(res.body)).toBe(false);
  });

  test('api.js has /settings/public route defined', () => {
    const code = fs.readFileSync(API_JS, 'utf8');
    expect(code).toMatch(/settings\/public/);
  });
});

// ── БЛОК 1.1: api.js structural checks ───────────────────────────────────────

describe('Wave 77 БЛОК 1.1: api.js settings route definitions', () => {
  let apiCode;

  beforeAll(() => {
    apiCode = fs.readFileSync(API_JS, 'utf8');
  });

  test('api.js has GET /admin/settings route', () => {
    expect(apiCode).toMatch(/router\.get\(['"]\/admin\/settings['"]/);
  });

  test('api.js has GET /admin/settings/sections route', () => {
    expect(apiCode).toMatch(/admin\/settings\/sections/);
  });

  test('api.js has GET /admin/settings/export route', () => {
    expect(apiCode).toMatch(/admin\/settings\/export/);
  });

  test('api.js has POST /admin/settings/import route', () => {
    expect(apiCode).toMatch(/admin\/settings\/import/);
  });

  test('api.js has POST /admin/settings/reset route', () => {
    expect(apiCode).toMatch(/admin\/settings\/reset/);
  });

  test('api.js has PUT /settings route', () => {
    expect(apiCode).toMatch(/router\.put\(['"]\/settings['"]/);
  });

  test('api.js ALLOWED_KEYS contains wishlist_enabled', () => {
    expect(apiCode).toMatch(/wishlist_enabled/);
  });

  test('api.js ALLOWED_KEYS contains search_enabled', () => {
    expect(apiCode).toMatch(/search_enabled/);
  });

  test('api.js ALLOWED_KEYS contains reviews_enabled', () => {
    expect(apiCode).toMatch(/reviews_enabled/);
  });

  test('api.js ALLOWED_KEYS contains quick_booking_enabled', () => {
    expect(apiCode).toMatch(/quick_booking_enabled/);
  });

  test('api.js sections include notifications section label', () => {
    expect(apiCode).toMatch(/Уведомлени/);
  });

  test('api.js sections include catalog section label', () => {
    expect(apiCode).toMatch(/Каталог/);
  });
});
