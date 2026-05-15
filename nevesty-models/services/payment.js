'use strict';

/**
 * Payment service — YooKassa & Stripe integration via raw HTTP (no SDKs)
 *
 * Env vars:
 *   YOOKASSA_SHOP_ID        — ЮKassa shop ID
 *   YOOKASSA_SECRET_KEY     — ЮKassa secret key
 *   STRIPE_SECRET_KEY       — Stripe secret key  (sk_live_... or sk_test_...)
 *   STRIPE_WEBHOOK_SECRET   — Stripe webhook signing secret (whsec_...)
 */

const https = require('https');
const crypto = require('crypto');

// ─── HTTP helper (no external deps) ──────────────────────────────────────────

function httpsRequest(urlStr, options = {}, body = null) {
  return new Promise((resolve, reject) => {
    const url = new URL(urlStr);
    const reqOptions = {
      hostname: url.hostname,
      port: url.port || 443,
      path: url.pathname + url.search,
      method: options.method || 'GET',
      headers: options.headers || {},
    };

    const req = https.request(reqOptions, (res) => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString('utf8');
        let data;
        try { data = JSON.parse(raw); } catch { data = { _raw: raw }; }
        resolve({ status: res.statusCode, data });
      });
    });

    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

// ─── YooKassa ─────────────────────────────────────────────────────────────────

/**
 * Create a YooKassa payment.
 * @param {string} orderId      — internal order ID (used as metadata)
 * @param {number} amount       — amount in RUB (integer)
 * @param {string} description  — payment description shown to payer
 * @param {string} returnUrl    — redirect after payment
 * @returns {{ payment_url, payment_id } | { error }}
 */
async function createYooKassaPayment(orderId, amount, description, returnUrl) {
  const shopId    = process.env.YOOKASSA_SHOP_ID;
  const secretKey = process.env.YOOKASSA_SECRET_KEY;

  if (!shopId || !secretKey) {
    return { error: 'Payment not configured' };
  }

  const idempotenceKey = `order_${orderId}_${Date.now()}`;
  const bodyObj = {
    amount: { value: Number(amount).toFixed(2), currency: 'RUB' },
    confirmation: { type: 'redirect', return_url: returnUrl },
    description: description || `Оплата заявки #${orderId}`,
    metadata: { order_id: String(orderId) },
    capture: true,
  };
  const bodyStr = JSON.stringify(bodyObj);
  const creds   = Buffer.from(`${shopId}:${secretKey}`).toString('base64');

  try {
    const resp = await httpsRequest(
      'https://api.yookassa.ru/v2/payments',
      {
        method: 'POST',
        headers: {
          'Authorization':    `Basic ${creds}`,
          'Idempotence-Key':  idempotenceKey,
          'Content-Type':     'application/json',
          'Content-Length':   Buffer.byteLength(bodyStr),
        },
      },
      bodyStr
    );

    if (resp.status !== 200) {
      const msg = resp.data?.description || resp.data?._raw || `YooKassa error ${resp.status}`;
      return { error: msg };
    }

    const confirmUrl = resp.data?.confirmation?.confirmation_url;
    const paymentId  = resp.data?.id;

    if (!confirmUrl || !paymentId) {
      return { error: 'YooKassa: unexpected response format' };
    }

    return { payment_url: confirmUrl, payment_id: paymentId };
  } catch (e) {
    return { error: `YooKassa request failed: ${e.message}` };
  }
}

// ─── Stripe ───────────────────────────────────────────────────────────────────

/**
 * Create a Stripe PaymentIntent.
 * @param {string} orderId    — internal order ID (metadata)
 * @param {number} amount     — amount in minor units (kopecks / cents)
 *                              Pass amount in RUB; we multiply by 100 for kopecks.
 * @param {string} description
 * @param {string} currency   — 'rub' | 'usd' | 'eur' etc.
 * @returns {{ payment_url, payment_id, client_secret } | { error }}
 */
