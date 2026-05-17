'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');

// ─── T1: Back navigation callbacks in booking flow ────────────────────────────

describe('T1: Back navigation callbacks in booking flow', () => {
  test('T01: bk_back_event_type handler exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]bk_back_event_type['"]/);
  });

  test('T02: bk_back_event_type handler calls bkStep2EventType', () => {
    // Find the HANDLER (data === 'bk_back_event_type'), not the button definition
    const idx = botCode.indexOf("data === 'bk_back_event_type'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/bkStep2EventType/);
  });

  test('T03: bk_back_duration handler exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]bk_back_duration['"]/);
  });

  test('T04: bk_back_location handler exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]bk_back_location['"]/);
  });

  test('T05: bk_back_budget handler exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]bk_back_budget['"]/);
  });

  test('T06: bkStep2EventType has back button to bk_start', () => {
    const idx = botCode.indexOf('async function bkStep2EventType');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 600);
    expect(nearby).toMatch(/bk_start/);
  });

  test('T07: bkStep2Duration has back button', () => {
    const idx = botCode.indexOf('async function bkStep2Duration');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 1000);
    expect(nearby).toMatch(/bk_back_/);
  });

  test('T08: bkStep2Budget has back button', () => {
    const idx = botCode.indexOf('async function bkStep2Budget');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 600);
    expect(nearby).toMatch(/bk_back_/);
  });
});

// ─── T2: Booking cancel confirmation ─────────────────────────────────────────

describe('T2: Booking cancel confirmation dialog', () => {
  test('T09: bk_cancel shows confirmation dialog (not immediate cancel)', () => {
    const idx = botCode.indexOf("data === 'bk_cancel'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 500);
    expect(nearby).toMatch(/Да, отменить|bk_cancel_confirm/);
    expect(nearby).not.toMatch(/clearSession|clearSession/);
  });

  test('T10: bk_cancel_confirm handler clears session', () => {
    const idx = botCode.indexOf("data === 'bk_cancel_confirm'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/clearSession/);
  });

  test('T11: bk_cancel dialog has resume option (not dead-end)', () => {
    const idx = botCode.indexOf("data === 'bk_cancel'");
    const nearby = botCode.slice(idx, idx + 500);
    expect(nearby).toMatch(/bk_resume|Продолжить/);
  });

  test('T12: bk_cancel_confirm clears session warning timer', () => {
    const idx = botCode.indexOf("data === 'bk_cancel_confirm'");
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/clearSessionWarning|sessionWarningTimers/);
  });
});

// ─── T3: Session timeout warning ─────────────────────────────────────────────

describe('T3: Session timeout warning before expiry', () => {
  test('T13: sessionWarningTimers Map exists', () => {
    expect(botCode).toMatch(/sessionWarningTimers\s*=\s*new Map/);
  });

  test('T14: setSessionWarning function exists', () => {
    expect(botCode).toMatch(/function\s+setSessionWarning/);
  });

  test('T15: clearSessionWarning function exists', () => {
    expect(botCode).toMatch(/function\s+clearSessionWarning/);
  });

  test('T16: session_keepalive callback resets timer', () => {
    expect(botCode).toMatch(/session_keepalive/);
    const idx = botCode.indexOf("'session_keepalive'");
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/resetSessionTimer/);
  });

  test('T17: warning message text mentions 2 minutes', () => {
    expect(botCode).toMatch(/2\s*минут|через\s*2|2.*минут/);
  });

  test('T18: resetSessionTimer calls setSessionWarning', () => {
    const idx = botCode.indexOf('function resetSessionTimer');
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/setSessionWarning|clearSessionWarning/);
  });
});

// ─── T4: Social settings escaping ────────────────────────────────────────────

describe('T4: Social settings MarkdownV2 safety', () => {
  test('T19: social settings section uses parse_mode MarkdownV2', () => {
    const idx = botCode.indexOf("section === 'social'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 3000);
    expect(nearby).toMatch(/parse_mode/);
    expect(nearby).toMatch(/MarkdownV2/);
  });

  test('T20: Instagram handle in social section is wrapped with esc()', () => {
    const idx = botCode.indexOf("section === 'social'");
    const nearby = botCode.slice(idx, idx + 1600);
    expect(nearby).toMatch(/esc\s*\(\s*insta/);
  });
});

// ─── T5: Date format validation ───────────────────────────────────────────────

describe('T5: Date format validation in booking', () => {
  test('T21: date validation checks format before model busy date check', () => {
    const idx = botCode.indexOf("case 'bk_s2_date'");
    expect(idx).toBeGreaterThan(-1);
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/dmyFmt|Неверный формат|ДД\.ММ\.ГГГГ/);
  });

  test('T22: invalid month (>12) is rejected', () => {
    const idx = botCode.indexOf("case 'bk_s2_date'");
    const nearby = botCode.slice(idx, idx + 700);
    expect(nearby).toMatch(/mv.*12|12.*mv/i);
  });
});

// ─── T6: bk_skip_email security ──────────────────────────────────────────────

describe('T6: Email skip security enforcement', () => {
  test('T23: bk_skip_email checks booking_require_email before proceeding', () => {
    const idx = botCode.indexOf("data === 'bk_skip_email'");
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/booking_require_email/);
  });

  test('T24: bk_skip_email returns early with toast when email required', () => {
    const idx = botCode.indexOf("data === 'bk_skip_email'");
    const nearby = botCode.slice(idx, idx + 400);
    expect(nearby).toMatch(/answerCallbackQuery/);
  });

  test('T25: bk_s3_email error shows conditional skip button', () => {
    const idx = botCode.indexOf("case 'bk_s3_email'");
    const nearby = botCode.slice(idx, idx + 600);
    expect(nearby).toMatch(/requireEmailVal/);
    expect(nearby).toMatch(/bk_skip_email/);
  });
});

// ─── T7: Auto-confirm manager notification ────────────────────────────────────

describe('T7: Auto-confirm always notifies manager', () => {
  test('T26: auto-confirm notifyAdmin call exists after status change', () => {
    const idx = botCode.indexOf('booking_auto_confirm');
    const nearby = botCode.slice(idx, idx + 1000);
    expect(nearby).toMatch(/notifyAdmin[\s\S]{0,50}Автоподтверждение/s);
  });

  test('T27: auto-confirm uses booking_confirm_msg if set', () => {
    expect(botCode).toMatch(/customConfirmMsg|booking_confirm_msg/);
    const idx = botCode.indexOf('customConfirmMsg');
    expect(idx).toBeGreaterThan(-1);
  });
});

// ─── T8: Race condition post-insert check ────────────────────────────────────

describe('T8: Race condition prevention in order submission', () => {
  test('T28: bkSubmit has post-insert verification for max orders', () => {
    const bkSubmitIdx = botCode.indexOf('async function bkSubmit');
    expect(bkSubmitIdx).toBeGreaterThan(-1);
    const funcBody = botCode.slice(bkSubmitIdx, bkSubmitIdx + 3000);
    // Must have a count check after INSERT
    expect(funcBody).toMatch(/activeAfterInsert|COUNT\(\*\).*active|n.*maxActive/i);
  });

  test('T29: over-limit orders are deleted after race condition detected', () => {
    const bkSubmitIdx = botCode.indexOf('async function bkSubmit');
    const funcBody = botCode.slice(bkSubmitIdx, bkSubmitIdx + 3000);
    expect(funcBody).toMatch(/DELETE FROM orders WHERE order_number/i);
  });
});
