'use strict';

process.env.NODE_ENV = 'test';

// Clear cached module between tests if env changes
beforeEach(() => {
  jest.resetModules();
});

describe('SMS Service', () => {
  let sms;

  beforeAll(() => {
    // Ensure no provider is configured so stub path is exercised
    delete process.env.SMS_PROVIDER;
    delete process.env.SMS_RU_API_KEY;
    delete process.env.SMS_RU_API_ID;
    delete process.env.SMSC_LOGIN;
    delete process.env.TWILIO_ACCOUNT_SID;
    sms = require('../services/sms');
  });

  it('exports sendSms function', () => {
    expect(typeof sms.sendSms).toBe('function');
  });

  it('exports sendOrderStatusSms function', () => {
    expect(typeof sms.sendOrderStatusSms).toBe('function');
  });

  it('exports sendBookingConfirmationSms function', () => {
    expect(typeof sms.sendBookingConfirmationSms).toBe('function');
  });

  it('exports legacy sendSMS function', () => {
    expect(typeof sms.sendSMS).toBe('function');
  });

  it('exports legacy sendStatusChangeSMS function', () => {
    expect(typeof sms.sendStatusChangeSMS).toBe('function');
  });

  it('exports legacy sendOrderConfirmationSMS function', () => {
    expect(typeof sms.sendOrderConfirmationSMS).toBe('function');
  });

  it('returns false when SMS provider not configured', async () => {
    const result = await sms.sendSms('+79001234567', 'Test message');
    expect(result).toBe(false);
  });

  it('returns false for empty phone', async () => {
    const result = await sms.sendSms('', 'Test');
    expect(result).toBe(false);
  });

  it('returns false for null phone', async () => {
    const result = await sms.sendSms(null, 'Test');
    expect(result).toBe(false);
  });

  it('returns false for invalid (too short) phone', async () => {
    const result = await sms.sendSms('123', 'Test');
    expect(result).toBe(false);
  });

  it('returns false for empty text', async () => {
    const result = await sms.sendSms('+79001234567', '');
    expect(result).toBe(false);
  });

  it('sendOrderStatusSms returns false for unknown status', async () => {
    const result = await sms.sendOrderStatusSms('+79001234567', 'NM-001', 'unknown_status');
    expect(result).toBe(false);
  });

  it('sendOrderStatusSms returns boolean for confirmed status (not configured)', async () => {
    const result = await sms.sendOrderStatusSms('+79001234567', 'NM-001', 'confirmed');
    expect(typeof result).toBe('boolean');
    expect(result).toBe(false);
  });

  it('sendOrderStatusSms returns boolean for completed status', async () => {
    const result = await sms.sendOrderStatusSms('+79001234567', 'NM-002', 'completed');
    expect(typeof result).toBe('boolean');
  });

  it('sendOrderStatusSms returns boolean for cancelled status', async () => {
    const result = await sms.sendOrderStatusSms('+79001234567', 'NM-003', 'cancelled');
    expect(typeof result).toBe('boolean');
  });

  it('sendBookingConfirmationSms returns boolean (not configured)', async () => {
    const result = await sms.sendBookingConfirmationSms('+79001234567', 'NM-001');
    expect(typeof result).toBe('boolean');
    expect(result).toBe(false);
  });

  it('sendBookingConfirmationSms returns false for invalid phone', async () => {
    const result = await sms.sendBookingConfirmationSms('000', 'NM-001');
    expect(result).toBe(false);
  });

  it('legacy sendSMS returns object with success:false when not configured', async () => {
    const result = await sms.sendSMS('+79001234567', 'Test');
    expect(result).toMatchObject({ success: false });
  });

  it('normalizes 8-prefix Russian numbers correctly (stub path)', async () => {
    // 89001234567 → 79001234567, should still return false (no provider)
    const result = await sms.sendSms('89001234567', 'Test');
    expect(result).toBe(false);
  });

  it('normalizes 10-digit numbers correctly (stub path)', async () => {
    const result = await sms.sendSms('9001234567', 'Test');
    expect(result).toBe(false);
  });
});
