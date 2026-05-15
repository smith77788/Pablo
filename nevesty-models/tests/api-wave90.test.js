'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const dbCode = fs.readFileSync(path.join(ROOT, 'database.js'), 'utf8');
const bookingHtml = fs.readFileSync(path.join(ROOT, 'public/booking.html'), 'utf8');

// ─── T1: booking_thanks_text sent after submission ────────────────────────────

describe('T1: booking_thanks_text sent after submission', () => {
  test('T01: bkSubmit reads booking_thanks_text setting', () => {
    const idx = botCode.indexOf('async function bkSubmit');
    expect(idx).toBeGreaterThan(-1);
    const fnBody = botCode.slice(idx, idx + 6000);
    expect(fnBody).toMatch(/getSetting\s*\(\s*['"]booking_thanks_text['"]/);
  });

  test('T02: bkSubmit sends thanks text after main confirmation', () => {
    const idx = botCode.indexOf('async function bkSubmit');
    expect(idx).toBeGreaterThan(-1);
    const fnBody = botCode.slice(idx, idx + 6000);
    // should find thanks text and then send it
    const thanksIdx = fnBody.indexOf('booking_thanks_text');
    expect(thanksIdx).toBeGreaterThan(-1);
    const afterThanks = fnBody.slice(thanksIdx, thanksIdx + 300);
    expect(afterThanks).toMatch(/safeSend\s*\(/);
  });

  test('T03: thanks text uses esc() for MarkdownV2 safety', () => {
    const idx = botCode.indexOf('async function bkSubmit');
    expect(idx).toBeGreaterThan(-1);
    const fnBody = botCode.slice(idx, idx + 6000);
    const thanksIdx = fnBody.indexOf('booking_thanks_text');
    expect(thanksIdx).toBeGreaterThan(-1);
    const afterThanks = fnBody.slice(thanksIdx, thanksIdx + 300);
    expect(afterThanks).toMatch(/esc\s*\(/);
  });
});

// ─── T2: Wishlist DB schema ───────────────────────────────────────────────────

describe('T2: Wishlist DB schema', () => {
  test('T04: wishlists table has UNIQUE(chat_id, model_id)', () => {
    expect(dbCode).toMatch(/UNIQUE\s*\(\s*chat_id\s*,\s*model_id\s*\)/);
  });

  test('T05: fav_add handler inserts with INSERT OR IGNORE', () => {
    const idx = botCode.indexOf("data.startsWith('fav_add_')");
    expect(idx).toBeGreaterThan(-1);
    // addToWishlist function should use INSERT OR IGNORE
    expect(botCode).toMatch(/INSERT OR IGNORE INTO wishlists/);
  });

  test('T06: fav_remove handler deletes from wishlists', () => {
    const idx = botCode.indexOf("data.startsWith('fav_remove_')");
    expect(idx).toBeGreaterThan(-1);
    // removeFromWishlist function should delete from wishlists
    expect(botCode).toMatch(/DELETE FROM wishlists WHERE chat_id/);
  });

  test('T07: wishlist_enabled setting gates the keyboard button', () => {
    // The wishlist button should only appear when wishlist_enabled !== '0'
    const idx = botCode.indexOf('wishlist_enabled');
    expect(idx).toBeGreaterThan(-1);
    // Check that code reads the setting and uses it to conditionally show buttons
    const nearby = botCode.slice(idx, idx + 500);
    expect(nearby).toMatch(/wishlist_enabled/);
    // Verify the enabled check pattern exists in bot
    expect(botCode).toMatch(/wishlistEnabled\s*!==\s*['"]0['"]/);
  });
});

// ─── T3: Session warning system ───────────────────────────────────────────────

describe('T3: Session warning system', () => {
  test('T08: sessionWarningTimers Map exists', () => {
    expect(botCode).toMatch(/const\s+sessionWarningTimers\s*=\s*new\s+Map/);
  });

  test('T09: setSessionWarning function exists and schedules warning', () => {
    expect(botCode).toMatch(/function\s+setSessionWarning\s*\(\s*chatId\s*\)/);
    const idx = botCode.indexOf('function setSessionWarning');
    expect(idx).toBeGreaterThan(-1);
    const fnBody = botCode.slice(idx, idx + 500);
    expect(fnBody).toMatch(/setTimeout/);
  });

  test('T10: clearSessionWarning clears the timer', () => {
    expect(botCode).toMatch(/function\s+clearSessionWarning\s*\(\s*chatId\s*\)/);
    const idx = botCode.indexOf('function clearSessionWarning');
    expect(idx).toBeGreaterThan(-1);
    const fnBody = botCode.slice(idx, idx + 200);
    expect(fnBody).toMatch(/clearTimeout/);
  });

  test('T11: /cancel command calls clearSessionWarning', () => {
    const idx = botCode.indexOf('/cancel/');
    expect(idx).toBeGreaterThan(-1);
    // Find the onText handler for /cancel
    const handlerIdx = botCode.indexOf('bot.onText(/\\/cancel/');
    expect(handlerIdx).toBeGreaterThan(-1);
    const handlerBody = botCode.slice(handlerIdx, handlerIdx + 600);
    expect(handlerBody).toMatch(/clearSessionWarning\s*\(\s*chatId\s*\)/);
  });
});

// ─── T4: Schema versions coverage ────────────────────────────────────────────

describe('T4: Schema versions coverage', () => {
  test('T12: database.js has schema v19 (social_posts)', () => {
    expect(dbCode).toMatch(/schema.*v(?:ersion)?.*19|VALUES\s*\(\s*19\s*,/i);
    expect(dbCode).toMatch(/social_posts/);
  });

  test('T13: database.js has schema v20 (reviews UNIQUE index)', () => {
    expect(dbCode).toMatch(/VALUES\s*\(\s*20\s*,/);
    expect(dbCode).toMatch(/UNIQUE.*index.*reviews|idx_reviews_unique/i);
  });

  test('T14: database.js has schema v21 (wishlists v2)', () => {
    expect(dbCode).toMatch(/VALUES\s*\(\s*21\s*,/);
    // v21 re-creates wishlists table
    const v21Idx = dbCode.indexOf('VALUES(21,');
    expect(v21Idx).toBeGreaterThan(-1);
  });

  test('T15: database.js has schema v22 (model archive)', () => {
    expect(dbCode).toMatch(/VALUES\s*\(\s*22\s*,/i);
    const v22Idx = dbCode.indexOf('version=22');
    expect(v22Idx).toBeGreaterThan(-1);
    const v22Body = dbCode.slice(v22Idx, v22Idx + 300);
    expect(v22Body).toMatch(/archived/);
  });
});

// ─── T5: Booking form autosave ────────────────────────────────────────────────

describe('T5: Booking form autosave', () => {
  test('T16: booking.html has nm_booking_progress key', () => {
    expect(bookingHtml).toMatch(/nm_booking_progress/);
  });

  test('T17: booking.html reads draft on DOMContentLoaded', () => {
    const idx = bookingHtml.indexOf('DOMContentLoaded');
    expect(idx).toBeGreaterThan(-1);
    const nearby = bookingHtml.slice(idx, idx + 300);
    expect(nearby).toMatch(/loadBookingProgress\s*\(\s*\)|localStorage\.getItem/);
  });

  test('T18: booking.html has clearBookingProgress (clears on submit)', () => {
    expect(bookingHtml).toMatch(/function\s+clearBookingProgress\s*\(\s*\)/);
    // clearBookingProgress must call localStorage.removeItem
    const idx = bookingHtml.indexOf('function clearBookingProgress');
    expect(idx).toBeGreaterThan(-1);
    const fnBody = bookingHtml.slice(idx, idx + 150);
    expect(fnBody).toMatch(/localStorage\.removeItem/);
  });
});
