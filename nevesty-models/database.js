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

  // Performance pragmas — set before any table operations
  await run('PRAGMA journal_mode = WAL');
  await run('PRAGMA synchronous = NORMAL');
  await run('PRAGMA cache_size = 2000');
  await run('PRAGMA temp_store = MEMORY');
  await run('PRAGMA mmap_size = 67108864'); // 64 MB memory-mapped I/O

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

  // Agent communication blackboard
  await run(`CREATE TABLE IF NOT EXISTS agent_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    file TEXT,
    line INTEGER,
    auto_fixable INTEGER DEFAULT 0,
    proposed_fix TEXT,
    status TEXT DEFAULT 'open',
    claimed_by TEXT,
    claimed_at DATETIME,
    fixed_by TEXT,
    fix_summary TEXT,
    fixed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS agent_discussions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT DEFAULT 'all',
    topic TEXT NOT NULL,
    message TEXT NOT NULL,
    ref_finding_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    text TEXT NOT NULL,
    model_id INTEGER,
    approved INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  await run(`CREATE TABLE IF NOT EXISTS order_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    admin_note TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(order_id) REFERENCES orders(id)
  )`);

  await run(`CREATE TABLE IF NOT EXISTS order_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_by TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(order_id) REFERENCES orders(id)
  )`);

  // Migrations — add status column to reviews if missing
  await run(`ALTER TABLE reviews ADD COLUMN status TEXT DEFAULT 'pending'`).catch(() => {});

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

  // Favorites table — wishlist for Telegram bot users
  await run(`CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    model_id INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, model_id),
    FOREIGN KEY(model_id) REFERENCES models(id) ON DELETE CASCADE
  )`);
  await run(`CREATE INDEX IF NOT EXISTS idx_favorites_chat ON favorites(chat_id)`);

  // Quick bookings table — name + phone only, manager fills the rest
  await run(`CREATE TABLE IF NOT EXISTS quick_bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name TEXT NOT NULL,
    client_phone TEXT NOT NULL,
    chat_id TEXT,
    status TEXT DEFAULT 'new',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);

  // Migrations — add columns that may not exist in older DBs
  await run(`ALTER TABLE models ADD COLUMN city TEXT`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN featured INTEGER DEFAULT 0`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN phone TEXT`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN order_count INTEGER DEFAULT 0`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN view_count INTEGER DEFAULT 0`).catch(() => {});

  // Indexes for frequent queries
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_model_id ON orders(model_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_client_chat ON orders(client_chat_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_messages_order ON messages(order_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_category ON models(category)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_available ON models(available)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_featured ON models(featured DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_sessions_updated ON telegram_sessions(updated_at)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_findings_status ON agent_findings(status)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_findings_created ON agent_findings(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_discussions_created ON agent_discussions(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_logs_created ON agent_logs(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_order_status_history_order ON order_status_history(order_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_order_status_history_created ON order_status_history(created_at DESC)`);

  // Seed admin if not exists
  const admin = await get('SELECT id FROM admins WHERE username = ?', [process.env.ADMIN_USERNAME || 'admin']);
  if (!admin) {
    const hash = await bcrypt.hash(process.env.ADMIN_PASSWORD || 'admin123', 10);
    await run(
      'INSERT INTO admins (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
      [process.env.ADMIN_USERNAME || 'admin', process.env.AGENCY_EMAIL || 'admin@nevesty-models.ru', hash, 'superadmin']
    );
    console.log('Default admin account created. Set ADMIN_USERNAME and ADMIN_PASSWORD in .env');
  }

  // Seed demo models if empty
  const count = await get('SELECT COUNT(*) as n FROM models');
  if (count.n === 0) {
    await seedDemoModels();
  }

  // Seed sample reviews if empty
  const reviewCount = await get('SELECT COUNT(*) as n FROM reviews');
  if (reviewCount.n === 0) {
    await seedSampleReviews();
  }

  console.log('Database initialized');
}

async function seedDemoModels() {
  const models = [
    { name: 'Анастасия Белова', age: 22, height: 178, weight: 55, bust: 86, waist: 61, hips: 88, shoe_size: '38', hair_color: 'Блонд', eye_color: 'Голубые', city: 'Москва', bio: 'Профессиональная модель с опытом участия в показах ведущих дизайнеров. Специализируется на fashion и editorial съёмках.', category: 'fashion', instagram: '@anastasia_models' },
    { name: 'Виктория Нова', age: 24, height: 175, weight: 53, bust: 84, waist: 60, hips: 86, shoe_size: '37', hair_color: 'Шатен', eye_color: 'Карие', city: 'Санкт-Петербург', bio: 'Универсальная модель для коммерческих и fashion проектов. Работала с крупнейшими брендами России и Европы.', category: 'commercial', instagram: '@victoria_nova_model' },
    { name: 'Дарья Светлова', age: 20, height: 180, weight: 57, bust: 88, waist: 63, hips: 90, shoe_size: '39', hair_color: 'Рыжая', eye_color: 'Зелёные', city: 'Москва', bio: 'Начинающая модель с ярким имиджем. Идеально подходит для avant-garde и editorial проектов.', category: 'fashion', instagram: '@dasha_models' },
    { name: 'Екатерина Морозова', age: 26, height: 172, weight: 54, bust: 85, waist: 62, hips: 87, shoe_size: '38', hair_color: 'Брюнетка', eye_color: 'Серые', city: 'Казань', bio: 'Опытная модель для корпоративных мероприятий, рекламных кампаний и роскошных событий.', category: 'events', instagram: '@kate_morozova_' },
    { name: 'Полина Золотарёва', age: 23, height: 176, weight: 56, bust: 87, waist: 61, hips: 89, shoe_size: '38', hair_color: 'Блонд', eye_color: 'Голубые', city: 'Санкт-Петербург', bio: 'Fashion и lifestyle модель. Специализация: luxury brands, jewelry, beauty campaigns.', category: 'fashion', instagram: '@polina_models' },
    { name: 'Алина Лебедева', age: 21, height: 174, weight: 52, bust: 83, waist: 59, hips: 85, shoe_size: '37', hair_color: 'Тёмный блонд', eye_color: 'Зелёные', city: 'Екатеринбург', bio: 'Танцовщица и модель. Идеально для динамичных fashion-show и event-проектов.', category: 'events', instagram: '@alina_lebedeva_m' },
  ];

  for (const m of models) {
    await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,city,bio,category,instagram,available)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)`,
      [m.name, m.age, m.height, m.weight, m.bust, m.waist, m.hips, m.shoe_size, m.hair_color, m.eye_color, m.city, m.bio, m.category, m.instagram]
    );
  }
  console.log('Demo models seeded');
}

async function seedSampleReviews() {
  const reviews = [
    { client_name: 'Михаил Орлов',    rating: 5, text: 'Потрясающая работа агентства! Модели были профессиональны и пунктуальны. Мероприятие прошло на высшем уровне.',           model_id: null, approved: 1 },
    { client_name: 'Светлана Иванова', rating: 5, text: 'Заказывали фотосессию для рекламной кампании. Результат превзошёл все ожидания. Обязательно обратимся снова.',          model_id: null, approved: 1 },
    { client_name: 'Дмитрий Ковалёв', rating: 4, text: 'Очень хорошее агентство, широкий выбор моделей. Небольшая задержка при согласовании, но итогом довольны.',             model_id: null, approved: 1 },
    { client_name: 'Анна Петрова',    rating: 5, text: 'Работали с агентством на корпоративном мероприятии. Модели отлично справились с ролью хостес. Рекомендуем!',           model_id: null, approved: 1 },
    { client_name: 'Роман Смирнов',   rating: 5, text: 'Профессиональный подход на всех этапах: от подбора модели до самого мероприятия. Отличная команда, спасибо!',           model_id: null, approved: 0 },
  ];
  for (const r of reviews) {
    await run(
      'INSERT OR IGNORE INTO reviews (client_name, rating, text, model_id, approved) VALUES (?,?,?,?,?)',
      [r.client_name, r.rating, r.text, r.model_id, r.approved]
    );
  }
  console.log('Sample reviews seeded');
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
