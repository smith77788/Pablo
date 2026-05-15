'use strict';
const https = require('https');

const SMS_RU_API = 'https://sms.ru/sms/send';
const SMS_RU_API_ID = process.env.SMS_RU_API_ID || '';
const SMS_FROM = process.env.SMS_FROM || 'NEVESTY';

/**
 * Send SMS via sms.ru API
 * Returns {success, sms_id} or throws on error
 */
async function sendSMS(phone, text) {
  if (!SMS_RU_API_ID) {
    console.log('[SMS] SMS_RU_API_ID not set, skipping:', phone, text.slice(0, 50));
    return { success: false, reason: 'not_configured' };
  }

  // Normalize phone to 79xxxxxxxxx format
  const digits = String(phone || '').replace(/\D/g, '');
  let normalized = digits;
  if (digits.length === 11 && digits.startsWith('8')) normalized = '7' + digits.slice(1);
  if (digits.length === 10) normalized = '7' + digits;

  const params = new URLSearchParams({
    api_id: SMS_RU_API_ID,
    to: normalized,
    msg: text,
    from: SMS_FROM,
    json: '1',
    test: process.env.NODE_ENV !== 'production' ? '1' : '0',
  });

  return new Promise((resolve, reject) => {
    const url = `${SMS_RU_API}?${params}`;
    https.get(url, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          const json = JSON.parse(data);
          if (json.status === 'OK') {
            resolve({ success: true, sms_id: Object.values(json.sms || {})[0]?.sms_id });
          } else {
            console.error('[SMS] Error:', json.status_code, json.status_text);
            resolve({ success: false, reason: json.status_text });
          }
        } catch (e) {
          reject(new Error('SMS parse error: ' + e.message));
        }
      });
    }).on('error', reject);
  });
}

/**
 * Send order status change SMS to client
 */
async function sendStatusChangeSMS(phone, orderNumber, newStatus) {
  const statusTexts = {
    confirmed:   `Заявка ${orderNumber} подтверждена! Ждём вас.`,
    in_progress: `Работа по заявке ${orderNumber} началась.`,
    completed:   `Заявка ${orderNumber} выполнена. Спасибо за доверие!`,
    cancelled:   `Заявка ${orderNumber} отменена. Подробности у менеджера.`,
  };
  const text = statusTexts[newStatus];
  if (!text) return { success: false, reason: 'status_not_notifiable' };
  return sendSMS(phone, text);
}

/**
 * Send new order confirmation SMS to client
 */
async function sendOrderConfirmationSMS(phone, orderNumber) {
  const text = `Заявка ${orderNumber} принята! Менеджер свяжется с вами.`;
  return sendSMS(phone, text);
}

module.exports = { sendSMS, sendStatusChangeSMS, sendOrderConfirmationSMS };
