'use strict';
// Unit tests for bot.js helper functions (esc, ru_plural, etc.)
// We extract/re-implement the logic to test it in isolation.

// ─── esc() — MarkdownV2 escape ────────────────────────────────────────────────
// Same regex as in bot.js
function esc(text) {
  if (!text) return '';
  return String(text).replace(/[_*[\]()~`>#+=|{}.!\\-]/g, c => `\\${c}`);
}

// ─── ru_plural() — Russian pluralization ─────────────────────────────────────
// Same logic as in bot.js
function ru_plural(n, f1, f2, f5) {
  const abs = Math.abs(n) % 100;
  const n1 = abs % 10;
  if (abs > 10 && abs < 20) return f5;
  if (n1 > 1 && n1 < 5) return f2;
  if (n1 === 1) return f1;
  return f5;
}

describe('esc() — MarkdownV2 escape function', () => {
  test('escapes underscores', () => {
    expect(esc('hello_world')).toBe('hello\\_world');
  });

  test('escapes asterisks', () => {
    expect(esc('bold*text')).toBe('bold\\*text');
  });

  test('escapes square brackets', () => {
    expect(esc('[link]')).toBe('\\[link\\]');
  });

  test('escapes parentheses', () => {
    expect(esc('(test)')).toBe('\\(test\\)');
  });

  test('escapes dots', () => {
    expect(esc('3.14')).toBe('3\\.14');
  });

  test('escapes exclamation marks', () => {
    expect(esc('Hello!')).toBe('Hello\\!');
  });

  test('handles empty string', () => {
    expect(esc('')).toBe('');
  });

  test('handles null gracefully', () => {
    expect(esc(null)).toBe('');
  });

  test('handles undefined gracefully', () => {
    expect(esc(undefined)).toBe('');
  });

  test('handles numbers (coerces to string)', () => {
    expect(esc(42)).toBe('42');
  });

  test('escapes dash', () => {
    expect(esc('a-b')).toBe('a\\-b');
  });

  test('escapes backslash', () => {
    expect(esc('a\\b')).toBe('a\\\\b');
  });

  test('leaves non-special ASCII chars untouched', () => {
    expect(esc('hello world')).toBe('hello world');
  });

  test('leaves Cyrillic text untouched', () => {
    expect(esc('Привет мир')).toBe('Привет мир');
  });

  test('escapes tilde', () => {
    expect(esc('a~b')).toBe('a\\~b');
  });

  test('escapes hash', () => {
    expect(esc('a#b')).toBe('a\\#b');
  });

  test('escapes pipe', () => {
    expect(esc('a|b')).toBe('a\\|b');
  });

  test('escapes greater-than', () => {
    expect(esc('a>b')).toBe('a\\>b');
  });

  test('escapes plus', () => {
    expect(esc('a+b')).toBe('a\\+b');
  });

  test('escapes equals', () => {
    expect(esc('a=b')).toBe('a\\=b');
  });

  test('escapes backtick', () => {
    expect(esc('a`b')).toBe('a\\`b');
  });

  test('escapes curly braces', () => {
    expect(esc('a{b}c')).toBe('a\\{b\\}c');
  });

  test('plain alphanumerics pass through unchanged', () => {
    expect(esc('ABC123xyz')).toBe('ABC123xyz');
  });
});

describe('ru_plural() — Russian pluralization', () => {
  test('1 → singular form (заявка)', () => {
    expect(ru_plural(1, 'заявка', 'заявки', 'заявок')).toBe('заявка');
  });

  test('2 → genitive singular (заявки)', () => {
    expect(ru_plural(2, 'заявка', 'заявки', 'заявок')).toBe('заявки');
  });

  test('3 → genitive singular (заявки)', () => {
    expect(ru_plural(3, 'заявка', 'заявки', 'заявок')).toBe('заявки');
  });

  test('4 → genitive singular (заявки)', () => {
    expect(ru_plural(4, 'заявка', 'заявки', 'заявок')).toBe('заявки');
  });

  test('5 → genitive plural (заявок)', () => {
    expect(ru_plural(5, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('11 → genitive plural (exception, not singular)', () => {
    expect(ru_plural(11, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('12 → genitive plural (exception)', () => {
    expect(ru_plural(12, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('14 → genitive plural (exception)', () => {
    expect(ru_plural(14, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('21 → singular again', () => {
    expect(ru_plural(21, 'заявка', 'заявки', 'заявок')).toBe('заявка');
  });

  test('22 → genitive singular', () => {
    expect(ru_plural(22, 'заявка', 'заявки', 'заявок')).toBe('заявки');
  });

  test('100 → genitive plural', () => {
    expect(ru_plural(100, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('0 → genitive plural', () => {
    expect(ru_plural(0, 'заявка', 'заявки', 'заявок')).toBe('заявок');
  });

  test('works with other word sets — "день/дня/дней"', () => {
    expect(ru_plural(1, 'день', 'дня', 'дней')).toBe('день');
    expect(ru_plural(3, 'день', 'дня', 'дней')).toBe('дня');
    expect(ru_plural(7, 'день', 'дня', 'дней')).toBe('дней');
  });
});
