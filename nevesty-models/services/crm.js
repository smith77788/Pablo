'use strict';

/**
 * CRM integration service for exporting orders to AmoCRM/Bitrix24.
 * Uses DEV_MODE when CRM credentials are not configured.
 */

const https = require('https');

const DEV_MODE = !process.env.AMOCRM_SUBDOMAIN && !process.env.BITRIX24_WEBHOOK_URL;

/**
 * Export new order to configured CRM.
 * @param {Object} order - Order data from DB
 * @returns {Promise<{ok: boolean, id?: string|number}>}
 */
async function exportOrderToCrm(order) {
  if (DEV_MODE) {
    console.log('[CRM DEV] Would export order:', order.id);
    return { ok: true, dev: true };
  }

  if (process.env.BITRIX24_WEBHOOK_URL) {
    return exportToBitrix24(order);
  }

  if (process.env.AMOCRM_SUBDOMAIN) {
    return exportToAmoCrm(order);
  }

  return { ok: false, error: 'No CRM configured' };
}

async function exportToBitrix24(order) {
  const webhookUrl = process.env.BITRIX24_WEBHOOK_URL;
  const body = {
    fields: {
      TITLE: `Заявка #${order.id} — ${order.client_name || 'Клиент'} (${order.event_type || 'мероприятие'})`,
      NAME: order.client_name || '',
      PHONE: [{ VALUE: order.client_phone || '', VALUE_TYPE: 'MOBILE' }],
      EMAIL: order.client_email ? [{ VALUE: order.client_email, VALUE_TYPE: 'WORK' }] : [],
      COMMENTS: `Бюджет: ${order.budget || '?'}\nМодель: ${order.model_name || 'не выбрана'}\nДата: ${order.event_date || '?'}`,
      SOURCE_ID: 'TELEGRAM_BOT',
    },
  };

  const res = await fetch(`${webhookUrl}/crm.lead.add.json`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10000),
  });

  const data = await res.json();
  return { ok: !!data.result, id: data.result };
}

async function exportToAmoCrm(order) {
  // AmoCRM OAuth2 requires token management — simplified version
  const subdomain = process.env.AMOCRM_SUBDOMAIN;
  const token = process.env.AMOCRM_ACCESS_TOKEN;
  if (!token) return { ok: false, error: 'AMOCRM_ACCESS_TOKEN not set' };

  const res = await fetch(`https://${subdomain}.amocrm.ru/api/v4/leads`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify([
      {
        name: `Заявка #${order.id} — ${order.client_name || 'Клиент'}`,
        price: parseInt(order.budget) || 0,
        _embedded: { contacts: [{ first_name: order.client_name || '' }] },
      },
    ]),
    signal: AbortSignal.timeout(10000),
  });

  const data = await res.json();
  const leadId = data._embedded?.leads?.[0]?.id;
  return { ok: !!leadId, id: leadId };
}

/**
 * Register a webhook endpoint for incoming CRM events.
 * @param {import('express').Router} router - Express router
 */
function registerWebhooks(router) {
  // POST /api/crm/webhook/bitrix24 — incoming Bitrix24 webhooks
  router.post('/crm/webhook/bitrix24', (req, res) => {
    console.log('[CRM] Bitrix24 webhook received:', req.body?.event);
    // TODO: Handle Bitrix24 events (deal status change, etc.)
    res.json({ ok: true });
  });

  // POST /api/crm/webhook/amocrm — incoming AmoCRM webhooks
  router.post('/crm/webhook/amocrm', (req, res) => {
    console.log('[CRM] AmoCRM webhook received');
    res.json({ ok: true });
  });
}

// ─── Legacy settings-based API (backward compatibility) ───────────────────────

function _post(url, payload, headers = {}) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
    const u = new URL(url);
    const options = {
      hostname: u.hostname,
      port: u.port || 443,
      path: u.pathname + u.search,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        ...headers,
      },
    };
    const req = https.request(options, res => {
      let data = '';
      res.on('data', c => (data += c));
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('error', reject);
    req.setTimeout(5000, () => {
      req.destroy();
      reject(new Error('CRM webhook timeout'));
    });
    req.write(body);
    req.end();
  });
}

async function notifyAmoCrm(event, order, settings) {
  const url = settings.amocrm_webhook_url;
  if (!url) return;
  try {
    const payload = {
      event,
      order_id: order.id,
      order_number: order.order_number,
      client_name: order.client_name,
      client_phone: order.client_phone,
      client_email: order.client_email || '',
      event_type: order.event_type,
      budget: order.budget,
      status: order.status,
      model_name: order.model_name || '',
      created_at: order.created_at,
    };
    const apiKey = settings.amocrm_api_key;
    await _post(url, payload, apiKey ? { Authorization: `Bearer ${apiKey}` } : {});
  } catch (e) {
    console.error('[CRM] AmoCRM error:', e.message);
  }
}

async function notifyBitrix24(event, order, settings) {
  const url = settings.bitrix24_webhook_url;
  if (!url) return;
  try {
    const payload = {
      fields: {
        TITLE: `Заявка ${order.order_number} — ${order.client_name}`,
        NAME: order.client_name,
        PHONE: [{ VALUE: order.client_phone, VALUE_TYPE: 'WORK' }],
        EMAIL: order.client_email ? [{ VALUE: order.client_email, VALUE_TYPE: 'WORK' }] : [],
        COMMENTS: `Тип: ${order.event_type}, Бюджет: ${order.budget || '—'}, Статус: ${order.status}`,
        SOURCE_ID: 'OTHER',
        SOURCE_DESCRIPTION: 'Nevesty Models Bot',
      },
    };
    await _post(url + 'crm.lead.add.json', payload);
  } catch (e) {
    console.error('[CRM] Bitrix24 error:', e.message);
  }
}

async function notifyGenericWebhook(event, order, settings) {
  const url = settings.crm_webhook_url;
  if (!url) return;
  try {
    const secret = settings.crm_webhook_secret;
    const headers = {};
    if (secret) headers['X-Webhook-Secret'] = secret;
    await _post(url, { event, order, timestamp: new Date().toISOString() }, headers);
  } catch (e) {
    console.error('[CRM] Generic webhook error:', e.message);
  }
}

async function notifyCRM(event, order, getSetting) {
  const [amoCrmWebhook, amoCrmKey, bitrix24Webhook, genericWebhook, genericSecret] = await Promise.all([
    getSetting('amocrm_webhook_url'),
    getSetting('amocrm_api_key'),
    getSetting('bitrix24_webhook_url'),
    getSetting('crm_webhook_url'),
    getSetting('crm_webhook_secret'),
  ]);
  const settings = {
    amocrm_webhook_url: amoCrmWebhook,
    amocrm_api_key: amoCrmKey,
    bitrix24_webhook_url: bitrix24Webhook,
    crm_webhook_url: genericWebhook,
    crm_webhook_secret: genericSecret,
  };
  await Promise.all([
    notifyAmoCrm(event, order, settings),
    notifyBitrix24(event, order, settings),
    notifyGenericWebhook(event, order, settings),
  ]);
}

module.exports = { exportOrderToCrm, registerWebhooks, DEV_MODE, notifyCRM };
