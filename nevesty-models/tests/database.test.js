'use strict';
// Use in-memory SQLite for tests
process.env.DB_PATH = ':memory:';
const { initDB, query, get, run } = require('../database');

describe('database', () => {
  beforeAll(async () => {
    await initDB();
  });

  test('models table exists', async () => {
    const result = await get("SELECT name FROM sqlite_master WHERE type='table' AND name='models'");
    expect(result).toBeTruthy();
    expect(result.name).toBe('models');
  });

  test('orders table exists', async () => {
    const result = await get("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'");
    expect(result).toBeTruthy();
  });

  test('bot_settings table exists', async () => {
    const result = await get("SELECT name FROM sqlite_master WHERE type='table' AND name='bot_settings'");
    expect(result).toBeTruthy();
  });

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
});
