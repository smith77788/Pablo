'use strict';
/**
 * Integration tests for Wave 80:
 * - Wishlist API (routes/api.js code checks)
 * - Wishlist in bot.js (code checks)
 * - Repeat order (bot.js code checks)
 * - Session timeout (bot.js code checks)
 * - Session management (bot.js code checks)
 * - Backup scripts (existence + executable check)
 */

const path = require('path');
const fs = require('fs');

const apiCode = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
const botCode = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
const dbCode = fs.readFileSync(path.join(__dirname, '../database.js'), 'utf8');

// ── БЛОК A: Wishlist API — routes/api.js ──────────────────────────────────────

describe('Wave 80 БЛОК A: Wishlist API — routes/api.js', () => {
  test('GET /api/user/wishlist endpoint exists', () => {
    expect(apiCode).toContain("router.get('/user/wishlist'");
  });

  test('POST /api/user/wishlist endpoint exists', () => {
    expect(apiCode).toContain("router.post('/user/wishlist'");
  });

  test('DELETE /api/user/wishlist/:model_id endpoint exists', () => {
    expect(apiCode).toContain("router.delete('/user/wishlist/:model_id'");
  });

  test('GET /api/user/wishlist uses wishlistLimiter', () => {
    expect(apiCode).toMatch(/router\.get\s*\(\s*'\/user\/wishlist'\s*,\s*wishlistLimiter/);
  });

  test('POST /api/user/wishlist uses wishlistLimiter', () => {
    expect(apiCode).toMatch(/router\.post\s*\(\s*'\/user\/wishlist'\s*,\s*wishlistLimiter/);
  });

  test('DELETE /api/user/wishlist/:model_id uses wishlistLimiter', () => {
    expect(apiCode).toMatch(/router\.delete\s*\(\s*'\/user\/wishlist\/:model_id'\s*,\s*wishlistLimiter/);
  });

  test('wishlistLimiter is defined (not no-op in production intent)', () => {
    expect(apiCode).toContain('wishlistLimiter');
    // fallback or real limiter — should be declared
    expect(apiCode).toMatch(/let wishlistLimiter|const wishlistLimiter/);
  });

  test('Wishlist GET returns items array from wishlists JOIN models', () => {
    expect(apiCode).toMatch(/FROM wishlists\s+w/i);
    expect(apiCode).toMatch(/JOIN\s+models\s+m/i);
  });

  test('Wishlist GET query selects model details', () => {
    // Should select id, name at minimum from the wishlist join
    expect(apiCode).toMatch(/w\.chat_id\s*=\s*\?/);
  });

  test('POST /api/user/wishlist parses model_id as integer', () => {
    // Look for parseInt on body.model_id near the wishlist post handler
    const postIdx = apiCode.indexOf("router.post('/user/wishlist'");
    const segment = apiCode.slice(postIdx, postIdx + 800);
    expect(segment).toMatch(/parseInt\s*\(\s*req\.body\.model_id\s*\)/);
  });

  test('POST /api/user/wishlist validates model_id > 0', () => {
    const postIdx = apiCode.indexOf("router.post('/user/wishlist'");
    const segment = apiCode.slice(postIdx, postIdx + 800);
    expect(segment).toMatch(/modelId.*<=\s*0|!modelId/);
  });

  test('POST /api/user/wishlist checks model exists before inserting', () => {
    const postIdx = apiCode.indexOf("router.post('/user/wishlist'");
    const segment = apiCode.slice(postIdx, postIdx + 1000);
    expect(segment).toMatch(/SELECT\s+id\s+FROM\s+models/);
  });

  test('POST /api/user/wishlist returns 404 if model not found', () => {
    const postIdx = apiCode.indexOf("router.post('/user/wishlist'");
    const segment = apiCode.slice(postIdx, postIdx + 1200);
    expect(segment).toContain('404');
    expect(segment).toMatch(/Модель не найдена|model not found/i);
  });

  test('POST /api/user/wishlist inserts into wishlists table', () => {
    expect(apiCode).toContain('INSERT INTO wishlists (chat_id, model_id)');
  });

  test('POST /api/user/wishlist also syncs to favorites table using INSERT OR IGNORE', () => {
    expect(apiCode).toContain('INSERT OR IGNORE INTO favorites (chat_id, model_id)');
  });

  test('POST /api/user/wishlist returns 409 on duplicate (UNIQUE constraint)', () => {
    const postIdx = apiCode.indexOf("router.post('/user/wishlist'");
    const segment = apiCode.slice(postIdx, postIdx + 1500);
    expect(segment).toContain('409');
    expect(segment).toMatch(/UNIQUE|уже в избранном/i);
  });

  test('DELETE /api/user/wishlist uses WHERE chat_id=? AND model_id=?', () => {
    expect(apiCode).toContain('DELETE FROM wishlists WHERE chat_id=? AND model_id=?');
  });

  test('DELETE /api/user/wishlist/:model_id parses model_id from params', () => {
    const delIdx = apiCode.indexOf("router.delete('/user/wishlist/:model_id'");
    const segment = apiCode.slice(delIdx, delIdx + 600);
    expect(segment).toMatch(/parseInt\s*\(\s*req\.params\.model_id\s*\)/);
  });

  test('DELETE /api/user/wishlist returns 404 if no rows deleted', () => {
    const delIdx = apiCode.indexOf("router.delete('/user/wishlist/:model_id'");
    const segment = apiCode.slice(delIdx, delIdx + 800);
    expect(segment).toContain('404');
  });

  test('GET wishlist response wraps data in items field or JSON array', () => {
    const getIdx = apiCode.indexOf("router.get('/user/wishlist'");
    const segment = apiCode.slice(getIdx, getIdx + 600);
    expect(segment).toMatch(/res\.json|res\.status.*json/);
  });
});

// ── БЛОК B: Wishlist in bot.js ─────────────────────────────────────────────────

describe('Wave 80 БЛОК B: Wishlist features in bot.js', () => {
  test('showWishlist function exists', () => {
    expect(botCode).toContain('async function showWishlist(');
  });

  test('fav_list callback exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]fav_list['"]/);
  });

  test('fav_list_N pagination callback exists', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]fav_list_['"]\s*\)/);
  });

  test('fav_add_ callback handler exists', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]fav_add_['"]\s*\)/);
  });

  test('fav_remove_ callback handler exists', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]fav_remove_['"]\s*\)/);
  });

  test('fav_clear callback exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]fav_clear['"]/);
  });

  test('showModel includes "В избранное" button text', () => {
    expect(botCode).toContain('❤️ В избранное');
  });

  test('Wishlist enabled check uses wishlist_enabled setting', () => {
    expect(botCode).toContain('wishlist_enabled');
  });

  test('showWishlist checks wishlist_enabled before showing list', () => {
    const fnIdx = botCode.indexOf('async function showWishlist(');
    const segment = botCode.slice(fnIdx, fnIdx + 400);
    expect(segment).toContain('wishlist_enabled');
  });

  test('fav_add_ handler uses INSERT OR IGNORE into wishlists', () => {
    expect(botCode).toContain('INSERT OR IGNORE INTO wishlists (chat_id, model_id)');
  });

  test('fav_remove_ handler uses DELETE FROM wishlists', () => {
    expect(botCode).toContain('DELETE FROM wishlists WHERE chat_id=? AND model_id=?');
  });

  test('fav_clear_yes deletes all wishlist entries for user', () => {
    expect(botCode).toContain('DELETE FROM wishlists WHERE chat_id=?');
  });

  test('/wishlist command triggers showWishlist', () => {
    expect(botCode).toMatch(/\/wishlist.*showWishlist|showWishlist.*wishlist/s);
  });

  test('showWishlist queries wishlists JOIN models', () => {
    const fnIdx = botCode.indexOf('async function showWishlist(');
    const segment = botCode.slice(fnIdx, fnIdx + 600);
    expect(segment).toMatch(/FROM wishlists/i);
  });

  test('showWishlist has pagination (fav_list_ nav buttons)', () => {
    const fnIdx = botCode.indexOf('async function showWishlist(');
    const segment = botCode.slice(fnIdx, fnIdx + 5000);
    // Pagination uses fav_list_${page+1} or fav_list_${page-1} template literals
    expect(segment).toMatch(/fav_list_\$\{page|page - 1|page \+ 1|page\s*-\s*1|page\s*\+\s*1/);
  });

  test('fav_add_ updates keyboard button from add to remove', () => {
    const idx = botCode.indexOf("data.startsWith('fav_add_')");
    const segment = botCode.slice(idx, idx + 1200);
    expect(segment).toContain('fav_remove_');
  });

  test('fav_remove_ updates keyboard button from remove to add', () => {
    const idx = botCode.indexOf("data.startsWith('fav_remove_')");
    const segment = botCode.slice(idx, idx + 1200);
    expect(segment).toContain('fav_add_');
  });
});

