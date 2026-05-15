'use strict';

describe('Email Service', () => {
  it('exports required functions', () => {
    const email = require('../services/email');
    expect(typeof email.sendOrderStatusEmail).toBe('function');
    expect(typeof email.sendNewOrderEmail).toBe('function');
  });

  it('returns false when SMTP not configured', async () => {
    const email = require('../services/email');
    const result = await email.sendOrderStatusEmail(
      { client_email: 'test@test.com', order_number: '001', client_name: 'Test' },
      'confirmed',
      'Подтверждена'
    );
    expect(result).toBe(false); // No SMTP in test env
  });

  it('returns false when order has no email', async () => {
    const email = require('../services/email');
    const result = await email.sendOrderStatusEmail(
      { order_number: '001' },
      'confirmed',
      'Подтверждена'
    );
    expect(result).toBe(false);
  });
});
