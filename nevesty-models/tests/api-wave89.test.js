'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');

describe('T1: Booking form localStorage autosave', () => {
  let bookingHtml;
  beforeAll(() => {
    bookingHtml = fs.readFileSync(path.join(ROOT, 'public/booking.html'), 'utf8');
  });

  test('T01: booking.html has localStorage storage key constant', () => {
    expect(bookingHtml).toMatch(/nm_booking|BOOKING_STORAGE|booking_progress/i);
  });
  test('T02: booking.html saves progress to localStorage', () => {
    expect(bookingHtml).toMatch(/localStorage\.setItem/);
  });
  test('T03: booking.html loads saved progress on page load', () => {
    expect(bookingHtml).toMatch(/localStorage\.getItem/);
  });
  test('T04: booking.html clears localStorage on successful submission', () => {
    expect(bookingHtml).toMatch(/localStorage\.removeItem/);
  });
  test('T05: booking form has resume banner or restore functionality', () => {
    expect(bookingHtml).toMatch(/resume|restore|Продолжить|незавершённ/i);
  });
});
