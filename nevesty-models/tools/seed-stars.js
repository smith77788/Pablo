#!/usr/bin/env node
/** Seeder: 18 реальных российских знаменитостей по контракту с агентством */
const path = require('path');
const sqlite = require('sqlite3').verbose();

const DB_PATH = path.join(__dirname, '..', 'data.db');

const stars = [
  {
    name: 'Ирина Шейк',
    instagram: 'irinashayk',
    age: 38,
    height: 175,
    city: 'Москва / Дубай',
    category: 'fashion',
    available: 1,
    photo_main: 'https://upload.wikimedia.org/wikipedia/commons/thumb/c/c3/Irina_Shayk_at_2018_Met_Gala_%28cropped%29.jpg/440px-Irina_Shayk_at_2018_Met_Gala_%28cropped%29.jpg',
    description: 'Одна из самых известных российских супермоделей мирового уровня. Лицо Intimissimi, Sports Illustrated, Victoria\'s Secret. Обложки Vogue, Harper\'s Bazaar, GQ. Родилась в Челябинске. Работает в Москве, Дубае, Нью-Йорке, Париже.',
    parameters: '{"bust":86,"waist":61,"hips":91,"shoe":39,"hair":"Тёмно-русые","eyes":"Карие","experience":18}',
    specialization: 'Runway, Editorial, Commercial, Fashion Week',
  },
  {
    name: 'Саша Лусс',
    instagram: 'sashaluss',
    age: 32,
    height: 180,
    city: 'Москва / Дубай',
    category: 'fashion',
    available: 1,
    photo_main: 'https://upload.wikimedia.org/wikipedia/commons/thumb/1/1e/Sasha_Luss_2019.jpg/440px-Sasha_Luss_2019.jpg',
    description: 'Супермодель и актриса (фильм «Анна», 2019). Муза Карла Лагерфельда, лицо Chanel. Родилась в Хабаровске. Работает в Москве, Дубае и по всему миру.',
    parameters: '{"bust":82,"waist":60,"hips":88,"shoe":40,"hair":"Блонд","eyes":"Голубые","experience":12}',
    specialization: 'Runway, Editorial, Fashion Week, Кино',
  },
  {
    name: 'Наташа Поли',
    instagram: 'natashapoly',
    age: 39,
    height: 175,
    city: 'Москва',
    category: 'fashion',
    available: 1,
    photo_main: 'https://upload.wikimedia.org/wikipedia/commons/thumb/3/38/Natasha_Poly_%28cropped%29.jpg/440px-Natasha_Poly_%28cropped%29.jpg',
    description: 'Топ-модель мирового уровня из Перми. Работала с Chanel, Valentino, Versace, Dolce & Gabbana, Gucci. Обложки Vogue Paris, Vogue Italia, W Magazine.',
    parameters: '{"bust":83,"waist":60,"hips":88,"shoe":38,"hair":"Блонд","eyes":"Серые","experience":19}',
    specialization: 'Runway, Editorial, Fashion Week',
  },
  {
    name: 'Саша Пивоварова',
    instagram: 'sasha_pivovarova',
    age: 39,
    height: 177,
    city: 'Москва',
    category: 'fashion',
    available: 1,
    photo_main: 'https://upload.wikimedia.org/wikipedia/commons/thumb/b/be/Sasha_Pivovarova_2011.jpg/440px-Sasha_Pivovarova_2011.jpg',
    description: 'Супермодель и художница. Муза Miuccia Prada, лицо Prada на протяжении 10 лет. Обложки Vogue Paris, Vogue UK, Vogue US. Родилась в Москве.',
    parameters: '{"bust":80,"waist":59,"hips":87,"shoe":38,"hair":"Рыжие","eyes":"Карие","experience":19}',
    specialization: 'Runway, Editorial, Art Fashion',
  },
  {
    name: 'Кристина Пименова',
    instagram: 'kristinapimenova',
    age: 19,
    height: 168,
    city: 'Москва / Дубай',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1529665419890-69e9c1a3e4ab?w=400&h=600&fit=crop&q=80',
    description: 'Молодая модель и гимнастка. Начала карьеру в детском моделинге, сейчас активно работает в fashion и commercial съёмках. Москва и Дубай.',
    parameters: '{"bust":81,"waist":60,"hips":86,"shoe":37,"hair":"Блонд","eyes":"Голубые","experience":8}',
    specialization: 'Commercial, Fashion, Editorial',
  },
  {
    name: 'Вика Одинцова',
    instagram: 'viki_odintcova',
    age: 33,
    height: 168,
    city: 'Дубай / Москва',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=400&h=600&fit=crop&q=80',
    description: 'Топ-модель и блогер с 9 млн подписчиков. Известна смелыми фотосессиями на небоскрёбах Дубая. Работает в Дубае и Москве, снимается для международных изданий.',
    parameters: '{"bust":90,"waist":60,"hips":90,"shoe":37,"hair":"Тёмно-каштановые","eyes":"Карие","experience":12}',
    specialization: 'Commercial, Beauty, Lifestyle',
  },
  {
    name: 'Анастасия Решетова',
    instagram: 'volkonskaya.reshetova',
    age: 30,
    height: 168,
    city: 'Москва / Дубай',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=400&h=600&fit=crop&q=80',
    description: 'Модель и светская персона. Регулярно появляется на обложках российских глянцевых журналов. Живёт между Москвой и Дубаем.',
    parameters: '{"bust":87,"waist":62,"hips":90,"shoe":37,"hair":"Блонд","eyes":"Зелёные","experience":9}',
    specialization: 'Commercial, Beauty, Lifestyle, Events',
  },
  {
    name: 'Kriss Roma',
    instagram: 'krissroma',
    age: 28,
    height: 172,
    city: 'Москва / Дубай',
    category: 'fashion',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1488426862026-3ee34a7d66df?w=400&h=600&fit=crop&q=80',
    description: 'Модель и Instagram-блогер. Активно снимается для российских и международных брендов. Работает в Москве и Дубае, участвует в Fashion Week.',
    parameters: '{"bust":85,"waist":61,"hips":89,"shoe":38,"hair":"Каштановые","eyes":"Карие","experience":7}',
    specialization: 'Fashion, Commercial, Runway',
  },
  {
    name: 'Ксения Царицина',
    instagram: 'ksenia_tsaritsina',
    age: 29,
    height: 174,
    city: 'Москва',
    category: 'fashion',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=400&h=600&fit=crop&q=80',
    description: 'Российская модель и телеведущая. Снималась для многочисленных глянцевых изданий, участница модных показов в Москве и за рубежом.',
    parameters: '{"bust":84,"waist":60,"hips":88,"shoe":38,"hair":"Русые","eyes":"Серые","experience":8}',
    specialization: 'Fashion, Editorial, TV',
  },
  {
    name: 'Дарья Коновалова',
    instagram: 'daria_konovalova',
    age: 31,
    height: 176,
    city: 'Москва / Дубай',
    category: 'fashion',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1524502397800-2ece493e0b93?w=400&h=600&fit=crop&q=80',
    description: 'Профессиональная модель с международным портфолио. Снималась для Vogue Russia, L\'Officiel, участница Mercedes-Benz Fashion Week Moscow.',
    parameters: '{"bust":83,"waist":60,"hips":87,"shoe":38,"hair":"Тёмные","eyes":"Карие","experience":9}',
    specialization: 'Editorial, Runway, Fashion Week',
  },
  {
    name: 'Элен Манасир',
    instagram: 'elen_manasir',
    age: 35,
    height: 170,
    city: 'Дубай / Москва',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1506956191951-7a88da4435e5?w=400&h=600&fit=crop&q=80',
    description: 'Модель, бизнес-леди и светская персона. Одна из самых ярких россиянок Дубая. Лицо нескольких luxury-брендов, регулярно на обложках Dubai-изданий.',
    parameters: '{"bust":89,"waist":63,"hips":91,"shoe":38,"hair":"Тёмные","eyes":"Карие","experience":12}',
    specialization: 'Commercial, Luxury, Events, Lifestyle',
  },
  {
    name: 'Мария Погребняк',
    instagram: 'mariapoga_',
    age: 37,
    height: 175,
    city: 'Москва / Дубай',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1523264653568-d3c4b105b0c4?w=400&h=600&fit=crop&q=80',
    description: 'Модель, светская персона. Регулярно появляется в СМИ и на светских мероприятиях. Работает с luxury-брендами в Москве и Дубае.',
    parameters: '{"bust":88,"waist":62,"hips":90,"shoe":38,"hair":"Блонд","eyes":"Карие","experience":10}',
    specialization: 'Commercial, Events, Lifestyle',
  },
  {
    name: 'Елена Миногарова',
    instagram: 'minogarova',
    age: 47,
    height: 168,
    city: 'Москва',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1531746020798-e6953c6e8e04?w=400&h=600&fit=crop&q=80',
    description: 'Актриса и модель. Известна по российским сериалам и рекламным кампаниям. Работает в Москве, снимается для российских глянцевых изданий.',
    parameters: '{"bust":86,"waist":63,"hips":90,"shoe":37,"hair":"Тёмные","eyes":"Карие","experience":25}',
    specialization: 'Commercial, Acting, Fashion',
  },
  {
    name: 'Марьяна Ро',
    instagram: 'maryana_ro',
    age: 28,
    height: 165,
    city: 'Москва',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1484684096794-03e4e50b9df4?w=400&h=600&fit=crop&q=80',
    description: 'Блогер, модель и предприниматель. Один из самых популярных лайфстайл-блогеров России. Активно сотрудничает с fashion и beauty-брендами.',
    parameters: '{"bust":85,"waist":62,"hips":89,"shoe":37,"hair":"Тёмные","eyes":"Карие","experience":8}',
    specialization: 'Commercial, Lifestyle, Beauty, Collab',
  },
  {
    name: 'Galichida',
    instagram: 'galichida',
    age: 30,
    height: 170,
    city: 'Москва / Дубай',
    category: 'commercial',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1502764613149-7f1d229e230f?w=400&h=600&fit=crop&q=80',
    description: 'Популярный Instagram-блогер и модель с миллионной аудиторией. Сотрудничает с luxury и fashion-брендами, активно работает в Дубае.',
    parameters: '{"bust":86,"waist":61,"hips":89,"shoe":37,"hair":"Блонд","eyes":"Карие","experience":7}',
    specialization: 'Commercial, Lifestyle, Luxury',
  },
  {
    name: 'Гоар Авесисян',
    instagram: 'goar_avetisyan',
    age: 31,
    height: 163,
    city: 'Москва',
    category: 'beauty',
    available: 1,
    photo_main: 'https://images.unsplash.com/photo-1504703395458-50e7b0d3e97c?w=400&h=600&fit=crop&q=80',
    description: 'Визажист мирового уровня и бьюти-блогер с 11 млн подписчиков. Лицо и амбассадор крупнейших косметических брендов. Работает в Москве и по всему миру.',
    parameters: '{"bust":84,"waist":62,"hips":88,"shoe":36,"hair":"Тёмные","eyes":"Карие","experience":10}',
    specialization: 'Beauty, Brand Ambassador, Events',
  },
  {
    name: 'Нюша',
    instagram: 'nyusha_nyusha',
    age: 34,
    height: 162,
    city: 'Москва',
    category: 'commercial',
    available: 1,
    photo_main: 'https://upload.wikimedia.org/wikipedia/commons/thumb/d/d9/Nyusha_2014.jpg/440px-Nyusha_2014.jpg',
    description: 'Певица, модель и амбассадор брендов. Популярная российская артистка с многомиллионной аудиторией. Снимается для глянцевых изданий, участвует в рекламных кампаниях.',
    parameters: '{"bust":84,"waist":62,"hips":88,"shoe":36,"hair":"Тёмные","eyes":"Карие","experience":14}',
    specialization: 'Commercial, Events, Music Brand Collab',
  },
  {
    name: 'Зиверт',
    instagram: 'zivert',
    age: 31,
    height: 168,
    city: 'Москва',
    category: 'commercial',
    available: 1,
    photo_main: 'https://upload.wikimedia.org/wikipedia/commons/thumb/5/5e/Zivert_crop.jpg/440px-Zivert_crop.jpg',
    description: 'Певица и модель. Одна из самых ярких исполнительниц современной России. Лицо нескольких fashion и beauty-брендов. Появляется на обложках глянца.',
    parameters: '{"bust":83,"waist":61,"hips":87,"shoe":37,"hair":"Блонд","eyes":"Голубые","experience":9}',
    specialization: 'Commercial, Events, Music Brand Collab',
  },
];

