'use strict';
/**
 * SMS notification service.
 * Currently supports SMS.ru, SMSC.ru, and Twilio.
 * Set SMS_PROVIDER env var to enable: 'smsru' | 'smsc' | 'twilio'
 *
 * Required env vars for SMS.ru:  SMS_PROVIDER=smsru  SMS_RU_API_KEY=<key>
 * Required env vars for SMSC:    SMS_PROVIDER=smsc   SMSC_LOGIN=<login>  SMSC_PASSWORD=<pass>
 * Required env vars for Twilio:  SMS_PROVIDER=twilio TWILIO_ACCOUNT_SID=<sid> TWILIO_AUTH_TOKEN=<token> TWILIO_FROM_NUMBER=<+number>
 *
 * Legacy support: SMS_RU_API_ID (old key name) still works if SMS_PROVIDER is not set.
 */

const SMS_PROVIDER = process.env.SMS_PROVIDER || (process.env.SMS_RU_API_ID ? 'smsru' : '');
const SMS_FROM = process.env.SMS_FROM || 'NEVESTY';

/**
 * Normalize phone number to digits only, with leading country code.
 * Handles Russian numbers: 8XXXXXXXXXX → 7XXXXXXXXXX, 10-digit → 7XXXXXXXXXX
 */
function normalizePhone(phone) {
  const digits = String(phone || '').replace(/\D/g, '');
  if (!digits || digits.length < 10) return null;
  if (digits.length === 11 && digits.startsWith('8')) return '7' + digits.slice(1);
  if (digits.length === 10) return '7' + digits;
  return digits;
}

/**
 * Send SMS to a phone number.
 * Returns true if sent, false if not configured, throws on actual provider error.
 */
async function sendSms(phone, text) {
  if (!phone || !text) return false;

  const normalized = normalizePhone(phone);
  if (!normalized) return false;

  if (SMS_PROVIDER === 'smsru') {
    const apiKey = process.env.SMS_RU_API_KEY || process.env.SMS_RU_API_ID;
    if (apiKey) return sendViaSmsRu(normalized, text, apiKey);
  }

  if (SMS_PROVIDER === 'smsc' && process.env.SMSC_LOGIN) {
    return sendViaSmsc(normalized, text);
  }

  if (SMS_PROVIDER === 'twilio' && process.env.TWILIO_ACCOUNT_SID) {
    return sendViaTwilio(normalized, text);
  }

  // Not configured — log stub and return false
  console.log(`[SMS] Would send to ${normalized}: ${text.substring(0, 50)}`);
  return false;
}

async function sendViaSmsRu(phone, text, apiKey) {
  const params = new URLSearchParams({
    api_id: apiKey,
    to: phone,
    msg: text,
    from: SMS_FROM,
    json: '1',
    test: process.env.NODE_ENV !== 'production' ? '1' : '0',
  });
  const res = await fetch(`https://sms.ru/sms/send?${params}`);
  const data = await res.json();
  if (data.status !== 'OK') throw new Error(`SMS.ru error: ${data.status_text}`);
  return true;
}

async function sendViaSmsc(phone, text) {
  const params = new URLSearchParams({
    login: process.env.SMSC_LOGIN,
    psw: process.env.SMSC_PASSWORD,
    phones: phone,
    mes: text,
    from: SMS_FROM,
    fmt: '3', // JSON response
  });
  const res = await fetch(`https://smsc.ru/sys/send.php?${params}`);
  const data = await res.json();
  if (data.error) throw new Error(`SMSC error: ${data.error}`);
  return true;
}

async function sendViaTwilio(phone, text) {
  const { TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER } = process.env;
  const url = `https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Messages.json`;
  const auth = Buffer.from(`${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}`).toString('base64');
  const body = new URLSearchParams({
    From: TWILIO_FROM_NUMBER,
    To: `+${phone}`,
    Body: text,
  });
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Authorization': `Basic ${auth}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body,
  });
  if (!res.ok) {
    const err = await res.text().catch(() => res.status);
    throw new Error(`Twilio error ${res.status}: ${err}`);
  }
  return true;
}

// ─── Higher-level helpers ──────────────────────────────────────────────────────

/**
 * Send order status change SMS to client.
 */
async function sendOrderStatusSms(phone, orderNumber, newStatus) {
  const statusMessages = {
    confirmed:   `Ваша заявка ${orderNumber} подтверждена! Менеджер свяжется с вами в ближайшее время. Nevesty Models`,
    completed:   `Заявка ${orderNumber} завершена. Спасибо за выбор Nevesty Models! Будем рады вашему отзыву.`,
    cancelled:   `Заявка ${orderNumber} отменена. Если есть вопросы, звоните менеджеру. Nevesty Models`,
    in_progress: `Работа по заявке ${orderNumber} началась. Nevesty Models`,
  };
  const text = statusMessages[newStatus];
  if (!text) return false;
  try {
    return await sendSms(phone, text);
  } catch (e) {
    console.error('[SMS] Error sending order status SMS:', e.message);
    return false;
  }
}

/**
 * Send booking confirmation SMS after new order is placed.
 */
async function sendBookingConfirmationSms(phone, orderNumber) {
  const text = `Заявка ${orderNumber} принята! Менеджер Nevesty Models свяжется с вами в течение часа для подтверждения.`;
  try {
    return await sendSms(phone, text);
  } catch (e) {
    console.error('[SMS] Error sending booking confirmation SMS:', e.message);
    return false;
  }
}

// ─── Legacy aliases (backward-compat with existing api.js calls) ───────────────

/** @deprecated Use sendSms() */
async function sendSMS(phone, text) {
  const normalized = normalizePhone(phone);
  if (!normalized) return { success: false, reason: 'invalid_phone' };
  try {
    const sent = await sendSms(normalized, text);
    return { success: sent, reason: sent ? undefined : 'not_configured' };
  } catch (e) {
    console.error('[SMS] Error:', e.message);
    return { success: false, reason: e.message };
  }
}

/** @deprecated Use sendOrderStatusSms() */
async function sendStatusChangeSMS(phone, orderNumber, newStatus) {
  return sendOrderStatusSms(phone, orderNumber, newStatus);
}

/** @deprecated Use sendBookingConfirmationSms() */
async function sendOrderConfirmationSMS(phone, orderNumber) {
  return sendBookingConfirmationSms(phone, orderNumber);
}

module.exports = {
  sendSms,
  sendOrderStatusSms,
  sendBookingConfirmationSms,
  // Legacy exports kept for backward compatibility
  sendSMS,
  sendStatusChangeSMS,
  sendOrderConfirmationSMS,
};
