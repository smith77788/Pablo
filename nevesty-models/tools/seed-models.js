require('dotenv').config({ path: require('path').join(__dirname, '../.env') });
const sqlite3 = require('sqlite3').verbose();
const path = require('path');

const DB_PATH = path.join(__dirname, '../data.db');
const db = new sqlite3.Database(DB_PATH);

const models = [
  {
    name: 'Анастасия Воронова', age: 23, height: 178, weight: 55,
    bust: 84, waist: 62, hips: 90, shoe_size: '38',
    hair_color: 'Блонд', eye_color: 'Голубые',
    category: 'fashion', instagram: 'anastasia.voronova',
    bio: 'Профессиональная fashion-модель с опытом работы на подиуме 5 лет. Участница недели моды в Москве и Санкт-Петербурге. Специализируется на haute couture и editorial съёмках.',
    available: 1
  },
  {
    name: 'Екатерина Соболева', age: 21, height: 175, weight: 53,
    bust: 82, waist: 60, hips: 88, shoe_size: '37',
    hair_color: 'Брюнетка', eye_color: 'Карие',
    category: 'commercial', instagram: 'kate.soboleva',
    bio: 'Коммерческая модель с широкой специализацией — от рекламы банков до fashion-каталогов. Работала с крупнейшими российскими брендами. Натуральная красота, выразительный взгляд.',
    available: 1
  },
  {
    name: 'Виктория Лебедева', age: 25, height: 180, weight: 57,
    bust: 86, waist: 63, hips: 91, shoe_size: '39',
    hair_color: 'Шатен', eye_color: 'Зелёные',
    category: 'fashion', instagram: 'vika.lebedeva',
    bio: 'Высокая статная модель, специализирующаяся на показах haute couture. Победительница регионального конкурса красоты. Опыт в Париже и Милане на стажировке.',
    available: 1
  },
  {
    name: 'Мария Захарова', age: 22, height: 172, weight: 52,
    bust: 83, waist: 61, hips: 89, shoe_size: '37',
    hair_color: 'Рыжая', eye_color: 'Зелёные',
    category: 'events', instagram: 'masha.zakharova',
    bio: 'Энергичная промо-модель для мероприятий любого формата. Яркая внешность, открытая улыбка, безупречный имидж. Опыт на корпоративах, выставках и светских вечеринках.',
    available: 1
  },
  {
    name: 'Полина Сергеева', age: 20, height: 176, weight: 54,
    bust: 83, waist: 61, hips: 89, shoe_size: '38',
    hair_color: 'Блонд', eye_color: 'Серые',
    category: 'commercial', instagram: 'polina.sergeeva',
    bio: 'Молодая перспективная модель с природной фотогеничностью. Работает в рекламе косметики, одежды и lifestyle-брендов. Быстро обучается, легко работает с командой.',
    available: 1
  },
  {
    name: 'Дарья Морозова', age: 26, height: 174, weight: 56,
    bust: 85, waist: 62, hips: 90, shoe_size: '38',
    hair_color: 'Блонд', eye_color: 'Голубые',
    category: 'fashion', instagram: 'dasha.morozova',
    bio: 'Опытная fashion-модель с портфолио в ведущих российских журналах: Vogue Russia, Harper\'s Bazaar, L\'Officiel. Работает с топовыми фотографами страны.',
    available: 1
  },
  {
    name: 'Алина Петрова', age: 24, height: 169, weight: 50,
    bust: 81, waist: 59, hips: 87, shoe_size: '36',
    hair_color: 'Брюнетка', eye_color: 'Карие',
    category: 'commercial', instagram: 'alina.petrova',
    bio: 'Универсальная модель для рекламных съёмок. Работала в рекламных кампаниях крупных ритейлеров, банков и застройщиков. Естественная, обаятельная, профессиональная.',
    available: 1
  },
  {
    name: 'Ксения Блинова', age: 23, height: 177, weight: 55,
    bust: 84, waist: 61, hips: 89, shoe_size: '38',
    hair_color: 'Шатен', eye_color: 'Серые',
    category: 'events', instagram: 'ksenya.blinova',
    bio: 'Хостес и event-модель премиум класса. Безупречные манеры, уверенная осанка, свободный английский. Идеальна для статусных корпоративов и деловых мероприятий.',
    available: 1
  },
  {
    name: 'Юлия Романова', age: 27, height: 181, weight: 58,
    bust: 86, waist: 64, hips: 92, shoe_size: '40',
    hair_color: 'Блонд', eye_color: 'Голубые',
    category: 'fashion', instagram: 'julia.romanova',
    bio: 'Опытнейшая подиумная модель. Более 50 показов мод в Москве, Санкт-Петербурге, Лондоне. Работала с Alexander McQueen Russia, Ulyana Sergeenko, Yanina Couture.',
    available: 0
  },
  {
    name: 'Наталья Горбунова', age: 22, height: 173, weight: 53,
    bust: 83, waist: 60, hips: 88, shoe_size: '37',
    hair_color: 'Рыжая', eye_color: 'Карие',
    category: 'commercial', instagram: 'natasha.gorbunova',
    bio: 'Яркая рыжеволосая модель — настоящая «изюминка» для рекламных кампаний. Запоминающаяся внешность делает её идеальной для брендов, которые хотят выделиться.',
    available: 1
  },
  {
    name: 'Ирина Волкова', age: 28, height: 170, weight: 52,
    bust: 82, waist: 60, hips: 87, shoe_size: '37',
    hair_color: 'Брюнетка', eye_color: 'Зелёные',
    category: 'events', instagram: 'irina.volkova.model',
    bio: 'Зрелая, уверенная модель с многолетним опытом работы на мероприятиях. Прекрасно держится перед камерой и публикой. Работала на открытиях ресторанов, галерей и fashion-шоурумов.',
    available: 1
  },
  {
    name: 'Тамара Орлова', age: 21, height: 175, weight: 54,
    bust: 84, waist: 62, hips: 90, shoe_size: '38',
    hair_color: 'Шатен', eye_color: 'Зелёные',
    category: 'fashion', instagram: 'tamara.orlova',
    bio: 'Восходящая звезда российской fashion-индустрии. Нестандартный взгляд, скуластое лицо — идеал для авангардных съёмок и дизайнерских коллекций.',
    available: 1
  },
  {
    name: 'Светлана Кузнецова', age: 24, height: 168, weight: 51,
    bust: 82, waist: 60, hips: 87, shoe_size: '36',
    hair_color: 'Блонд', eye_color: 'Серые',
    category: 'commercial', instagram: 'sveta.kuznetsova',
    bio: 'Рекламная модель с «соседской» красотой — натуральной и доступной. Идеально подходит для брендов, ориентированных на массовую аудиторию. Работала с DNS, Ростелеком, Магнит.',
    available: 1
  },
  {
    name: 'Ольга Миронова', age: 25, height: 176, weight: 55,
    bust: 85, waist: 62, hips: 90, shoe_size: '38',
    hair_color: 'Брюнетка', eye_color: 'Голубые',
    category: 'events', instagram: 'olga.mironova',
    bio: 'Модель и промоутер с опытом работы на международных выставках (Иннопром, ПМЭФ, AutoSalon). Владеет английским и немецким языками. Профессиональная ведущая мероприятий.',
    available: 1
  },
  {
    name: 'Александра Новикова', age: 19, height: 179, weight: 55,
    bust: 83, waist: 61, hips: 89, shoe_size: '39',
    hair_color: 'Блонд', eye_color: 'Карие',
    category: 'fashion', instagram: 'sasha.novikova',
    bio: 'Юная перспективная модель, только начинающая карьеру, но уже с несколькими показами в портфолио. Высокая, стройная, с природной элегантностью движений.',
    available: 1
  },
];

db.serialize(() => {
  // Проверяем сколько уже есть
  db.get('SELECT COUNT(*) as n FROM models', (err, row) => {
    if (err) { console.error(err); return; }
    if (row.n > 0) {
      console.log(`Уже есть ${row.n} моделей, пропускаем seed`);
      db.close();
      return;
    }
    const stmt = db.prepare(`
      INSERT INTO models (name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color, category, instagram, bio, available, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    `);
    models.forEach(m => {
      stmt.run(m.name, m.age, m.height, m.weight, m.bust, m.waist, m.hips, m.shoe_size, m.hair_color, m.eye_color, m.category, m.instagram, m.bio, m.available);
    });
    stmt.finalize(() => {
      console.log(`✅ Добавлено ${models.length} моделей в базу`);
      db.close();
    });
  });
});
