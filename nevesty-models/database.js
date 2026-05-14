const sqlite3 = require('sqlite3').verbose();
const path = require('path');
const bcrypt = require('bcryptjs');
const crypto = require('crypto');

const DB_PATH = path.join(__dirname, 'data.db');
let db;

function query(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.all(sql, params, (err, rows) => {
      if (err) reject(err);
      else resolve(rows);
    });
  });
}

function run(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.run(sql, params, function (err) {
      if (err) reject(err);
      else resolve({ id: this.lastID, changes: this.changes });
    });
  });
}

function get(sql, params = []) {
  return new Promise((resolve, reject) => {
    db.get(sql, params, (err, row) => {
      if (err) reject(err);
      else resolve(row);
    });
  });
}

async function initDatabase() {
  db = new sqlite3.Database(DB_PATH);

  await run(`CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT,
    password_hash TEXT NOT NULL,
    telegram_id TEXT,
    role TEXT DEFAULT 'manager',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    age INTEGER,
    height INTEGER,
    weight INTEGER,
    bust INTEGER,
    waist INTEGER,
    hips INTEGER,
    shoe_size TEXT,
    hair_color TEXT,
    eye_color TEXT,
    bio TEXT,
    photo_main TEXT,
    photos TEXT DEFAULT '[]',
    instagram TEXT,
    category TEXT DEFAULT 'fashion',
    available INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT UNIQUE NOT NULL,
    client_name TEXT NOT NULL,
    client_phone TEXT NOT NULL,
    client_email TEXT,
    client_telegram TEXT,
    client_chat_id TEXT,
    model_id INTEGER,
    event_type TEXT NOT NULL,
    event_date TEXT,
    event_duration INTEGER DEFAULT 4,
    location TEXT,
    budget TEXT,
    comments TEXT,
    status TEXT DEFAULT 'new',
    admin_notes TEXT,
    manager_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(model_id) REFERENCES models(id),
    FOREIGN KEY(manager_id) REFERENCES admins(id)
  )`);

  await run(`CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    sender_type TEXT NOT NULL,
    sender_name TEXT,
    content TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(order_id) REFERENCES orders(id)
  )`);

  await run(`CREATE TABLE IF NOT EXISTS telegram_sessions (
    chat_id TEXT PRIMARY KEY,
    state TEXT DEFAULT 'idle',
    order_id INTEGER,
    data TEXT DEFAULT '{}',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_name TEXT,
    message TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  // Default settings
  const defaults = [
    ['greeting',       'Добро пожаловать в Nevesty Models — агентство профессиональных моделей!'],
    ['about',          'Мы работаем с 2018 года. Более 200 моделей в базе. Fashion, Commercial, Events.'],
    ['contacts_phone', '+7 (900) 000-00-00'],
    ['contacts_email', 'info@nevesty-models.ru'],
    ['contacts_insta', '@nevesty_models'],
    ['contacts_addr',  'Москва, ул. Пресненская, 8'],
    ['pricing',        'Fashion/Commercial — от 5000₽/час\nEvents — от 8000₽/час\nRunway — от 10000₽/час'],
    ['notif_new_order', '1'],
    ['notif_status',    '1'],
    ['notif_message',   '1'],
  ];
  for (const [key, value] of defaults) {
    await run('INSERT OR IGNORE INTO bot_settings (key,value) VALUES (?,?)', [key, value]);
  }

  // Indexes for frequent queries
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_model_id ON orders(model_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_client_chat ON orders(client_chat_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_messages_order ON messages(order_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_category ON models(category)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_available ON models(available)`);

  // Seed admin if not exists
  const admin = await get('SELECT id FROM admins WHERE username = ?', [process.env.ADMIN_USERNAME || 'admin']);
  if (!admin) {
    const hash = await bcrypt.hash(process.env.ADMIN_PASSWORD || 'admin123', 10);
    await run(
      'INSERT INTO admins (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
      [process.env.ADMIN_USERNAME || 'admin', process.env.AGENCY_EMAIL || 'admin@nevesty-models.ru', hash, 'superadmin']
    );
    console.log('Admin created: admin / admin123');
  }

  // Seed demo models if empty
  const count = await get('SELECT COUNT(*) as n FROM models');
  if (count.n === 0) {
    await seedDemoModels();
  }

  console.log('Database initialized');
}

async function seedDemoModels() {
  const models = [
    { name: 'Анастасия Белова', age: 22, height: 178, weight: 55, bust: 86, waist: 61, hips: 88, shoe_size: '38', hair_color: 'Блонд', eye_color: 'Голубые', bio: 'Профессиональная модель с опытом участия в показах ведущих дизайнеров. Специализируется на fashion и editorial съёмках.', category: 'fashion', instagram: '@anastasia_models' },
    { name: 'Виктория Нова', age: 24, height: 175, weight: 53, bust: 84, waist: 60, hips: 86, shoe_size: '37', hair_color: 'Шатен', eye_color: 'Карие', bio: 'Универсальная модель для коммерческих и fashion проектов. Работала с крупнейшими брендами России и Европы.', category: 'commercial', instagram: '@victoria_nova_model' },
    { name: 'Дарья Светлова', age: 20, height: 180, weight: 57, bust: 88, waist: 63, hips: 90, shoe_size: '39', hair_color: 'Рыжая', eye_color: 'Зелёные', bio: 'Начинающая модель с ярким имиджем. Идеально подходит для avant-garde и editorial проектов.', category: 'fashion', instagram: '@dasha_models' },
    { name: 'Екатерина Морозова', age: 26, height: 172, weight: 54, bust: 85, waist: 62, hips: 87, shoe_size: '38', hair_color: 'Брюнетка', eye_color: 'Серые', bio: 'Опытная модель для корпоративных мероприятий, рекламных кампаний и роскошных событий.', category: 'events', instagram: '@kate_morozova_' },
    { name: 'Полина Золотарёва', age: 23, height: 176, weight: 56, bust: 87, waist: 61, hips: 89, shoe_size: '38', hair_color: 'Блонд', eye_color: 'Голубые', bio: 'Fashion и lifestyle модель. Специализация: luxury brands, jewelry, beauty campaigns.', category: 'fashion', instagram: '@polina_models' },
    { name: 'Алина Лебедева', age: 21, height: 174, weight: 52, bust: 83, waist: 59, hips: 85, shoe_size: '37', hair_color: 'Тёмный блонд', eye_color: 'Зелёные', bio: 'Танцовщица и модель. Идеально для динамичных fashion-show и event-проектов.', category: 'events', instagram: '@alina_lebedeva_m' },
  ];

  for (const m of models) {
    await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,category,instagram,available)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)`,
      [m.name, m.age, m.height, m.weight, m.bust, m.waist, m.hips, m.shoe_size, m.hair_color, m.eye_color, m.bio, m.category, m.instagram]
    );
  }
  console.log('Demo models seeded');
}

function generateOrderNumber() {
  const year = new Date().getFullYear();
  return `NM-${year}-${crypto.randomBytes(3).toString('hex').toUpperCase()}`;
}

function closeDatabase() {
  return new Promise(resolve => {
    if (db) db.close(err => { if (err) console.error('DB close error:', err.message); resolve(); });
    else resolve();
  });
}

module.exports = { initDatabase, query, run, get, generateOrderNumber, closeDatabase };
