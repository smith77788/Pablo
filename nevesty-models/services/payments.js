'use strict';
/**
 * YooKassa (ЮKassa) payment integration — simplified wrapper.
 * Requires env vars: YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
 * Falls back gracefully when vars are not set (dev mode).
 *
 * For the full payment service (including Stripe) see services/payment.js.
 */

const https = require('https');

const SHOP_ID = process.env.YOOKASSA_SHOP_ID;
const SECRET_KEY = process.env.YOOKASSA_SECRET_KEY;
const DEV_MODE = !SHOP_ID || !SECRET_KEY;

/**
 * Create a payment for an order.
 * @param {object} order      — order object with id, budget, client_name
 * @param {string} returnUrl  — URL to redirect the user after payment
 * @returns {Promise<{paymentId: string, confirmationUrl: string, status: string}>}
 */
async function createPayment(order, returnUrl) {
  if (DEV_MODE) {
    const mockId = `mock_${Date.now()}`;
    return {
      paymentId: mockId,
      confirmationUrl: `${returnUrl}?mock=1&orderId=${order.id}`,
      status: 'pending',
    };
  }

  const idempotenceKey = `order_${order.id}_${Date.now()}`;
  const amountStr = String(order.budget || '').replace(/[^\d]/g, '') || '100';
  const body = JSON.stringify({
    amount: { value: Number(amountStr).toFixed(2), currency: 'RUB' },
    confirmation: { type: 'redirect', return_url: returnUrl },
    capture: true,
    description: `Заявка #${order.id} — ${order.client_name || 'клиент'}`,
    metadata: { order_id: String(order.id) },
  });

  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        hostname: 'api.yookassa.ru',
        port: 443,
        path: '/v3/payments',
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
          'Idempotence-Key': idempotenceKey,
          Authorization: `Basic ${Buffer.from(`${SHOP_ID}:${SECRET_KEY}`).toString('base64')}`,
        },
      },
      res => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => {
          try {
            const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
            if (parsed.id && parsed.confirmation?.confirmation_url) {
              resolve({
                paymentId: parsed.id,
                confirmationUrl: parsed.confirmation.confirmation_url,
                status: parsed.status,
              });
            } else {
              reject(new Error(parsed.description || 'YooKassa: unexpected response'));
            }
          } catch (e) {
            reject(e);
          }
        });
      }
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

/**
 * Verify YooKassa webhook by request IP.
 * YooKassa authenticates via IP allowlist (no signature header).
 * In dev mode (no credentials configured) always returns true.
 *
 * @param {Buffer|string} _body — raw request body (unused; reserved for future HMAC support)
 * @param {string}         ip   — request IP
 * @returns {boolean}
 */
function verifyWebhook(_body, ip) {
  if (DEV_MODE) return true;
  // Official YooKassa IP ranges (2024)
  const ALLOWED_PREFIXES = ['185.71.76.', '185.71.77.', '77.75.153.', '77.75.156.11', '77.75.156.35'];
  return ALLOWED_PREFIXES.some(prefix => ip.startsWith(prefix));
}

/**
 * Parse a YooKassa webhook payload into a normalised event object.
 * @param {Buffer|string|object} body
 * @returns {{ type, paymentId, status, orderId, amount } | null}
 */
function parseWebhookEvent(body) {
  try {
    const event = typeof body === 'string' || Buffer.isBuffer(body) ? JSON.parse(body.toString('utf8')) : body;
    const metadata = event?.object?.metadata || {};
    return {
      type: event.event,
      paymentId: event.object?.id || null,
      status: event.object?.status || null,
      orderId: parseInt(metadata.order_id) || null,
      amount: parseFloat(event.object?.amount?.value) || 0,
    };
  } catch {
    return null;
  }
}

module.exports = { createPayment, verifyWebhook, parseWebhookEvent, DEV_MODE };
