#!/usr/bin/env node
// Send a message to all admin Telegram IDs from .env
// Usage:
//   node tools/notify.js "your message"
//   echo "your message" | node tools/notify.js
//   node tools/notify.js --from "Claude" "starting work on bot.js"
//   node tools/notify.js --from "Agent: bot-rewriter" "task done"

const path = require('path');
const https = require('https');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);

if (!TOKEN || TOKEN === 'your_bot_token_here') {
  console.error('❌ TELEGRAM_BOT_TOKEN not set in .env');
  process.exit(1);
}
if (!ADMIN_IDS.length) {
  console.error('❌ ADMIN_TELEGRAM_IDS not set in .env');
  process.exit(1);
}

// ─── Parse args ──────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
let from = null;
const textParts = [];
for (let i = 0; i < args.length; i++) {
  if ((args[i] === '--from' || args[i] === '-f') && args[i + 1]) {
    from = args[++i];
  } else {
    textParts.push(args[i]);
  }
}
let body = textParts.join(' ').trim();

// ─── Read stdin if no body ───────────────────────────────────────────────────
async function readStdin() {
  if (process.stdin.isTTY) return '';
  return new Promise(resolve => {
    let data = '';
    process.stdin.on('data', chunk => data += chunk);
    process.stdin.on('end', () => resolve(data.trim()));
  });
}

// ─── Send to Telegram ────────────────────────────────────────────────────────
function sendRaw(chatId, text, useMarkdown) {
  return new Promise(resolve => {
    const payload = JSON.stringify({
      chat_id: chatId,
      text,
      ...(useMarkdown ? { parse_mode: 'Markdown' } : {}),
      disable_web_page_preview: true
    });
    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TOKEN}/sendMessage`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) }
    }, res => {
      let resp = '';
      res.on('data', c => resp += c);
      res.on('end', () => {
        try {
          const json = JSON.parse(resp);
          resolve(json);
        } catch { resolve({ ok: false, description: 'parse error' }); }
      });
    });
    req.on('error', e => resolve({ ok: false, description: e.message }));
    req.write(payload);
    req.end();
  });
}

async function send(chatId, text) {
  let res = await sendRaw(chatId, text, true);
  if (!res.ok && /parse entities|can't parse/i.test(res.description || '')) {
    // Markdown failed — fall back to plain text
    res = await sendRaw(chatId, text, false);
  }
  if (!res.ok) console.error(`[notify] ${chatId}: ${res.description}`);
  return res.ok;
}

// ─── Main ────────────────────────────────────────────────────────────────────
(async () => {
  if (!body) body = await readStdin();
  if (!body) {
    console.error('❌ No message provided. Usage: node notify.js "text" [--from "name"]');
    process.exit(1);
  }
  // Telegram message limit is 4096 chars
  if (body.length > 3900) body = body.slice(0, 3900) + '\n…(обрізано)';
  const ts = new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const header = from ? `🤖 *${from}* · _${ts}_` : `🤖 _${ts}_`;
  const text = `${header}\n${body}`;
  const results = await Promise.all(ADMIN_IDS.map(id => send(id, text)));
  const ok = results.filter(Boolean).length;
  console.log(`✓ sent to ${ok}/${ADMIN_IDS.length} admins`);
})();
