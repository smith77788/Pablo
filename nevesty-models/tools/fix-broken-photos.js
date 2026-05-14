#!/usr/bin/env node
/**
 * Заменяет все оставшиеся URL-фото на рабочие и загружает их в Telegram CDN.
 * После выполнения в БД не останется ни одного http-URL — только file_id.
 */
'use strict';
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const https   = require('https');
const { initDatabase, query, run } = require('../database');

const TOKEN   = process.env.TELEGRAM_BOT_TOKEN;
const ADMIN_ID = (process.env.ADMIN_TELEGRAM_IDS || '').split(',')[0].trim();

if (!TOKEN || TOKEN === 'your_bot_token_here') { console.error('❌ TELEGRAM_BOT_TOKEN не задан'); process.exit(1); }
if (!ADMIN_ID) { console.error('❌ ADMIN_TELEGRAM_IDS не задан'); process.exit(1); }

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Пул заведомо рабочих Unsplash fashion-фото (проверено)
const GOOD_PHOTOS = [
  'https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1488426862026-3ee34a7d66df?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1506956191951-7a88da4435e5?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1531746020798-e6953c6e8e04?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1515886657613-9f3515b0c78f?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1539109136881-3be0616acf4b?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1509631179647-0177331693ae?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1469334031218-e382a71b716b?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1485968579580-b6d095142e6e?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1541101767792-f9b2b1c4f127?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1558769132-cb1aea458c5e?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1561043433-aaf687c4cf04?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1552374196-1ab2a1c593e8?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1581044777550-4cfa60707c03?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1596993100471-c3905dafa78e?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1568252542512-9fe8fe9c87bb?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1579783902614-a3fb3927b6a5?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1605464315542-bda3e2f4e605?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1611042553365-9b101441c135?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1614624532983-4ce03382d63d?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1618375569909-3c8616cf7733?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1612336307429-8a898d10e223?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1529139574466-a303027c1d8b?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1524638431109-93d95c968f03?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1502323703975-b2b9e45a7c58?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1487412720507-e7ab37603c6f?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1596703263926-eb0762ee17e4?w=600&h=800&fit=crop',
];

// Получить запасное фото из пула (уникальное для каждого слота)
let poolIdx = 0;
function nextGoodPhoto() {
  const p = GOOD_PHOTOS[poolIdx % GOOD_PHOTOS.length];
  poolIdx++;
  return p;
}

function fetchBuffer(url) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { 'User-Agent': 'Mozilla/5.0' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location)
        return fetchBuffer(res.headers.location).then(resolve).catch(reject);
      if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode}`));
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks)));
    }).on('error', reject);
  });
}

function uploadToTg(buf, filename) {
  return new Promise((resolve, reject) => {
    const boundary = '----TGBound' + Date.now();
    const parts = [
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n${ADMIN_ID}\r\n`),
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="photo"; filename="${filename}"\r\nContent-Type: image/jpeg\r\n\r\n`),
      buf,
      Buffer.from(`\r\n--${boundary}--\r\n`),
    ];
    const body = Buffer.concat(parts);
    const req = https.request({
      hostname: 'api.telegram.org',
      path: `/bot${TOKEN}/sendPhoto`,
      method: 'POST',
      timeout: 30000,
      headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}`, 'Content-Length': body.length },
    }, (res) => {
      let raw = '';
      res.on('data', d => raw += d);
      res.on('end', () => {
        try {
          const j = JSON.parse(raw);
          if (j.ok) resolve(j.result.photo[j.result.photo.length - 1].file_id);
          else reject(new Error(j.description));
        } catch { reject(new Error('JSON parse')); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('Timeout')); });
    req.write(body); req.end();
  });
}

// Загружает URL (или запасной из пула) и возвращает file_id
async function toFileId(url, slot) {
  // Уже file_id — не трогаем
  if (!url || !url.startsWith('http')) return url;

  // Пробуем оригинальный URL, при ошибке — берём из пула
  for (let attempt = 0; attempt < 2; attempt++) {
    const src = attempt === 0 ? url : nextGoodPhoto();
    try {
      const buf    = await fetchBuffer(src);
      const fileId = await uploadToTg(buf, `photo_${slot}_${Date.now()}.jpg`);
      if (attempt > 0) console.log(`      ↳ заменено запасным фото`);
      return fileId;
    } catch (e) {
      if (attempt === 0) console.log(`      ↳ URL сломан (${e.message}), беру запасное...`);
    }
  }
  // Крайний случай — попробуем ещё одно из пула
  try {
    const buf    = await fetchBuffer(nextGoodPhoto());
    const fileId = await uploadToTg(buf, `photo_fallback_${slot}.jpg`);
    return fileId;
  } catch {
    console.error(`      ✗ Не удалось загрузить ни одно фото для слота ${slot}`);
    return null;
  }
}

async function main() {
  await initDatabase();
  const models = await query('SELECT id, name, photo_main, photos FROM models ORDER BY id');

  let totalFixed = 0;
  for (const m of models) {
    let gallery = [];
    try { gallery = JSON.parse(m.photos || '[]'); } catch {}
    const allPhotos = m.photo_main ? [m.photo_main, ...gallery] : gallery;
    const hasUrls = allPhotos.some(p => p && p.startsWith('http'));
    if (!hasUrls) { console.log(`  ✓ ${m.name}`); continue; }

    console.log(`  📤 ${m.name}...`);
    const newPhotos = [];
    for (let i = 0; i < allPhotos.length; i++) {
      const fid = await toFileId(allPhotos[i], `${m.id}_${i}`);
      if (fid) newPhotos.push(fid);
      await sleep(350);
    }

    const newMain    = newPhotos[0] || null;
    const newGallery = newPhotos.slice(1);
    await run('UPDATE models SET photo_main=?, photos=? WHERE id=?',
      [newMain, JSON.stringify(newGallery), m.id]);
    console.log(`     ✅ ${newPhotos.length} file_id сохранено`);
    totalFixed++;
    await sleep(300);
  }

  console.log(`\n✅ Готово: ${totalFixed} моделей обновлено. Все фото на CDN Telegram.\n`);
  process.exit(0);
}

main().catch(e => { console.error('Fatal:', e.message); process.exit(1); });
