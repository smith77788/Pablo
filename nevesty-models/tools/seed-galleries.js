#!/usr/bin/env node
/**
 * Заполняет поле photos для всех моделей у которых оно пустое.
 * Использует Unsplash open-source фото — разные наборы для каждой модели.
 */
'use strict';
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const { initDatabase, query, run } = require('../database');

// Пул fashion-фото с Unsplash (публичные URL)
const PHOTO_POOL = [
  'https://images.unsplash.com/photo-1529665419890-69e9c1a3e4ab?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1488426862026-3ee34a7d66df?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1524502397800-2ece493e0b93?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1506956191951-7a88da4435e5?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1523264653568-d3c4b105b0c4?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1531746020798-e6953c6e8e04?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1515886657613-9f3515b0c78f?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1539109136881-3be0616acf4b?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1509631179647-0177331693ae?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1581044777550-4cfa60707c03?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1469334031218-e382a71b716b?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1485968579580-b6d095142e6e?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1541101767792-f9b2b1c4f127?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1552374196-1ab2a1c593e8?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1562572159-4efd90cfa5a4?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1558769132-cb1aea458c5e?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1561043433-aaf687c4cf04?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1596993100471-c3905dafa78e?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1568252542512-9fe8fe9c87bb?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1594938298603-c8148c4b4068?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1579783902614-a3fb3927b6a5?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1605464315542-bda3e2f4e605?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1611042553365-9b101441c135?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1614624532983-4ce03382d63d?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1618375569909-3c8616cf7733?w=600&h=800&fit=crop',
  'https://images.unsplash.com/photo-1626686671969-c3b9e6e3a5e8?w=600&h=800&fit=crop',
];

async function main() {
  await initDatabase();
  const models = await query('SELECT id, name, photo_main, photos FROM models ORDER BY id');
  console.log(`Всего моделей: ${models.length}`);

  let updated = 0;
  for (const m of models) {
    let existing = [];
    try { existing = JSON.parse(m.photos || '[]'); } catch {}

    // Пропускаем если уже есть галерея
    if (existing.length >= 3) {
      console.log(`  ✓ ${m.name} — уже ${existing.length} фото`);
      continue;
    }

    // Выбираем 3 фото из пула, уникальных для каждой модели (по смещению от id)
    const offset = (m.id * 3) % PHOTO_POOL.length;
    const gallery = [];
    for (let i = 0; i < 3; i++) {
      const photo = PHOTO_POOL[(offset + i) % PHOTO_POOL.length];
      // Не дублируем photo_main
      if (photo !== m.photo_main) gallery.push(photo);
    }
    // Если photo_main не задан — используем первое из галереи как main
    if (!m.photo_main && gallery.length > 0) {
      await run('UPDATE models SET photo_main=? WHERE id=?', [gallery[0], m.id]);
    }

    await run('UPDATE models SET photos=? WHERE id=?', [JSON.stringify(gallery), m.id]);
    console.log(`  ✅ ${m.name} — добавлено ${gallery.length} фото`);
    updated++;
  }

  console.log(`\nГотово: обновлено ${updated} моделей`);
  process.exit(0);
}

main().catch(e => { console.error(e); process.exit(1); });
