'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');
const apiCode = fs.readFileSync(path.join(ROOT, 'routes', 'api.js'), 'utf8');

// ─── T1: FAQ feedback callbacks (bot.js) ─────────────────────────────────────

describe('T1: FAQ feedback callbacks (bot.js)', () => {
  test('T01: faq_helpful_ callback handler exists and sends feedback confirmation', () => {
    // The handler for faq_helpful_ must check data.startsWith('faq_helpful_')
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]faq_helpful_['"]\s*\)/);
    // Confirmation sent via answerCallbackQuery (toast) or safeSend (message)
    const hasAnswerCQ = /faq_helpful_[\s\S]{0,300}answerCallbackQuery/.test(botCode);
    const hasSafeSend = /faq_helpful_[\s\S]{0,300}safeSend/.test(botCode);
    expect(hasAnswerCQ || hasSafeSend).toBe(true);
  });

  test('T02: faq_helpful_ answerCallbackQuery includes a thanks text', () => {
    // The positive feedback path sends a toast: 'Спасибо за отзыв!'
    expect(botCode).toMatch(/faq_helpful_[\s\S]{0,200}Спасибо за отзыв/);
  });

  test('T03: faq_nothelpful_ callback handler exists', () => {
    expect(botCode).toMatch(/data\.startsWith\s*\(\s*['"]faq_nothelpful_['"]\s*\)/);
  });

  test('T04: faq_nothelpful_ offers to contact a manager (msg_manager_start callback)', () => {
    // After negative feedback the bot proposes the user to write to a manager
    const block = botCode.match(/faq_nothelpful_[\s\S]{0,500}msg_manager_start/);
    expect(block).not.toBeNull();
  });

  test('T05: faq_nothelpful_ shows a "back to FAQ" button (faq callback)', () => {
    // The reply keyboard must contain a ← All questions link back to FAQ
    const block = botCode.match(/faq_nothelpful_[\s\S]{0,600}callback_data\s*:\s*['"]faq['"]/);
    expect(block).not.toBeNull();
  });

  test('T06: FAQ item view adds both helpful / nothelpful buttons', () => {
    // When showing a single faq_ item the bot builds both feedback buttons
    expect(botCode).toMatch(/callback_data\s*:\s*`faq_helpful_\${/);
    expect(botCode).toMatch(/callback_data\s*:\s*`faq_nothelpful_\${/);
  });
});

// ─── T2: Price calculator sharing (bot.js) ───────────────────────────────────

describe('T2: Price calculator sharing (bot.js)', () => {
  test('T07: calc_share_ regex parses N, H, TYPE groups', () => {
    // The regex ^calc_share_(\d+)_(\d+)_(.+)$ must be present in bot.js
    expect(botCode).toMatch(/calc_share_\(\\d\+\)_\(\\d\+\)_\(\.\+\)/);
  });

  test('T08: calc_share handler reads calcModels and calcHours from regex groups', () => {
    // After parsing, both calcModels and calcHours must be derived from parseInt
    const block = botCode.match(/csm[\s\S]{0,600}calcModels[\s\S]{0,200}calcHours/);
    expect(block).not.toBeNull();
  });

  test('T09: calc_share builds a shareText that mentions "Расчёт стоимости"', () => {
    // The shareable message header must contain this phrase
    expect(botCode).toMatch(/shareText[\s\S]{0,100}Расчёт стоимости/);
  });

  test('T10: calc_share shareText includes min and max price', () => {
    // The text contains minPrice and maxPrice interpolations
    const block = botCode.match(/shareText\s*=[\s\S]{0,600}minPrice[\s\S]{0,200}maxPrice/);
    expect(block).not.toBeNull();
  });

  test('T11: calc_share reply has "Оформить заявку" button pointing to calc_book_', () => {
    // After sharing, bot shows a booking button back to calc_book_
    const block = botCode.match(/shareText[\s\S]{0,800}calc_book_/);
    expect(block).not.toBeNull();
  });

  test('T12: calc_share "Share" button is built with N_H_TYPE format', () => {
    // The button that triggers sharing uses the template literal `calc_share_${...}_${...}_${...}`
    expect(botCode).toMatch(/calc_share_\$\{[^}]+\}_\$\{[^}]+\}_\$\{[^}]+\}/);
  });
});

// ─── T3: WhatsApp order notifications (routes/api.js) ────────────────────────

describe('T3: WhatsApp order notifications (routes/api.js)', () => {
  test('T13: POST /orders handler requires whatsapp service', () => {
    // The handler for POST /orders must require('../services/whatsapp')
    expect(apiCode).toMatch(/require\s*\(\s*['"]\.\.\/services\/whatsapp['"]\s*\)/);
  });

  test('T14: WhatsApp sendText is called inside POST /orders', () => {
    // sendText must be called in the orders route
    expect(apiCode).toMatch(/whatsapp\.sendText\s*\(/);
  });

  test('T15: WhatsApp phone is sanitized to digits only via replace(/\\D/g)', () => {
    // client_phone must be stripped of non-digits before passing to sendText
    expect(apiCode).toMatch(/client_phone\.replace\s*\(\s*\/\\D\/g\s*,\s*['"]{2}\s*\)/);
  });

  test('T16: WhatsApp message references the order_number', () => {
    // The message text must embed the order_number variable
    const block = apiCode.match(/whatsapp\.sendText[\s\S]{0,300}order_number/);
    expect(block).not.toBeNull();
  });

  test('T17: WhatsApp sendText call is non-blocking (uses .catch)', () => {
    // Fire-and-forget pattern: call is followed by .catch(...)
    expect(apiCode).toMatch(/whatsapp\.sendText\([^)]+\)\.catch\s*\(/);
  });
});

// ─── T4: contacts_photo_url setting in bot.js ────────────────────────────────

describe('T4: contacts_photo_url setting (bot.js)', () => {
  test('T18: showContactManager reads contacts_photo_url via getSetting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]contacts_photo_url['"]\s*\)/);
  });

  test('T19: showContactManager calls bot.sendPhoto when contactPhoto is set', () => {
    // Both contacts_photo_url and bot.sendPhoto must appear in showContactManager
    const fnBlock = botCode.match(/async function showContactManager[\s\S]{0,2000}?bot\.sendPhoto/);
    expect(fnBlock).not.toBeNull();
  });
});
