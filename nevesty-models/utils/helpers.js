'use strict';

/**
 * Escape special characters for Telegram MarkdownV2 parse mode.
 */
function esc(s) {
  return String(s).replace(/[_*[\]()~`>#+=|{}.!\\\-]/g, '\\$&');
}

/**
 * Russian plural form: ru_plural(1, 'заявка', 'заявки', 'заявок')
 */
function ru_plural(n, one, few, many) {
  const abs = Math.abs(n);
  const mod10 = abs % 10;
  const mod100 = abs % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}

/**
 * Format phone number to Russian format: +7 (999) 000-00-00
 */
function formatPhone(phone) {
  const digits = String(phone || '').replace(/\D/g, '');
  if (digits.length === 11) {
    return `+${digits[0]} (${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7, 9)}-${digits.slice(9, 11)}`;
  }
  return phone;
}

/**
 * Normalize phone: keep digits only, replace leading 8 with 7
 */
function normalizePhone(phone) {
  const digits = String(phone || '').replace(/\D/g, '');
  if (digits.startsWith('8') && digits.length === 11) return '7' + digits.slice(1);
  return digits;
}

/**
 * Format currency: 15000 → '15 000 ₽'
 */
function formatCurrency(amount) {
  if (!amount) return '—';
  return Number(amount).toLocaleString('ru-RU') + ' ₽';
}

/**
 * Truncate text to maxLen, adding ellipsis
 */
function truncate(text, maxLen = 100) {
  if (!text) return '';
  const s = String(text);
  return s.length <= maxLen ? s : s.slice(0, maxLen - 1) + '…';
}

module.exports = { esc, ru_plural, formatPhone, normalizePhone, formatCurrency, truncate };
