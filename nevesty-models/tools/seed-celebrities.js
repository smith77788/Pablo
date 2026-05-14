require('dotenv').config({ path: require('path').join(__dirname, '../.env') });
const sqlite3 = require('sqlite3').verbose();
const path = require('path');

const DB_PATH = path.join(__dirname, '../data.db');
const db = new sqlite3.Database(DB_PATH);

// 20 premium celebrity-tier model profiles (Moscow + Dubai international scene)
const celebrities = [
  {
    name: 'Валерия Золотарёва',
    age: 25,
    height: 180,
    weight: 56,
    bust: 90,
    waist: 60,
    hips: 90,
    shoe_size: '39',
    hair_color: 'Блонд',
    eye_color: 'Голубые',
    category: 'fashion',
    instagram: 'valeria.zolotareva',
    photo_main: 'https://images.unsplash.com/photo-1529665419890-69e9c1a3e4ab?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Chanel, Valentino Russia. Опыт: коммерческая, runway, editorial съёмка. 7 лет в индустрии. Лицо кампании Dior Beauty Russia 2023.',
    available: 1
  },
  {
    name: 'Диана Крылова',
    age: 22,
    height: 178,
    weight: 54,
    bust: 88,
    waist: 59,
    hips: 89,
    shoe_size: '38',
    hair_color: 'Брюнетка',
    eye_color: 'Карие',
    category: 'fashion',
    instagram: 'diana.krylova.model',
    photo_main: 'https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Ulyana Sergeenko, Viktor & Rolf. Опыт: коммерческая, runway, editorial съёмка. 4 года в профессии. Постоянный резидент Dubai Fashion Week.',
    available: 1
  },
  {
    name: 'Арина Белоусова',
    age: 24,
    height: 182,
    weight: 57,
    bust: 89,
    waist: 61,
    hips: 91,
    shoe_size: '40',
    hair_color: 'Шатен',
    eye_color: 'Зелёные',
    category: 'runway',
    instagram: 'arina.belousova',
    photo_main: 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Yanina Couture, Zuhair Murad. Опыт: коммерческая, runway, editorial съёмка. 6 лет карьеры. Работала в агентствах Милана и Дубая.',
    available: 1
  },
  {
    name: 'Кристина Астахова',
    age: 27,
    height: 176,
    weight: 55,
    bust: 91,
    waist: 62,
    hips: 92,
    shoe_size: '38',
    hair_color: 'Блонд',
    eye_color: 'Серые',
    category: 'commercial',
    instagram: 'kristina.astakhova',
    photo_main: 'https://images.unsplash.com/photo-1488426862026-3ee34a7d66df?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Vogue Russia, L\'Officiel. Опыт: коммерческая, runway, editorial съёмка. 8 лет в индустрии. Амбассадор Emirates luxury brands.',
    available: 1
  },
  {
    name: 'Николь Захарченко',
    age: 21,
    height: 175,
    weight: 53,
    bust: 87,
    waist: 59,
    hips: 89,
    shoe_size: '37',
    hair_color: 'Блонд',
    eye_color: 'Голубые',
    category: 'beauty',
    instagram: 'nicole.zakharchenko',
    photo_main: 'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов MAC Cosmetics, Lancome Russia. Опыт: коммерческая, runway, editorial съёмка. 3 года в beauty-индустрии. Победительница Elite Model Look Russia.',
    available: 1
  },
  {
    name: 'Алёна Черникова',
    age: 26,
    height: 179,
    weight: 56,
    bust: 90,
    waist: 60,
    hips: 90,
    shoe_size: '39',
    hair_color: 'Брюнетка',
    eye_color: 'Карие',
    category: 'editorial',
    instagram: 'alyona.chernikova',
    photo_main: 'https://images.unsplash.com/photo-1524502397800-2ece493e0b93?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Harper\'s Bazaar, Vogue Arabia. Опыт: коммерческая, runway, editorial съёмка. 7 лет портфолио. Регулярные контракты с Dubai-based luxury labels.',
    available: 1
  },
  {
    name: 'Элина Юсупова',
    age: 23,
    height: 177,
    weight: 54,
    bust: 88,
    waist: 60,
    hips: 90,
    shoe_size: '38',
    hair_color: 'Шатен',
    eye_color: 'Карие',
    category: 'fashion',
    instagram: 'elina.yusupova',
    photo_main: 'https://images.unsplash.com/photo-1506956191951-7a88da4435e5?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Alexander McQueen, Elie Saab. Опыт: коммерческая, runway, editorial съёмка. 5 лет карьеры. Востребована в Dubai Fashion Scene.',
    available: 1
  },
  {
    name: 'Камилла Ростовцева',
    age: 28,
    height: 183,
    weight: 58,
    bust: 92,
    waist: 63,
    hips: 93,
    shoe_size: '40',
    hair_color: 'Блонд',
    eye_color: 'Зелёные',
    category: 'runway',
    instagram: 'kamilla.rostovtseva',
    photo_main: 'https://images.unsplash.com/photo-1523264653568-d3c4b105b0c4?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Versace, Dolce & Gabbana Russia. Опыт: коммерческая, runway, editorial съёмка. 9 лет на подиуме. Самая высокая модель агентства, ikon runway.',
    available: 1
  },
  {
    name: 'Милана Тихонова',
    age: 20,
    height: 176,
    weight: 53,
    bust: 87,
    waist: 59,
    hips: 88,
    shoe_size: '38',
    hair_color: 'Рыжая',
    eye_color: 'Зелёные',
    category: 'commercial',
    instagram: 'milana.tikhonova',
    photo_main: 'https://images.unsplash.com/photo-1531746020798-e6953c6e8e04?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов ZARA Russia, H&M Campaign. Опыт: коммерческая, runway, editorial съёмка. 3 года в профессии. Яркая запоминающаяся внешность, звезда Instagram.',
    available: 1
  },
  {
    name: 'Соня Матвеева',
    age: 25,
    height: 178,
    weight: 55,
    bust: 89,
    waist: 61,
    hips: 91,
    shoe_size: '39',
    hair_color: 'Брюнетка',
    eye_color: 'Голубые',
    category: 'beauty',
    instagram: 'sonya.matveeva',
    photo_main: 'https://images.unsplash.com/photo-1484684096794-03e4e50b9df4?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Chanel Beauty, YSL Beauty Arabia. Опыт: коммерческая, runway, editorial съёмка. 6 лет в beauty. Лицо нескольких luxury fragrance кампаний.',
    available: 1
  },
  {
    name: 'Виктория Шаповалова',
    age: 24,
    height: 180,
    weight: 56,
    bust: 90,
    waist: 60,
    hips: 90,
    shoe_size: '39',
    hair_color: 'Шатен',
    eye_color: 'Карие',
    category: 'editorial',
    instagram: 'vika.shapovalova',
    photo_main: 'https://images.unsplash.com/photo-1502764613149-7f1d229e230f?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Vogue Russia, GQ Style. Опыт: коммерческая, runway, editorial съёмка. 5 лет в editorial. Работает с топовыми фотографами Москвы и Дубая.',
    available: 1
  },
  {
    name: 'Надя Ковалёва',
    age: 22,
    height: 175,
    weight: 53,
    bust: 88,
    waist: 60,
    hips: 89,
    shoe_size: '37',
    hair_color: 'Блонд',
    eye_color: 'Серые',
    category: 'fashion',
    instagram: 'nadya.kovaleva',
    photo_main: 'https://images.unsplash.com/photo-1504703395458-50e7b0d3e97c?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Boss, Escada Russia. Опыт: коммерческая, runway, editorial съёмка. 4 года опыта. Частый гость показов Дубайской недели моды.',
    available: 1
  },
  {
    name: 'Карина Жукова',
    age: 29,
    height: 177,
    weight: 57,
    bust: 91,
    waist: 62,
    hips: 92,
    shoe_size: '38',
    hair_color: 'Рыжая',
    eye_color: 'Карие',
    category: 'commercial',
    instagram: 'karina.zhukova',
    photo_main: 'https://images.unsplash.com/photo-1560087639-cf5f69a2f6c3?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Max Factor, L\'Oreal Russia. Опыт: коммерческая, runway, editorial съёмка. 10 лет в индустрии. Один из самых опытных кадров агентства.',
    available: 1
  },
  {
    name: 'Ева Прохорова',
    age: 21,
    height: 176,
    weight: 54,
    bust: 88,
    waist: 59,
    hips: 90,
    shoe_size: '38',
    hair_color: 'Брюнетка',
    eye_color: 'Зелёные',
    category: 'runway',
    instagram: 'eva.prokhorova',
    photo_main: 'https://images.unsplash.com/photo-1557053910-d9eadeed1c58?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Givenchy, Balenciaga Russia. Опыт: коммерческая, runway, editorial съёмка. 3 года подиумного опыта. Стремительно растущая карьера.',
    available: 1
  },
  {
    name: 'Таисия Фёдорова',
    age: 26,
    height: 181,
    weight: 57,
    bust: 90,
    waist: 61,
    hips: 91,
    shoe_size: '40',
    hair_color: 'Блонд',
    eye_color: 'Голубые',
    category: 'fashion',
    instagram: 'taisia.fedorova',
    photo_main: 'https://images.unsplash.com/photo-1531123897727-d0d34bfafc51?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Gucci, Prada Italia. Опыт: коммерческая, runway, editorial съёмка. 7 лет карьеры. Сотрудничает с ведущими буккинг-агентствами ОАЭ.',
    available: 1
  },
  {
    name: 'Зоя Смирнова',
    age: 23,
    height: 178,
    weight: 55,
    bust: 89,
    waist: 60,
    hips: 90,
    shoe_size: '38',
    hair_color: 'Шатен',
    eye_color: 'Серые',
    category: 'beauty',
    instagram: 'zoya.smirnova.model',
    photo_main: 'https://images.unsplash.com/photo-1469334031218-e382a71b716b?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Estée Lauder, Armani Beauty. Опыт: коммерческая, runway, editorial съёмка. 5 лет в beauty-сегменте. Любимица арабских роскошных брендов.',
    available: 1
  },
  {
    name: 'Полина Дроздова',
    age: 27,
    height: 179,
    weight: 56,
    bust: 91,
    waist: 62,
    hips: 92,
    shoe_size: '39',
    hair_color: 'Брюнетка',
    eye_color: 'Карие',
    category: 'editorial',
    instagram: 'polina.drozdova',
    photo_main: 'https://images.unsplash.com/photo-1544005313-b2c0f26d4f40?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Elle Russia, Numéro Moscow. Опыт: коммерческая, runway, editorial съёмка. 8 лет в editorial. Сотрудничает с именитыми fashion-фотографами.',
    available: 1
  },
  {
    name: 'Лера Баранова',
    age: 20,
    height: 175,
    weight: 52,
    bust: 87,
    waist: 59,
    hips: 88,
    shoe_size: '37',
    hair_color: 'Блонд',
    eye_color: 'Голубые',
    category: 'commercial',
    instagram: 'lera.baranova',
    photo_main: 'https://images.unsplash.com/photo-1508214751196-bcfd4ca60f91?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Mango, Massimo Dutti Russia. Опыт: коммерческая, runway, editorial съёмка. 3 года в коммерческой съёмке. Восходящая звезда модельного бизнеса.',
    available: 1
  },
  {
    name: 'Ника Головина',
    age: 30,
    height: 177,
    weight: 56,
    bust: 90,
    waist: 61,
    hips: 91,
    shoe_size: '38',
    hair_color: 'Рыжая',
    eye_color: 'Зелёные',
    category: 'fashion',
    instagram: 'nika.golovina',
    photo_main: 'https://images.unsplash.com/photo-1524253482453-3012f9e8075a?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Comme des Garçons, Ann Demeulemeester. Опыт: коммерческая, runway, editorial съёмка. 10 лет в индустрии. Икона авангардной моды.',
    available: 1
  },
  {
    name: 'Анжела Курбатова',
    age: 24,
    height: 178,
    weight: 55,
    bust: 89,
    waist: 61,
    hips: 90,
    shoe_size: '38',
    hair_color: 'Шатен',
    eye_color: 'Карие',
    category: 'runway',
    instagram: 'angela.kurbatova',
    photo_main: 'https://images.unsplash.com/photo-1541516160671-478db32a2a3c?w=400&h=600&fit=crop&q=80',
    bio: 'Топ-модель с международным опытом. Работает в Москве и Дубае. Участница показов Saint Laurent, Celine Russia. Опыт: коммерческая, runway, editorial съёмка. 5 лет подиумного опыта. Постоянный контракт с Dubai luxury agency.',
    available: 1
  },
];

db.serialize(() => {
  // Check if celebrity profiles already seeded — look for "Дубай" in bio
  db.get("SELECT COUNT(*) as n FROM models WHERE bio LIKE '%Дубай%'", (err, row) => {
    if (err) { console.error(err); db.close(); return; }

    if (row.n >= 10) {
      console.log(`✅ Celebrity-анкеты уже добавлены (${row.n} анкет с Дубай). Пропускаем.`);
      db.close();
      return;
    }

    const stmt = db.prepare(`
      INSERT INTO models
        (name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color,
         category, instagram, photo_main, bio, available, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    `);

    celebrities.forEach(m => {
      stmt.run(
        m.name, m.age, m.height, m.weight,
        m.bust, m.waist, m.hips, m.shoe_size,
        m.hair_color, m.eye_color,
        m.category, m.instagram, m.photo_main,
        m.bio, m.available
      );
    });

    stmt.finalize(() => {
      console.log(`✅ Добавлено ${celebrities.length} celebrity-tier анкет (Москва + Дубай)`);
      db.close();
    });
  });
});
