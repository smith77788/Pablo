'use strict';

const https = require('https');

async function _post(url, payload, headers = {}) {
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
      res.on('data', c => data += c);
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('error', reject);
    req.setTimeout(5000, () => { req.destroy(); reject(new Error('CRM webhook timeout')); });
    req.write(body);
    req.end();
  });
}

// AmoCRM: create deal on new order
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
    await _post(url, payload, apiKey ? { 'Authorization': `Bearer ${apiKey}` } : {});
  } catch (e) {
    console.error('[CRM] AmoCRM error:', e.message);
  }
}

// Bitrix24: create lead on new order
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
      }
    };
    await _post(url + 'crm.lead.add.json', payload);
  } catch (e) {
    console.error('[CRM] Bitrix24 error:', e.message);
  }
}

// Generic outgoing webhook (custom URL)
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
  const settings = { amocrm_webhook_url: amoCrmWebhook, amocrm_api_key: amoCrmKey,
                     bitrix24_webhook_url: bitrix24Webhook, crm_webhook_url: genericWebhook,
                     crm_webhook_secret: genericSecret };
  await Promise.all([
    notifyAmoCrm(event, order, settings),
    notifyBitrix24(event, order, settings),
    notifyGenericWebhook(event, order, settings),
  ]);
}

module.exports = { notifyCRM };
