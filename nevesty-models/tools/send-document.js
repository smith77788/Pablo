#!/usr/bin/env node
/** Отправляет файл всем admin через Telegram Bot API (multipart/form-data) */
const path = require('path');
const fs = require('fs');
const https = require('https');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const TOKEN     = process.env.TELEGRAM_BOT_TOKEN;
const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);

if (!TOKEN || TOKEN === 'your_bot_token_here') { console.error('❌ TELEGRAM_BOT_TOKEN не задан'); process.exit(1); }
if (!ADMIN_IDS.length)                         { console.error('❌ ADMIN_TELEGRAM_IDS не задан');  process.exit(1); }

const [filePath, caption = ''] = process.argv.slice(2);
if (!filePath) { console.error('Usage: node send-document.js <path> [caption]'); process.exit(1); }
if (!fs.existsSync(filePath)) { console.error('❌ Файл не найден:', filePath); process.exit(1); }

const fileContent = fs.readFileSync(filePath);
const fileName    = path.basename(filePath);

function sendDoc(chatId) {
  return new Promise((resolve, reject) => {
    const boundary = '----TGBoundary' + Date.now();
    const parts = [];
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n${chatId}\r\n`
    ));
    if (caption) parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n${caption}\r\n`
    ));
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="document"; filename="${fileName}"\r\nContent-Type: application/octet-stream\r\n\r\n`
    ));
    parts.push(fileContent);
    parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));
    const body = Buffer.concat(parts);

    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TOKEN}/sendDocument`,
      method: 'POST',
      timeout: 60000,
      headers: {
        'Content-Type': `multipart/form-data; boundary=${boundary}`,
        'Content-Length': body.length
      }
    }, res => {
      let raw = '';
      res.on('data', d => raw += d);
      res.on('end', () => {
        try {
          const j = JSON.parse(raw);
          if (j.ok) { console.log(`✅ Отправлено → ${chatId}`); resolve(); }
          else { console.error(`❌ TG error ${chatId}:`, j.description); resolve(); }
        } catch { resolve(); }
      });
    });
    req.on('error', e => { console.error('HTTP error:', e.message); resolve(); });
    req.on('timeout', () => { req.destroy(); console.error('Timeout'); resolve(); });
    req.write(body);
    req.end();
  });
}

(async () => {
  console.log(`📤 Отправляю ${fileName} (${Math.round(fileContent.length/1024)}кб) → ${ADMIN_IDS.length} адм.`);
  for (const id of ADMIN_IDS) await sendDoc(id);
  console.log('✅ Готово');
})();
