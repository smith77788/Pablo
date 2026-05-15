'use strict';
const { esc, ru_plural, formatPhone, normalizePhone, formatCurrency, truncate } = require('../utils/helpers');

describe('esc()', () => {
  test('escapes underscore', () => expect(esc('hello_world')).toBe('hello\\_world'));
  test('escapes asterisk', () => expect(esc('**bold**')).toBe('\\*\\*bold\\*\\*'));
  test('escapes dot', () => expect(esc('example.com')).toBe('example\\.com'));
  test('escapes parentheses', () => expect(esc('(text)')).toBe('\\(text\\)'));
  test('escapes hyphen', () => expect(esc('a-b')).toBe('a\\-b'));
  test('does not double-escape', () => expect(esc('plain text')).toBe('plain text'));
  test('handles numbers', () => expect(esc(42)).toBe('42'));
  test('handles null/undefined gracefully', () => {
    expect(esc(null)).toBe('null');
    expect(esc(undefined)).toBe('undefined');
  });
});

describe('ru_plural()', () => {
  test('1 заявка', () => expect(ru_plural(1, 'заявка', 'заявки', 'заявок')).toBe('заявка'));
  test('2 заявки', () => expect(ru_plural(2, 'заявка', 'заявки', 'заявок')).toBe('заявки'));
  test('5 заявок', () => expect(ru_plural(5, 'заявка', 'заявки', 'заявок')).toBe('заявок'));
  test('11 заявок (exception)', () => expect(ru_plural(11, 'заявка', 'заявки', 'заявок')).toBe('заявок'));
  test('21 заявка', () => expect(ru_plural(21, 'заявка', 'заявки', 'заявок')).toBe('заявка'));
  test('100 заявок', () => expect(ru_plural(100, 'заявка', 'заявки', 'заявок')).toBe('заявок'));
});

describe('formatPhone()', () => {
  test('formats 11-digit Russian number', () =>
    expect(formatPhone('79991234567')).toBe('+7 (999) 123-45-67'));
  test('handles already-formatted input (digits extracted and re-formatted)', () =>
    expect(formatPhone('+7 999 123-45-67')).toBe('+7 (999) 123-45-67'));
});

describe('normalizePhone()', () => {
  test('normalizes 8-prefixed number', () =>
    expect(normalizePhone('89991234567')).toBe('79991234567'));
  test('strips non-digits', () =>
    expect(normalizePhone('+7 (999) 123-45-67')).toBe('79991234567'));
});

describe('formatCurrency()', () => {
  test('formats 15000 as 15 000 ₽', () =>
    expect(formatCurrency(15000)).toMatch('15'));
  test('handles zero/null', () => expect(formatCurrency(0)).toBe('—'));
  test('handles null', () => expect(formatCurrency(null)).toBe('—'));
});

describe('truncate()', () => {
  test('does not truncate short string', () =>
    expect(truncate('hello', 10)).toBe('hello'));
  test('truncates long string', () => {
    const result = truncate('abcdefghij', 5);
    expect(result.length).toBe(5);
    expect(result).toMatch('…');
  });
  test('handles empty string', () => expect(truncate('')).toBe(''));
});
