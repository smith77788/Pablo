'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const dbCode = fs.readFileSync(path.join(ROOT, 'database.js'), 'utf8');

describe('T1: Wishlist database migration', () => {
  test('T01: schema v21 creates wishlists table', () => {
    expect(dbCode).toMatch(/CREATE TABLE IF NOT EXISTS wishlists/);
  });
  test('T02: wishlists has UNIQUE(chat_id, model_id)', () => {
    expect(dbCode).toMatch(/UNIQUE\(chat_id, model_id\)|UNIQUE.*chat_id.*model_id/);
  });
  test('T03: idx_wishlists_chat index created', () => {
    expect(dbCode).toMatch(/idx_wishlists_chat/);
  });
});

describe('T2: Wishlist bot callbacks', () => {
  test('T04: fav_add_ handler exists', () => {
    expect(botCode).toMatch(/fav_add_/);
  });
  test('T05: fav_remove_ handler exists', () => {
    expect(botCode).toMatch(/fav_remove_/);
  });
  test('T06: fav_list handler exists', () => {
    expect(botCode).toMatch(/fav_list/);
  });
  test('T07: showWishlist function exists', () => {
    expect(botCode).toMatch(/async function showWishlist/);
  });
  test('T08: fav_add inserts into wishlists', () => {
    // addToWishlist helper is called by the fav_add_ handler and contains the INSERT
    const idx = botCode.indexOf('addToWishlist');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 500);
    expect(nearby).toMatch(/INSERT.*wishlists|wishlists.*INSERT/i);
  });
  test('T09: fav_remove deletes from wishlists', () => {
    // removeFromWishlist helper is called by the fav_remove_ handler and contains the DELETE
    const idx = botCode.indexOf('removeFromWishlist');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 500);
    expect(nearby).toMatch(/DELETE.*wishlists|wishlists.*DELETE/i);
  });
});

describe('T3: Wishlist in main keyboard', () => {
  test('T10: buildClientKeyboard checks wishlist_enabled', () => {
    const idx = botCode.indexOf('async function buildClientKeyboard');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 800);
    expect(nearby).toMatch(/wishlist_enabled/);
  });
  test('T11: Избранное button with fav_list callback', () => {
    expect(botCode).toMatch(/Избранное.*fav_list|fav_list.*Избранное/s);
  });
});
