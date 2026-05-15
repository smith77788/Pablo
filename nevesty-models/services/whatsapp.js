'use strict';

/**
 * WhatsApp Business API notification service.
 *
 * Env vars (all optional — service degrades gracefully if not set):
 *   WHATSAPP_TOKEN         — WhatsApp Cloud API access token
 *   WHATSAPP_PHONE_ID      — Sending phone number ID
 *   WHATSAPP_VERIFY_TOKEN  — Webhook verification token
 *
 * Uses WhatsApp Cloud API (Meta) — free tier supports template + text messages.
 */

const https = require('https');

const WA_BASE = 'https://graph.facebook.com/v19.0';

function _isConfigured() {
  return !!(process.env.WHATSAPP_TOKEN && process.env.WHATSAPP_PHONE_ID);
}

function _request(path, payload) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
    const url = new URL(WA_BASE + path);
    const options = {
      hostname: url.hostname,
      path: url.pathname,
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${process.env.WHATSAPP_TOKEN}`,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
    };
    const req = https.request(options, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(data) }); }
        catch { resolve({ status: res.statusCode, data: { _raw: data } }); }
      });
    });
    req.on('error', reject);
    req.setTimeout(8000, () => { req.destroy(); reject(new Error('WhatsApp request timeout')); });
    req.write(body);
    req.end();
  });
}

/**
 * Send a plain text message to a phone number.
 * @param {string} to      — recipient phone in E.164 format: +79001234567
 * @param {string} text    — message text (max 4096 chars)
 */
async function sendText(to, text) {
  if (!_isConfigured()) {
    console.info('[WhatsApp] not configured — skip text to', to);
    return { sent: false, reason: 'not_configured' };
  }
  const phone = to.replace(/[^+\d]/g, '');
  if (!phone) return { sent: false, reason: 'invalid_phone' };
  try {
    const result = await _request(`/${process.env.WHATSAPP_PHONE_ID}/messages`, {
      messaging_product: 'whatsapp',
      to: phone,
      type: 'text',
      text: { body: text.slice(0, 4096) },
    });
    return { sent: result.status === 200, status: result.status, data: result.data };
  } catch (e) {
    console.error('[WhatsApp] sendText error:', e.message);
    return { sent: false, error: e.message };
  }
}

/**
 * Send a template message (pre-approved by Meta).
 * @param {string} to           — recipient phone E.164
 * @param {string} templateName — template name registered in Meta dashboard
 * @param {string} lang         — language code e.g. 'ru'
 * @param {string[]} components — array of body parameter values
 */
async function sendTemplate(to, templateName, lang = 'ru', components = []) {
  if (!_isConfigured()) return { sent: false, reason: 'not_configured' };
  const phone = to.replace(/[^+\d]/g, '');
  if (!phone) return { sent: false, reason: 'invalid_phone' };
  const bodyParams = components.map(text => ({ type: 'text', text: String(text) }));
  try {
    const result = await _request(`/${process.env.WHATSAPP_PHONE_ID}/messages`, {
      messaging_product: 'whatsapp',
      to: phone,
      type: 'template',
      template: {
        name: templateName,
        language: { code: lang },
        components: bodyParams.length ? [{ type: 'body', parameters: bodyParams }] : [],
      },
    });
    return { sent: result.status === 200, status: result.status, data: result.data };
  } catch (e) {
    console.error('[WhatsApp] sendTemplate error:', e.message);
    return { sent: false, error: e.message };
  }
}

/**
 * Send order status update notification to client.
 */
async function sendOrderStatusWA(order, newStatus, statusLabel) {
  if (!order?.client_phone) return { sent: false, reason: 'no_phone' };
  const text =
    `Nevesty Models: статус вашей заявки #${order.order_number || order.id} изменён.\n` +
    `Новый статус: ${statusLabel || newStatus}\n` +
    `Для уточнений пишите менеджеру.`;
  return sendText(order.client_phone, text);
}

/**
 * Send booking confirmation to client.
 */
async function sendBookingConfirmationWA(order) {
  if (!order?.client_phone) return { sent: false, reason: 'no_phone' };
  const text =
    `✅ Nevesty Models: заявка #${order.order_number || order.id} принята!\n` +
    `Менеджер свяжется с вами в ближайшее время для уточнения деталей.`;
  return sendText(order.client_phone, text);
}

/**
 * Verify webhook from Meta (GET request with hub.challenge).
 * Returns the hub.challenge value if token matches, null otherwise.
 */
function verifyWebhook(query) {
  const verifyToken = process.env.WHATSAPP_VERIFY_TOKEN;
  if (!verifyToken) return null;
  if (query['hub.mode'] === 'subscribe' && query['hub.verify_token'] === verifyToken) {
    return query['hub.challenge'];
  }
  return null;
}

module.exports = {
  sendText,
  sendTemplate,
  sendOrderStatusWA,
  sendBookingConfirmationWA,
  verifyWebhook,
  isConfigured: _isConfigured,
};
