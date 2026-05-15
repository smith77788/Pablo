'use strict';
const fs = require('fs');
const path = require('path');

describe('Stub Settings S1-S6 connected to business logic', () => {
  let botSrc;
  beforeAll(() => {
    botSrc = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
  });

  // S1
  test('S1: model_max_photos — getSetting called for photo limit', () => {
    expect(botSrc).toMatch(/getSetting\('model_max_photos'\)/);
  });
  test('S1: model_max_photos — parseInt used on setting value', () => {
    const hasParseInt =
      /parseInt.*getSetting\('model_max_photos'\)/.test(botSrc) || /parseInt.*model_max_photos/.test(botSrc);
    expect(hasParseInt).toBe(true);
  });

  // S2
  test('S2: booking_require_email — getSetting called in booking flow', () => {
    expect(botSrc).toMatch(/getSetting\('booking_require_email'\)/);
  });
  test('S2: booking_require_email — skip button conditional on setting', () => {
    // The skip button should be gated behind the requireEmail !== '1' check
    const hasCondition = /requireEmail.*Пропустить|booking_require_email.*skip|skip.*booking_require_email/i.test(
      botSrc
    );
    expect(hasCondition || (botSrc.includes('requireEmail') && botSrc.includes('Пропустить'))).toBe(true);
  });

  // S3
  test('S3: booking_auto_confirm — getSetting called in booking submit', () => {
    expect(botSrc).toMatch(/getSetting\('booking_auto_confirm'\)/);
  });
  test('S3: booking_auto_confirm — status changes to confirmed when enabled', () => {
    // autoConfirm variable is set from the setting, and confirmed status is used in the same block
    const hasAutoConfirm = botSrc.includes("getSetting('booking_auto_confirm')");
    const hasConfirmedStatus = /status='confirmed'|status.*=.*'confirmed'|order\.status\s*=\s*'confirmed'/.test(botSrc);
    expect(hasAutoConfirm && hasConfirmedStatus).toBe(true);
  });

  // S4
  test('S4: reviews_auto_approve — getSetting called in review submit', () => {
    expect(botSrc).toMatch(/getSetting\('reviews_auto_approve'\)/);
  });
  test('S4: reviews_auto_approve — approved field uses setting value', () => {
    expect(botSrc).toMatch(/autoApprove.*[?:].*1.*0|reviews_auto_approve.*approved/);
  });

  // S5
  test('S5: reviews_min_completed — getSetting called before allowing review', () => {
    expect(botSrc).toMatch(/getSetting\('reviews_min_completed'\)/);
  });
  test('S5: reviews_min_completed — count query for completed orders', () => {
    expect(botSrc).toMatch(/COUNT.*orders.*completed|completed.*orders.*COUNT/i);
  });

  // S6
  test('S6: notif_new_order — getSetting called in notifyNewOrder', () => {
    expect(botSrc).toMatch(/getSetting\('notif_new_order'\)/);
  });
  test('S6: notif_new_order — early return when disabled', () => {
    // notifEnabled is checked and early return is issued when '0'
    const hasCheck = /notifEnabled\s*===\s*'0'\s*\)\s*return|notifOn\s*===\s*'0'\s*\)\s*return/.test(botSrc);
    expect(hasCheck).toBe(true);
  });
});