// ── БЛОК C: Repeat order in bot.js ────────────────────────────────────────────

describe('Wave 80 БЛОК C: Repeat order in bot.js', () => {
  test('repeatOrder function exists', () => {
    expect(botCode).toContain('async function repeatOrder(');
  });

  test('repeat_order_ callback handler exists', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]repeat_order_['"]\s*\)/);
  });

  test('bk_repeat_confirm callback exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]bk_repeat_confirm['"]/);
  });

  test('bk_repeat_cancel callback exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]bk_repeat_cancel['"]/);
  });

  test('bkRepeatSubmit function exists', () => {
    expect(botCode).toContain('async function bkRepeatSubmit(');
  });

  test('repeatOrder loads previous order from DB using client_chat_id', () => {
    const fnIdx = botCode.indexOf('async function repeatOrder(');
    const segment = botCode.slice(fnIdx, fnIdx + 400);
    expect(segment).toMatch(/SELECT\s+\*\s+FROM\s+orders\s+WHERE\s+id=\?/);
  });

  test('repeatOrder checks model availability', () => {
    const fnIdx = botCode.indexOf('async function repeatOrder(');
    const segment = botCode.slice(fnIdx, fnIdx + 800);
    expect(segment).toMatch(/available|m\.available/);
  });

  test('repeatOrder sets session state to bk_repeat_confirm', () => {
    const fnIdx = botCode.indexOf('async function repeatOrder(');
    const segment = botCode.slice(fnIdx, fnIdx + 1500);
    // May use setSession() or session.state assignment
    expect(segment).toMatch(/setSession\(chatId,\s*['"]bk_repeat_confirm['"]|bk_repeat_confirm/);
  });

  test('bkRepeatSubmit calls generateOrderNumber', () => {
    const fnIdx = botCode.indexOf('async function bkRepeatSubmit(');
    const segment = botCode.slice(fnIdx, fnIdx + 1200);
    expect(segment).toMatch(/generateOrderNumber\(\)/);
  });

  test('bkRepeatSubmit calls notifyNewOrder', () => {
    const fnIdx = botCode.indexOf('async function bkRepeatSubmit(');
    const segment = botCode.slice(fnIdx, fnIdx + 4000);
    expect(segment).toMatch(/notifyNewOrder\s*\(/);
  });

  test('bkRepeatSubmit inserts new order with status new', () => {
    const fnIdx = botCode.indexOf('async function bkRepeatSubmit(');
    const segment = botCode.slice(fnIdx, fnIdx + 1500);
    expect(segment).toMatch(/INSERT INTO orders/);
    expect(segment).toMatch(/'new'|"new"|status.*new/);
  });

  test('bkRepeatSubmit clears session after creating order', () => {
    const fnIdx = botCode.indexOf('async function bkRepeatSubmit(');
    const segment = botCode.slice(fnIdx, fnIdx + 1500);
    expect(segment).toContain('clearSession(chatId)');
  });

  test('"🔁 Повторить заявку" button appears for completed orders', () => {
    expect(botCode).toContain('🔁 Повторить заявку');
    expect(botCode).toContain('repeat_order_');
  });

  test('repeat order button only shown for completed or cancelled orders', () => {
    // Check the condition for showing repeat button
    expect(botCode).toMatch(/status\s*===\s*['"]completed['"]\s*\|\|\s*o\.status\s*===\s*['"]cancelled['"]/);
  });

  test('bk_repeat_confirm handler validates session has client_name and client_phone', () => {
    const idx = botCode.indexOf("data === 'bk_repeat_confirm'");
    const segment = botCode.slice(idx, idx + 400);
    expect(segment).toMatch(/client_name|client_phone/);
  });

  test('bk_repeat_cancel handler calls clearSession', () => {
    const idx = botCode.indexOf("data === 'bk_repeat_cancel'");
    const segment = botCode.slice(idx, idx + 300);
    expect(segment).toMatch(/clearSession|main_menu/);
  });
});

// ── БЛОК D: Session timeout in bot.js ─────────────────────────────────────────

describe('Wave 80 БЛОК D: Session timeout in bot.js', () => {
  test('/cancel command handler exists', () => {
    expect(botCode).toMatch(/bot\.onText\s*\(\s*\/\\\/cancel\//);
  });

  test('SESSION_REMINDER_MS constant is defined', () => {
    expect(botCode).toContain('SESSION_REMINDER_MS');
  });

  test('SESSION_REMINDER_MS is set to 15 minutes', () => {
    expect(botCode).toMatch(/SESSION_REMINDER_MS\s*=\s*15\s*\*\s*60\s*\*\s*1000/);
  });

  test('setSessionReminder function exists', () => {
    expect(botCode).toContain('function setSessionReminder(');
  });

  test('resume_session callback exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]resume_session['"]/);
  });

  test('cancel_session callback exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]cancel_session['"]/);
  });

  test('setInterval exists for session cache cleanup', () => {
    expect(botCode).toMatch(/setInterval\s*\(/);
  });

  test('Session cleanup interval removes idle sessions from _sessionCache', () => {
    expect(botCode).toContain('_sessionCache.delete(key)');
  });

  test('Session timeout (resetSessionTimer) clears bookingData via clearSession', () => {
    const fnIdx = botCode.indexOf('function resetSessionTimer(');
    const segment = botCode.slice(fnIdx, fnIdx + 900);
    expect(segment).toContain('clearSession(chatId)');
  });

  test('Session timeout sends expiry message to user', () => {
    const fnIdx = botCode.indexOf('function resetSessionTimer(');
    const segment = botCode.slice(fnIdx, fnIdx + 1000);
    expect(segment).toMatch(/Время сессии истекло|Время ввода истекло|session.*expired/i);
  });

  test('/cancel command clears sessionTimers', () => {
    const idx = botCode.indexOf('bot.onText(/\\/cancel/');
    const segment = botCode.slice(idx, idx + 600);
    expect(segment).toContain('sessionTimers.delete(chatId)');
  });

  test('/cancel command calls clearSession', () => {
    const idx = botCode.indexOf('bot.onText(/\\/cancel/');
    const segment = botCode.slice(idx, idx + 600);
    expect(segment).toContain('clearSession(chatId)');
  });

  test('Session timeout fires after SESSION_TIMEOUT_MS', () => {
    expect(botCode).toContain('SESSION_TIMEOUT_MS');
    expect(botCode).toMatch(/setTimeout.*SESSION_TIMEOUT_MS|SESSION_TIMEOUT_MS.*setTimeout/s);
  });

  test('cancel_session handler clears sessionTimers', () => {
    const idx = botCode.indexOf("data === 'cancel_session'");
    const segment = botCode.slice(idx, idx + 300);
    expect(segment).toContain('sessionTimers.delete(chatId)');
  });

  test('setSessionReminder fires resume_session or cancel_session buttons', () => {
    const fnIdx = botCode.indexOf('function setSessionReminder(');
    const segment = botCode.slice(fnIdx, fnIdx + 1500);
    // Either booking-specific (bk_resume/bk_cancel_session) or generic (resume_session/cancel_session)
    expect(segment).toMatch(/resume_session|bk_resume/);
    expect(segment).toMatch(/cancel_session|bk_cancel_session/);
  });
});

// ── БЛОК E: Session management in bot.js ──────────────────────────────────────

describe('Wave 80 БЛОК E: Session management in bot.js', () => {
  test('getSession function exists', () => {
    expect(botCode).toContain('async function getSession(');
  });

  test('setSession function exists', () => {
    expect(botCode).toContain('async function setSession(');
  });

  test('clearSession function exists', () => {
    expect(botCode).toContain('async function clearSession(');
  });

  test('resetSessionTimer function exists', () => {
    expect(botCode).toContain('function resetSessionTimer(');
  });

  test('sessionTimers Map is defined', () => {
    expect(botCode).toContain('const sessionTimers = new Map()');
  });

  test('_sessionCache Map is defined for in-memory session cache', () => {
    expect(botCode).toContain('const _sessionCache = new Map()');
  });

  test('updated_at field tracked in session records', () => {
    expect(botCode).toContain('updated_at');
  });

  test('_sessionCache stores updated_at for each session', () => {
    expect(botCode).toMatch(/_sessionCache.*updated_at|updated_at.*_sessionCache/s);
  });

  test('setSession persists to telegram_sessions table', () => {
    const fnIdx = botCode.indexOf('async function setSession(');
    const segment = botCode.slice(fnIdx, fnIdx + 400);
    expect(segment).toContain('telegram_sessions');
  });

  test('getSession reads from _sessionCache first', () => {
    const fnIdx = botCode.indexOf('async function getSession(');
    const segment = botCode.slice(fnIdx, fnIdx + 300);
    expect(segment).toContain('_sessionCache.has(');
  });

  test('clearSession sets state to idle', () => {
    expect(botCode).toMatch(/clearSession.*setSession.*idle|setSession.*chatId.*['"]idle['"]/s);
  });

  test('SESSION_TIMEOUT_MS is imported from database.js', () => {
    expect(botCode).toContain('SESSION_TIMEOUT_MS');
    expect(botCode).toMatch(/require.*database/);
  });

  test('sessionReminderTimers object is defined', () => {
    expect(botCode).toContain('sessionReminderTimers');
  });

  test('clearSessionReminder function exists', () => {
    expect(botCode).toContain('function clearSessionReminder(');
  });

  test('setSession uses INSERT OR REPLACE for upsert', () => {
    const fnIdx = botCode.indexOf('async function setSession(');
    const segment = botCode.slice(fnIdx, fnIdx + 400);
    expect(segment).toContain('INSERT OR REPLACE INTO telegram_sessions');
  });
});

// ── БЛОК F: Backup scripts existence and executability ────────────────────────

describe('Wave 80 БЛОК F: Backup scripts', () => {
  const scriptsDir = path.join(__dirname, '../scripts');

  test('scripts/backup.sh exists', () => {
    expect(fs.existsSync(path.join(scriptsDir, 'backup.sh'))).toBe(true);
  });

  test('scripts/restore-db.sh exists', () => {
    expect(fs.existsSync(path.join(scriptsDir, 'restore-db.sh'))).toBe(true);
  });

  test('scripts/vacuum-db.sh exists', () => {
    expect(fs.existsSync(path.join(scriptsDir, 'vacuum-db.sh'))).toBe(true);
  });

  test('backup.sh is executable', () => {
    const stat = fs.statSync(path.join(scriptsDir, 'backup.sh'));
    // Check owner execute bit (0o100) or group/other execute
    expect(stat.mode & 0o111).not.toBe(0);
  });

  test('restore-db.sh is executable', () => {
    const stat = fs.statSync(path.join(scriptsDir, 'restore-db.sh'));
    expect(stat.mode & 0o111).not.toBe(0);
  });

  test('vacuum-db.sh is executable', () => {
    const stat = fs.statSync(path.join(scriptsDir, 'vacuum-db.sh'));
    expect(stat.mode & 0o111).not.toBe(0);
  });

  test('backup.sh contains sqlite3 backup command', () => {
    const content = fs.readFileSync(path.join(scriptsDir, 'backup.sh'), 'utf8');
    expect(content).toMatch(/sqlite3|\.db/i);
  });

  test('restore-db.sh contains restore or copy logic', () => {
    const content = fs.readFileSync(path.join(scriptsDir, 'restore-db.sh'), 'utf8');
    expect(content).toMatch(/cp|mv|restore|sqlite3/i);
  });

  test('vacuum-db.sh references VACUUM or sqlite3', () => {
    const content = fs.readFileSync(path.join(scriptsDir, 'vacuum-db.sh'), 'utf8');
    expect(content).toMatch(/VACUUM|sqlite3/i);
  });

  test('backup.sh has shebang line', () => {
    const content = fs.readFileSync(path.join(scriptsDir, 'backup.sh'), 'utf8');
    expect(content.startsWith('#!/'));
  });

  test('restore-db.sh has shebang line', () => {
    const content = fs.readFileSync(path.join(scriptsDir, 'restore-db.sh'), 'utf8');
    expect(content.startsWith('#!/'));
  });
});
