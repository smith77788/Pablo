'use strict';
const whatsapp = require('../services/whatsapp');

describe('WhatsApp service', () => {
  const originalToken = process.env.WHATSAPP_TOKEN;
  const originalPhoneId = process.env.WHATSAPP_PHONE_ID;

  afterEach(() => {
    process.env.WHATSAPP_TOKEN = originalToken;
    process.env.WHATSAPP_PHONE_ID = originalPhoneId;
  });

  describe('isConfigured()', () => {
    test('returns false when env vars missing', () => {
      delete process.env.WHATSAPP_TOKEN;
      delete process.env.WHATSAPP_PHONE_ID;
      expect(whatsapp.isConfigured()).toBe(false);
    });

    test('returns false when only token set', () => {
      process.env.WHATSAPP_TOKEN = 'test_token';
      delete process.env.WHATSAPP_PHONE_ID;
      expect(whatsapp.isConfigured()).toBe(false);
    });

    test('returns true when both vars set', () => {
      process.env.WHATSAPP_TOKEN = 'test_token';
      process.env.WHATSAPP_PHONE_ID = '123456789';
      expect(whatsapp.isConfigured()).toBe(true);
    });
  });

  describe('sendText() — not configured', () => {
    beforeEach(() => {
      delete process.env.WHATSAPP_TOKEN;
      delete process.env.WHATSAPP_PHONE_ID;
    });

    test('returns not_configured when env missing', async () => {
      const result = await whatsapp.sendText('+79001234567', 'Hello');
      expect(result.sent).toBe(false);
      expect(result.reason).toBe('not_configured');
    });

    test('returns not_configured for empty phone', async () => {
      const result = await whatsapp.sendText('', 'Hello');
      expect(result.sent).toBe(false);
    });
  });

  describe('sendTemplate() — not configured', () => {
    beforeEach(() => {
      delete process.env.WHATSAPP_TOKEN;
    });

    test('returns not_configured', async () => {
      const result = await whatsapp.sendTemplate('+79001234567', 'order_status', 'ru', ['ORD-001', 'confirmed']);
      expect(result.sent).toBe(false);
      expect(result.reason).toBe('not_configured');
    });
  });

  describe('sendOrderStatusWA()', () => {
    test('returns no_phone when client_phone missing', async () => {
      const result = await whatsapp.sendOrderStatusWA({ order_number: 'ORD-001' }, 'confirmed', 'Подтверждена');
      expect(result.sent).toBe(false);
      expect(result.reason).toBe('no_phone');
    });

    test('returns no_phone for null order', async () => {
      const result = await whatsapp.sendOrderStatusWA(null, 'confirmed', 'Подтверждена');
      expect(result.sent).toBe(false);
      expect(result.reason).toBe('no_phone');
    });
  });

  describe('sendBookingConfirmationWA()', () => {
    test('returns no_phone when client_phone missing', async () => {
      const result = await whatsapp.sendBookingConfirmationWA({ order_number: 'ORD-001' });
      expect(result.sent).toBe(false);
      expect(result.reason).toBe('no_phone');
    });
  });

  describe('verifyWebhook()', () => {
    beforeEach(() => {
      process.env.WHATSAPP_VERIFY_TOKEN = 'my_secret_token';
    });

    afterEach(() => {
      delete process.env.WHATSAPP_VERIFY_TOKEN;
    });

    test('returns challenge on valid token', () => {
      const result = whatsapp.verifyWebhook({
        'hub.mode': 'subscribe',
        'hub.verify_token': 'my_secret_token',
        'hub.challenge': '12345',
      });
      expect(result).toBe('12345');
    });

    test('returns null on wrong token', () => {
      const result = whatsapp.verifyWebhook({
        'hub.mode': 'subscribe',
        'hub.verify_token': 'wrong_token',
        'hub.challenge': '12345',
      });
      expect(result).toBeNull();
    });

    test('returns null when verify token not configured', () => {
      delete process.env.WHATSAPP_VERIFY_TOKEN;
      const result = whatsapp.verifyWebhook({
        'hub.mode': 'subscribe',
        'hub.verify_token': 'any',
        'hub.challenge': '12345',
      });
      expect(result).toBeNull();
    });

    test('returns null when mode is not subscribe', () => {
      const result = whatsapp.verifyWebhook({
        'hub.mode': 'other',
        'hub.verify_token': 'my_secret_token',
        'hub.challenge': '12345',
      });
      expect(result).toBeNull();
    });
  });
});