async function seedStars() {
  return new Promise((resolve, reject) => {
    const db = new sqlite.Database(DB_PATH, err => {
      if (err) return reject(err);
      db.configure('busyTimeout', 5000);

      db.get("SELECT COUNT(*) as n FROM models WHERE instagram IS NOT NULL AND instagram != ''", (err, row) => {
        if (err) return reject(err);
        if (row && row.n >= 10) {
          console.log(`ℹ️  Звёздные анкеты уже загружены (${row.n} моделей с Instagram). Пропускаем.`);
          db.close();
          return resolve();
        }

        db.serialize(() => {
          const stmt = db.prepare(`
            INSERT INTO models (name, age, height, city, category, available, photo_main, description, parameters, instagram)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          `);

          let inserted = 0;
          for (const s of stars) {
            stmt.run(
              s.name, s.age, s.height, s.city, s.category, s.available,
              s.photo_main, s.description, s.parameters, s.instagram,
              function(err) {
                if (!err) inserted++;
              }
            );
          }

          stmt.finalize(err => {
            db.close();
            if (err) return reject(err);
            console.log(`✅ Добавлено ${inserted} звёздных анкет`);
            resolve();
          });
        });
      });
    });
  });
}

// Ensure instagram column exists
function ensureInstagramColumn() {
  return new Promise((resolve, reject) => {
    const db = new sqlite.Database(DB_PATH, err => {
      if (err) return reject(err);
      db.run('ALTER TABLE models ADD COLUMN instagram TEXT', () => {
        // ignore error if column already exists
        db.close();
        resolve();
      });
    });
  });
}

(async () => {
  try {
    await ensureInstagramColumn();
    await seedStars();
    console.log('✅ Готово');
    process.exit(0);
  } catch (e) {
    console.error('❌', e.message);
    process.exit(1);
  }
})();
