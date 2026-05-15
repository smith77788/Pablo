#!/usr/bin/env node
/**
 * Загружает фото моделей напрямую в Telegram и сохраняет file_id в БД.
 * После этого фото отдаётся с CDN Telegram — быстро, без зависимости от внешних URL.
 */
'use strict';
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const https = require('https');
const http = require('http');
const _path = require('path');
const { initDatabase, query, run } = require('../database');

const TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ADMIN_ID = (process.env.ADMIN_TELEGRAM_IDS || '').split(',')[0].trim();

if (!TOKEN || TOKEN === 'your_bot_token_here') {
  console.error('❌ TELEGRAM_BOT_TOKEN не задан в .env');
  process.exit(1);
}
if (!ADMIN_ID) {
  console.error('❌ ADMIN_TELEGRAM_IDS не задан в .env');
  process.exit(1);
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// Скачивает URL как Buffer
function fetchBuffer(url) {
  return new Promise((resolve, reject) => {
    const mod = url.startsWith('https') ? https : http;
    mod
      .get(url, { headers: { 'User-Agent': 'Mozilla/5.0' } }, res => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          return fetchBuffer(res.headers.location).then(resolve).catch(reject);
        }
        if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
        const chunks = [];
        res.on('data', c => chunks.push(c));
        res.on('end', () => resolve(Buffer.concat(chunks)));
      })
      .on('error', reject);
  });
}

// Отправляет фото в Telegram через multipart, возвращает file_id
function sendPhotoToTg(imageBuffer, filename, chatId) {
  return new Promise((resolve, reject) => {
    const boundary = '----TGBound' + Date.now();
    const parts = [];
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n${chatId}\r\n`));
    parts.push(
      Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="photo"; filename="${filename}"\r\nContent-Type: image/jpeg\r\n\r\n`
      )
    );
    parts.push(imageBuffer);
    parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));
    const body = Buffer.concat(parts);

    const req = https.request(
      {
        hostname: 'api.telegram.org',
        path: `/bot${TOKEN}/sendPhoto`,
        method: 'POST',
        timeout: 30000,
        headers: {
          'Content-Type': `multipart/form-data; boundary=${boundary}`,
          'Content-Length': body.length,
        },
      },
      res => {
        let raw = '';
        res.on('data', d => (raw += d));
        res.on('end', () => {
          try {
            const j = JSON.parse(raw);
            if (j.ok) {
              // Берём самый большой размер фото
              const photos = j.result.photo;
              const best = photos[photos.length - 1];
              resolve(best.file_id);
            } else {
              reject(new Error(j.description || 'TG error'));
            }
          } catch {
            reject(new Error('JSON parse error'));
          }
        });
      }
    );
    req.on('error', reject);
    req.on('timeout', () => {
      req.destroy();
      reject(new Error('Timeout'));
    });
    req.write(body);
    req.end();
  });
}

// Удаляет последнее сообщение чтобы чат не засорялся
async function _deleteMsg(chatId, msgId) {
  return new Promise(resolve => {
    const body = JSON.stringify({ chat_id: chatId, message_id: msgId });
    const req = https.request(
      {
        hostname: 'api.telegram.org',
        path: `/bot${TOKEN}/deleteMessage`,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      },
      res => {
        res.resume();
        resolve();
      }
    );
    req.on('error', () => resolve());
    req.write(body);
    req.end();
  });
}

async function main() {
  await initDatabase();
  const models = await query('SELECT id, name, photo_main, photos FROM models ORDER BY id');
  console.log(`\n📤 Загружаю фото в Telegram для ${models.length} моделей...\n`);

  for (const m of models) {
    let urls = [];
    try {
      urls = JSON.parse(m.photos || '[]');
    } catch {}
    if (m.photo_main && !urls.includes(m.photo_main)) urls.unshift(m.photo_main);

    // Пропускаем если уже file_id (не URL)
    const needsUpload = urls.filter(u => u.startsWith('http'));
    if (needsUpload.length === 0) {
      console.log(`  ✓ ${m.name} — уже загружено`);
      continue;
    }

    console.log(`  📤 ${m.name} (${needsUpload.length} фото)...`);
    const fileIds = [];

    for (let i = 0; i < needsUpload.length; i++) {
      const url = needsUpload[i];
      try {
        const buf = await fetchBuffer(url);
        const fileId = await sendPhotoToTg(buf, `model_${m.id}_${i}.jpg`, ADMIN_ID);
        fileIds.push(fileId);
        process.stdout.write(`    ✅ фото ${i + 1}/${needsUpload.length}\r`);
        await sleep(400); // не спамим Telegram API
      } catch (e) {
        console.warn(`\n    ⚠️  фото ${i + 1} не загружено: ${e.message} — оставляю URL`);
        fileIds.push(url); // fallback — оставляем URL если загрузка не удалась
      }
    }

    // Сохраняем: первый file_id — photo_main, остальные — photos[]
    const newMain = fileIds[0] || m.photo_main;
    const newGallery = fileIds.slice(1);
    await run('UPDATE models SET photo_main=?, photos=? WHERE id=?', [newMain, JSON.stringify(newGallery), m.id]);

    console.log(`\n    💾 Сохранено: photo_main + ${newGallery.length} в галерее`);
    await sleep(500);
  }

  console.log('\n✅ Всё готово — фото теперь на CDN Telegram!\n');
  process.exit(0);
}

main().catch(e => {
  console.error('Fatal:', e.message);
  process.exit(1);
});
