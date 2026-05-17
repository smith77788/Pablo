#!/usr/bin/env node
/**
 * lovable-bridge.js — прямая коммуникация Claude ↔ Lovable
 *
 * Использует Lovable MCP Server (https://mcp.lovable.dev) или
 * Build-with-URL API для отправки инструкций в Lovable проект.
 *
 * Аргументы:
 *   --send "сообщение"     отправить инструкцию в Lovable
 *   --status               проверить статус проекта
 *   --read-response        прочитать последний ответ Lovable
 *   --project <id>         ID проекта (или из LOVABLE_PROJECT_ID)
 */

const https = require('https');
const fs = require('fs');
const path = require('path');

require('dotenv').config({ path: path.join(__dirname, '../.env') });

const LOVABLE_API_KEY = process.env.LOVABLE_API_KEY;
const LOVABLE_PROJECT_ID = process.env.LOVABLE_PROJECT_ID;
const QUEUE_FILE = path.join(__dirname, '../../CLAUDE_TO_LOVABLE.md');
const RESPONSE_FILE = path.join(__dirname, '../../LOVABLE_TO_CLAUDE.md');

const args = process.argv.slice(2);
const cmd = args[0];
const message = args[1];

// ─── Отправить инструкцию через MCP API ──────────────────────────
async function sendToLovable(text, projectId = LOVABLE_PROJECT_ID) {
  if (!LOVABLE_API_KEY) {
    // Fallback: записать в очередь файл (будет синкнут через Actions)
    writeToQueue(text);
    console.log('⚠️  LOVABLE_API_KEY не задан — инструкция записана в CLAUDE_TO_LOVABLE.md');
    console.log('   GitHub Actions синкнет файл в pablo-760fe3a5 автоматически.');
    console.log("   Открой Lovable Chat → 'Read CLAUDE_TO_LOVABLE.md and implement it'");
    return;
  }

  const body = JSON.stringify({ message: text });
  const options = {
    hostname: 'api.lovable.dev',
    path: `/v1/projects/${projectId}/messages`,
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${LOVABLE_API_KEY}`,
      'Content-Length': Buffer.byteLength(body),
    },
  };

  return new Promise((resolve, _reject) => {
    const req = https.request(options, res => {
      let data = '';
      res.on('data', chunk => {
        data += chunk;
      });
      res.on('end', () => {
        if (res.statusCode >= 400) {
          console.error(`❌ Lovable API error ${res.statusCode}:`, data);
          // Fallback to file queue
          writeToQueue(text);
          resolve(null);
        } else {
          const result = JSON.parse(data);
          console.log('✅ Отправлено в Lovable:', result);
          resolve(result);
        }
      });
    });
    req.on('error', e => {
      console.error('❌ Ошибка соединения:', e.message);
      writeToQueue(text);
      resolve(null);
    });
    req.write(body);
    req.end();
  });
}

// ─── Build-with-URL (открывает URL для создания нового проекта) ──
function buildWithUrl(prompt) {
  const encoded = encodeURIComponent(prompt);
  const url = `https://lovable.dev/?autosubmit=true#prompt=${encoded}`;
  console.log('🔗 Lovable Build URL:');
  console.log(url);
  console.log('\nОткрой этот URL в браузере (залогинься в Lovable) — проект создастся автоматически.');
  return url;
}

// ─── Записать в файл-очередь (синкается через GitHub Actions) ───
function writeToQueue(text) {
  const timestamp = new Date().toISOString();
  const entry = `\n## [${timestamp}]\n\n${text}\n\n---`;
  fs.appendFileSync(QUEUE_FILE, entry);
  console.log(`📝 Записано в CLAUDE_TO_LOVABLE.md`);
}

// ─── Прочитать ответ от Lovable ──────────────────────────────────
function readResponse() {
  if (!fs.existsSync(RESPONSE_FILE)) {
    console.log('📭 LOVABLE_TO_CLAUDE.md не существует — ответов пока нет.');
    return;
  }
  const content = fs.readFileSync(RESPONSE_FILE, 'utf8');
  console.log('📬 Последний ответ от Lovable:\n');
  console.log(content);
}

// ─── Main ────────────────────────────────────────────────────────
(async () => {
  switch (cmd) {
    case '--send':
      if (!message) {
        console.error("Укажи сообщение: --send 'текст'");
        process.exit(1);
      }
      await sendToLovable(message);
      break;

    case '--build-url':
      if (!message) {
        console.error("Укажи промпт: --build-url 'текст'");
        process.exit(1);
      }
      buildWithUrl(message);
      break;

    case '--queue':
      if (!message) {
        console.error("Укажи инструкцию: --queue 'текст'");
        process.exit(1);
      }
      writeToQueue(message);
      break;

    case '--read-response':
      readResponse();
      break;

    default:
      console.log(`
Lovable Bridge — прямая коммуникация Claude ↔ Lovable

Команды:
  --send "текст"       Отправить в Lovable API (нужен LOVABLE_API_KEY)
  --queue "текст"      Записать в файл-очередь (синкается через Actions)
  --build-url "текст"  Сгенерировать Build-with-URL ссылку
  --read-response      Прочитать ответ от Lovable

Переменные окружения (.env):
  LOVABLE_API_KEY      API ключ Lovable (получить в lovable.dev/settings)
  LOVABLE_PROJECT_ID   ID проекта в Lovable

Пример:
  node tools/lovable-bridge.js --queue "Add a contact page with a form"
  node tools/lovable-bridge.js --send "Fix the booking form validation"
      `);
  }
})();
