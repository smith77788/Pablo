'use strict';
// Use in-memory SQLite for tests
process.env.DB_PATH = ':memory:';
const { initDB, query, get, run } = require('../database');

describe('database', () => {
  beforeAll(async () => {
    await initDB();
  });

  // ── Schema existence ──────────────────────────────────────────────────────
  const tables = ['models', 'orders', 'bot_settings', 'reviews', 'wishlists', 'faq', 'admins', 'scheduled_broadcasts'];
  tables.forEach(t => {
    test(`${t} table exists`, async () => {
      const r = await get(`SELECT name FROM sqlite_master WHERE type='table' AND name=?`, [t]);
      expect(r).toBeTruthy();
    });
  });

  // ── Models CRUD ───────────────────────────────────────────────────────────
  test('can insert and retrieve a model', async () => {
    await run("INSERT INTO models (name, city, category, available) VALUES (?, ?, ?, ?)",
      ['Тест Модель', 'Москва', 'fashion', 1]);
    const model = await get("SELECT * FROM models WHERE name = ?", ['Тест Модель']);
    expect(model).toBeTruthy();
    expect(model.name).toBe('Тест Модель');
    expect(model.city).toBe('Москва');
  });

  test('can query multiple models', async () => {
    const models = await query("SELECT * FROM models WHERE available = 1");
    expect(Array.isArray(models)).toBe(true);
    expect(models.length).toBeGreaterThan(0);
  });

  test('model has required columns', async () => {
    const m = await get('SELECT * FROM models LIMIT 1');
    if (!m) return; // no data, skip
    expect(m).toHaveProperty('name');
    expect(m).toHaveProperty('available');
    expect(m).toHaveProperty('featured');
  });

  // ── Orders ────────────────────────────────────────────────────────────────
  test('can insert an order', async () => {
    const model = await get('SELECT id FROM models LIMIT 1');
    const r = await run(
      "INSERT INTO orders (order_number, client_name, client_phone, event_type, status, model_id) VALUES (?,?,?,?,?,?)",
      ['TEST-001', 'Іван Тест', '+79991234567', 'photo', 'new', model ? model.id : null]
    );
    expect(r.id).toBeGreaterThan(0);
    const o = await get('SELECT * FROM orders WHERE id=?', [r.id]);
    expect(o.client_name).toBe('Іван Тест');
    expect(o.status).toBe('new');
  });

  test('order status update works', async () => {
    const r = await run(
      "INSERT INTO orders (order_number, client_name, client_phone, status, event_type) VALUES (?,?,?,?,?)",
      ['TEST-002', 'Тест Статус', '+70000000000', 'new', 'other']
    );
    await run("UPDATE orders SET status=? WHERE id=?", ['confirmed', r.id]);
    const o = await get('SELECT status FROM orders WHERE id=?', [r.id]);
    expect(o.status).toBe('confirmed');
  });

  // ── Reviews ───────────────────────────────────────────────────────────────
  test('can insert a review', async () => {
    const r = await run(
      "INSERT INTO reviews (client_name, rating, text, approved) VALUES (?,?,?,?)",
      ['Клієнт Тест', 5, 'Відмінна робота!', 0]
    );
    expect(r.id).toBeGreaterThan(0);
    const rev = await get('SELECT * FROM reviews WHERE id=?', [r.id]);
    expect(rev.rating).toBe(5);
    expect(rev.approved).toBe(0);
  });

  test('admin_reply column exists in reviews', async () => {
    const info = await query("PRAGMA table_info(reviews)");
    const hasReply = info.some(c => c.name === 'admin_reply');
    expect(hasReply).toBe(true);
  });

  // ── Wishlists ─────────────────────────────────────────────────────────────
  test('can add to wishlist', async () => {
    const model = await get('SELECT id FROM models LIMIT 1');
    if (!model) return;
    await run("INSERT OR IGNORE INTO wishlists (chat_id, model_id) VALUES (?,?)", [999999, model.id]);
    const w = await get('SELECT * FROM wishlists WHERE chat_id=? AND model_id=?', [999999, model.id]);
    expect(w).toBeTruthy();
  });

  test('wishlist unique constraint prevents duplicates', async () => {
    const model = await get('SELECT id FROM models LIMIT 1');
    if (!model) return;
    await run("INSERT OR IGNORE INTO wishlists (chat_id, model_id) VALUES (?,?)", [888888, model.id]);
    await run("INSERT OR IGNORE INTO wishlists (chat_id, model_id) VALUES (?,?)", [888888, model.id]);
    const rows = await query('SELECT * FROM wishlists WHERE chat_id=?', [888888]);
    expect(rows.filter(r => r.model_id === model.id)).toHaveLength(1);
  });

  // ── Settings ──────────────────────────────────────────────────────────────
  test('bot_settings has default values', async () => {
    const rows = await query('SELECT key FROM bot_settings');
    expect(rows.length).toBeGreaterThan(0);
  });

  test('can read and write settings', async () => {
    await run(
      "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?,?)",
      ['test_setting_xyz', 'test_value_123']
    );
    const s = await get("SELECT value FROM bot_settings WHERE key=?", ['test_setting_xyz']);
    expect(s.value).toBe('test_value_123');
  });

  // ── FAQ ───────────────────────────────────────────────────────────────────
  test('faq has seeded items', async () => {
    const rows = await query('SELECT * FROM faq WHERE active=1');
    expect(rows.length).toBeGreaterThan(0);
  });

  test('can insert a faq item', async () => {
    const r = await run(
      "INSERT INTO faq (question, answer, sort_order, active) VALUES (?,?,?,?)",
      ['Тест питання?', 'Тест відповідь.', 99, 1]
    );
    expect(r.id).toBeGreaterThan(0);
  });

  // ── Broadcasts ────────────────────────────────────────────────────────────
  test('can insert a scheduled broadcast', async () => {
    const r = await run(
      "INSERT INTO scheduled_broadcasts (text, segment, scheduled_at) VALUES (?,?,datetime('now','+1 hour'))",
      ['Тест розсилка', 'all']
    );
    expect(r.id).toBeGreaterThan(0);
    const b = await get('SELECT * FROM scheduled_broadcasts WHERE id=?', [r.id]);
    expect(b.status).toBe('pending');
  });

  test('photo_url column exists in scheduled_broadcasts', async () => {
    const info = await query("PRAGMA table_info(scheduled_broadcasts)");
    const hasPhoto = info.some(c => c.name === 'photo_url');
    expect(hasPhoto).toBe(true);
  });
});
