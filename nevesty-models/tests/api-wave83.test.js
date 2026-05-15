'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const dbCode = fs.readFileSync(path.join(ROOT, 'database.js'), 'utf8');

// ─── T1: model_max_photos — dynamic limit ─────────────────────────────────────

describe('T1: model_max_photos — dynamic limit from settings', () => {
  test('T01: bot.js reads model_max_photos setting (not hardcoded 8)', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]model_max_photos['"]/);
  });

  test('T02: model_max_photos is used to limit photo display', () => {
    const idx = botCode.indexOf("getSetting('model_max_photos')");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(Math.max(0, idx - 200), idx + 500);
    expect(nearby).toMatch(/parseInt|maxPhotos|photosLimit/i);
  });

  test('T03: fallback to 8 when model_max_photos not set', () => {
    expect(botCode).toMatch(/getSetting\(['"]model_max_photos['"]\)[\s\S]{0,80}8/);
  });
});

// ─── T2: booking_require_email — email enforcement ────────────────────────────

describe('T2: booking_require_email — email enforcement', () => {
  test('T04: bk_skip_email handler checks booking_require_email', () => {
    // Handler is at "if (data === 'bk_skip_email')" — find handler not button definition
    const idx = botCode.indexOf("data === 'bk_skip_email'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/booking_require_email/);
  });

  test('T05: bk_skip_email returns early when email is required', () => {
    const idx = botCode.indexOf("data === 'bk_skip_email'");
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/requireEmail|booking_require_email/);
    expect(nearby).toMatch(/return/);
  });

  test('T06: bk_s3_email case conditionally shows skip button based on setting', () => {
    const idx = botCode.indexOf("case 'bk_s3_email'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 600);
    expect(nearby).toMatch(/booking_require_email/);
    expect(nearby).toMatch(/bk_skip_email/);
  });

  test('T07: bk_step3Email function respects booking_require_email to hide skip button', () => {
    expect(botCode).toMatch(
      /booking_require_email[\s\S]{0,300}bk_skip_email|bk_skip_email[\s\S]{0,300}booking_require_email/s
    );
  });
});

// ─── T3: booking_auto_confirm — auto status + manager notification ────────────

describe('T3: booking_auto_confirm — auto confirmation', () => {
  test('T08: bkSubmit reads booking_auto_confirm setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]booking_auto_confirm['"]/);
  });

  test('T09: auto-confirm updates order status to confirmed', () => {
    const idx = botCode.indexOf("getSetting('booking_auto_confirm')");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 500);
    expect(nearby).toMatch(/status.*confirmed|confirmed.*status/i);
  });

  test('T10: auto-confirm notifies manager even when notif_new_order is off', () => {
    const idx = botCode.indexOf("getSetting('booking_auto_confirm')");
    const nearby = botCode.slice(idx, idx + 800);
    expect(nearby).toMatch(/notifyAdmin|Автоподтверждение/);
  });

  test('T11: booking_confirm_msg used in auto-confirm confirmation message', () => {
    const idx = botCode.indexOf("getSetting('booking_auto_confirm')");
    const nearby = botCode.slice(idx - 50, idx + 1000);
    expect(nearby).toMatch(/booking_confirm_msg/);
  });

  test('T12: auto-confirm calls notifyStatusChange for client notification', () => {
    const idx = botCode.indexOf("autoConfirm === '1'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/notifyStatusChange/);
  });
});

// ─── T4: reviews_auto_approve — auto approval ────────────────────────────────

describe('T4: reviews_auto_approve — auto approval of reviews', () => {
  test('T13: bot reads reviews_auto_approve before inserting review', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]reviews_auto_approve['"]/);
  });

  test('T14: approved value set based on reviews_auto_approve setting', () => {
    // approvedVal assigned near the getSetting call (find all occurrences)
    expect(botCode).toMatch(/getSetting\(['"]reviews_auto_approve['"]\)[\s\S]{0,100}approvedVal/s);
  });

  test('T15: INSERT reviews uses dynamic approved value via approvedVal', () => {
    expect(botCode).toMatch(/approvedVal/);
    // approvedVal must appear in INSERT statement context
    const insertIdx = botCode.indexOf('INSERT OR IGNORE INTO reviews');
    expect(insertIdx).toBeGreaterThan(-1);
    const nearbyInsert = botCode.slice(Math.max(0, insertIdx - 300), insertIdx + 400);
    expect(nearbyInsert).toMatch(/approvedVal/i);
  });
});

// ─── T5: feature flags — loyalty, referral, faq ──────────────────────────────

describe('T5: feature flags — loyalty_enabled, referral_enabled, faq_enabled', () => {
  test('T16: buildClientKeyboard reads loyalty_enabled setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]loyalty_enabled['"]/);
  });

  test('T17: buildClientKeyboard reads referral_enabled setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]referral_enabled['"]/);
  });

  test('T18: buildClientKeyboard reads faq_enabled setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]faq_enabled['"]/);
  });

  test('T19: loyalty button is conditionally rendered', () => {
    // Check that loyaltyEnabled is used in a conditional
    expect(botCode).toMatch(/loyaltyEnabled\s*!==\s*['"]0['"]|loyaltyEnabled\s*===\s*['"]1['"]/);
  });

  test('T20: faq button is conditionally rendered', () => {
    // Check that faqEnabled is used in a conditional
    expect(botCode).toMatch(/faqEnabled\s*!==\s*['"]0['"]|faqEnabled\s*===\s*['"]1['"]/);
  });
});

// ─── T6: notif settings — gate checks ────────────────────────────────────────

describe('T6: notification settings gate checks', () => {
  test('T21: notifyNewOrder checks notif_new_order setting', () => {
    const idx = botCode.indexOf('async function notifyNewOrder');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/notif_new_order/);
  });

  test('T22: notif_new_order=0 causes early return from notifyNewOrder', () => {
    const idx = botCode.indexOf('async function notifyNewOrder');
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/=== '0'[\s\S]{0,20}return|notifEnabled[\s\S]{0,50}return/s);
  });

  test('T23: notif_new_review check exists for review notifications', () => {
    expect(botCode).toMatch(/notif_new_review/);
  });

  test('T24: notif_new_message check exists for message forwarding', () => {
    expect(botCode).toMatch(/notif_new_message/);
  });

  test('T25: notifyStatusChange does NOT gate-check notif_status (client always notified)', () => {
    const idx = botCode.indexOf('async function notifyStatusChange');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 500);
    // notif_status should NOT cause early return — client notifications always send
    expect(nearby).not.toMatch(/notif_status[\s\S]{0,100}return\s*;/);
  });
});

// ─── T7: database schema v20 — reviews unique constraint ─────────────────────

describe('T7: database schema v20 — reviews unique constraint', () => {
  test('T26: database.js has schema v20 migration', () => {
    expect(dbCode).toMatch(/VALUES\s*\(\s*20\s*,|version.*20.*UNIQUE|schema_versions[\s\S]{0,100}20/s);
  });

  test('T27: UNIQUE index on reviews(chat_id, order_id) exists in migration', () => {
    expect(dbCode).toMatch(/UNIQUE.*reviews.*chat_id.*order_id|idx_reviews_unique_chat_order/s);
  });

  test('T28: UNIQUE index uses partial WHERE clause for non-null order_id', () => {
    expect(dbCode).toMatch(/idx_reviews_unique_chat_order[\s\S]{0,200}WHERE.*order_id.*IS NOT NULL/s);
  });
});

// ─── T8: date validation in booking ──────────────────────────────────────────

describe('T8: date validation in booking flow', () => {
  test('T29: bk_s2_date validates date format before processing', () => {
    const idx = botCode.indexOf("case 'bk_s2_date'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 600);
    expect(nearby).toMatch(/ДД\.ММ\.ГГГГ|Неверный формат|dmyFmt/i);
  });

  test('T30: bk_s2_date validates month range 1-12', () => {
    const idx = botCode.indexOf("case 'bk_s2_date'");
    const nearby = botCode.slice(idx, idx + 800);
    expect(nearby).toMatch(/mv.*12|12.*mv|month.*12/i);
  });

  test('T31: location input is trimmed', () => {
    const idx = botCode.indexOf("case 'bk_s2_loc'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 200);
    expect(nearby).toMatch(/\.trim\(\)/);
  });
});