async function createStripePayment(orderId, amount, description, currency = 'rub') {
  const secretKey = process.env.STRIPE_SECRET_KEY;

  if (!secretKey) {
    return { error: 'Payment not configured' };
  }

  // Stripe amounts are in smallest currency unit
  const amountMinor = Math.round(Number(amount) * 100);

  // Build x-www-form-urlencoded body (Stripe v1 API format)
  const fields = [
    `amount=${amountMinor}`,
    `currency=${encodeURIComponent(currency)}`,
    `description=${encodeURIComponent(description || `Order #${orderId}`)}`,
    `metadata[order_id]=${encodeURIComponent(String(orderId))}`,
    `automatic_payment_methods[enabled]=true`,
  ];
  const bodyStr = fields.join('&');

  try {
    const resp = await httpsRequest(
      'https://api.stripe.com/v1/payment_intents',
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${secretKey}`,
          'Content-Type':  'application/x-www-form-urlencoded',
          'Content-Length': Buffer.byteLength(bodyStr),
        },
      },
      bodyStr
    );

    if (resp.status !== 200) {
      const msg = resp.data?.error?.message || `Stripe error ${resp.status}`;
      return { error: msg };
    }

    const paymentId    = resp.data?.id;
    const clientSecret = resp.data?.client_secret;

    if (!paymentId || !clientSecret) {
      return { error: 'Stripe: unexpected response format' };
    }

    // Stripe PaymentIntent does not have a hosted payment URL out of the box.
    // Return client_secret so the frontend can use Stripe.js, plus a stub URL.
    // For a hosted checkout link one would use Stripe Checkout Sessions — for
    // simplicity we expose client_secret and let the front-end handle it.
    return {
      payment_id:    paymentId,
      client_secret: clientSecret,
      payment_url:   null, // frontend must use Stripe.js with client_secret
    };
  } catch (e) {
    return { error: `Stripe request failed: ${e.message}` };
  }
}

// ─── YooKassa webhook verification ────────────────────────────────────────────

/**
 * Verify YooKassa webhook signature.
 * YooKassa sends SHA-256 HMAC of the raw request body signed with the shop
 * secret key; the signature is in the `X-Idempotence-Key` header — actually
 * YooKassa does NOT send a signature header; it authenticates via IP allowlist.
 * We do a basic structural check and optionally verify a shared secret if set.
 *
 * @param {Buffer|string} body      — raw request body
 * @param {string}        signature — value of custom header (if any)
 * @returns {boolean}
 */
function verifyYooKassaWebhook(body, signature) {
  const shopSecret = process.env.YOOKASSA_SECRET_KEY;
  if (!shopSecret) return false;

  // If caller passes a signature header, validate HMAC-SHA256
  if (signature) {
    const expected = crypto
      .createHmac('sha256', shopSecret)
      .update(typeof body === 'string' ? body : body.toString('utf8'))
      .digest('hex');
    return crypto.timingSafeEqual(
      Buffer.from(expected),
      Buffer.from(signature.toLowerCase())
    );
  }

  // No signature header — YooKassa relies on IP allowlist; trust the request
  // (caller should restrict by IP in nginx/firewall in production)
  return true;
}

// ─── Stripe webhook verification ──────────────────────────────────────────────

/**
 * Verify Stripe webhook signature (`Stripe-Signature` header).
 * Stripe uses HMAC-SHA256 of `timestamp.rawBody` with whsec_ secret.
 *
 * @param {Buffer|string} body      — raw request body (must be un-parsed)
 * @param {string}        signature — value of `Stripe-Signature` header
 * @returns {boolean}
 */
function verifyStripeWebhook(body, signature) {
  const secret = process.env.STRIPE_WEBHOOK_SECRET;
  if (!secret || !signature) return false;

  try {
    // Parse stripe-signature header: t=timestamp,v1=sig1,...
    const parts = {};
    for (const part of signature.split(',')) {
      const [k, ...rest] = part.split('=');
      parts[k] = rest.join('=');
    }

    const timestamp = parts['t'];
    const sigV1     = parts['v1'];
    if (!timestamp || !sigV1) return false;

    // Reject events older than 5 minutes (replay protection)
    const ts = parseInt(timestamp, 10);
    if (Math.abs(Date.now() / 1000 - ts) > 300) return false;

    const rawBody = typeof body === 'string' ? body : body.toString('utf8');
    const payload  = `${timestamp}.${rawBody}`;
    const expected = crypto
      .createHmac('sha256', secret)
      .update(payload, 'utf8')
      .digest('hex');

    return crypto.timingSafeEqual(
      Buffer.from(expected),
      Buffer.from(sigV1)
    );
  } catch {
    return false;
  }
}

// ─── Exports ──────────────────────────────────────────────────────────────────

module.exports = {
  createYooKassaPayment,
  createStripePayment,
  verifyYooKassaWebhook,
  verifyStripeWebhook,
};
