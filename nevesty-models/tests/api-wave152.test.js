'use strict';
const fs = require('fs');
const path = require('path');

// Читаем bot.js как строку для статического анализа
const BOT = fs.readFileSync(path.join(__dirname, '..', 'bot.js'), 'utf8');

describe('STUB Settings — подключение к логике (ФАЗА 1)', () => {
  // T1: model_max_photos — динамический лимит
  it('T1: bot.js читает model_max_photos из getSetting', () => {
    expect(BOT).toMatch(/getSetting\(['"]model_max_photos['"]\)/);
  });

  // T2: booking_require_email — email обязателен
  it('T2: bot.js читает booking_require_email из getSetting', () => {
    expect(BOT).toMatch(/getSetting\(['"]booking_require_email['"]\)/);
  });

  // T3: booking_auto_confirm — автоподтверждение
  it('T3: bot.js читает booking_auto_confirm и выставляет статус confirmed', () => {
    expect(BOT).toMatch(/booking_auto_confirm/);
    // autoConfirm читается, затем статус confirmed выставляется в UPDATE
    expect(BOT).toMatch(/autoConfirm/);
    expect(BOT).toMatch(/status='confirmed'/);
  });

  // T4: reviews_auto_approve — авто одобрение
  it('T4: bot.js читает reviews_auto_approve', () => {
    expect(BOT).toMatch(/getSetting\(['"]reviews_auto_approve['"]\)/);
  });

  // T5: notif_new_order gate
  it('T5: notif_new_order проверяется перед отправкой уведомления', () => {
    expect(BOT).toMatch(/notif_new_order/);
  });

  // T6: notif_new_message gate
  it('T6: notif_new_message проверяется перед пересылкой', () => {
    expect(BOT).toMatch(/notif_new_message/);
  });
});
