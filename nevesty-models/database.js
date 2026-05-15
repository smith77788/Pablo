const sqlite3 = require('sqlite3').verbose();
const path = require('path');
const bcrypt = require('bcryptjs');
const crypto = require('crypto');
const { cache, TTL_SETTINGS } = require('./services/cache');

const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'data.db');
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
  db = new sqlite3.Database(process.env.DB_PATH || DB_PATH);

  // Performance pragmas — set before any table operations
  await run('PRAGMA journal_mode=WAL');
  await run('PRAGMA synchronous=NORMAL'); // Faster than FULL, still safe with WAL
  await run('PRAGMA cache_size=-32000'); // 32MB page cache
  await run('PRAGMA temp_store=MEMORY');
  await run('PRAGMA mmap_size=268435456'); // 256MB mmap
  await run('PRAGMA optimize'); // Optimize query planner

  // Schema version tracking
  await run(`CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(()=>{});

  // Record current schema version if not exists
  const schemaVer = await get('SELECT MAX(version) as v FROM schema_versions').catch(()=>null);
  if (!schemaVer?.v) {
    const migrations = [
      [1, 'Initial schema — models, orders, admins, settings'],
      [2, 'Add reviews table'],
      [3, 'Add favorites, order_notes, factory_tasks'],
      [4, 'Add wishlists, model fields (city, phone, featured, view_count, archived)'],
      [5, 'Add loyalty_points, loyalty_transactions, client_prefs'],
      [6, 'Add referrals, blocked_clients, ab_experiments, audit_log'],
      [7, 'Add UTM columns to orders, quick_bookings'],
      [8, 'Add achievements table'],
    ];
    for (const [v, desc] of migrations) {
      await run('INSERT OR IGNORE INTO schema_versions (version, description) VALUES (?,?)', [v, desc]).catch(()=>{});
    }
  }

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

  // Factory tasks — synced from AI Factory CEO growth_actions
  await run(`CREATE TABLE IF NOT EXISTS factory_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    priority INTEGER DEFAULT 5,
    department TEXT,
    expected_impact TEXT,
    status TEXT DEFAULT 'pending',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`);
  await run(`CREATE INDEX IF NOT EXISTS idx_factory_tasks_status ON factory_tasks(status)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_factory_tasks_priority ON factory_tasks(priority DESC, created_at DESC)`);

  // Loyalty points system
  await run(`CREATE TABLE IF NOT EXISTS loyalty_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE,
    points INTEGER DEFAULT 0,
    total_earned INTEGER DEFAULT 0,
    level TEXT DEFAULT 'bronze',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(()=>{});

  await run(`CREATE TABLE IF NOT EXISTS loyalty_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    type TEXT NOT NULL,
    description TEXT,
    order_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(()=>{});

  await run(`CREATE INDEX IF NOT EXISTS idx_loyalty_chat ON loyalty_points(chat_id)`).catch(()=>{});

  // Referral program
  await run(`CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_chat_id INTEGER NOT NULL,
    referred_chat_id INTEGER NOT NULL,
    bonus_points INTEGER DEFAULT 50,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(()=>{});
  await run(`CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_chat_id)`).catch(()=>{});

  // Achievements system
  await run(`CREATE TABLE IF NOT EXISTS achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    achievement_key TEXT NOT NULL,
    achieved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, achievement_key)
  )`).catch(()=>{});
  await run(`CREATE INDEX IF NOT EXISTS idx_achievements_chat ON achievements(chat_id)`).catch(()=>{});

  // Migrations — add status column to reviews if missing
  await run(`ALTER TABLE reviews ADD COLUMN status TEXT DEFAULT 'pending'`).catch(() => {});

  // Migration — add order_id to reviews for follow-up tracking
  await run(`ALTER TABLE reviews ADD COLUMN order_id INTEGER DEFAULT NULL`).catch(() => {});

  // Migration — add chat_id to reviews to track which client left the review
  await run(`ALTER TABLE reviews ADD COLUMN chat_id TEXT DEFAULT NULL`).catch(() => {});

  // Migration — add admin_reply column to reviews
  await run(`ALTER TABLE reviews ADD COLUMN admin_reply TEXT`).catch(() => {});

  // Migration — add review_requested timestamp to orders
  await run(`ALTER TABLE orders ADD COLUMN review_requested DATETIME DEFAULT NULL`).catch(() => {});

  // Default settings
  const defaults = [
    ['greeting',       'Добро пожаловать в Nevesty Models — агентство профессиональных моделей!'],
    ['about',          'Мы работаем с 2018 года. Более 200 моделей в базе. Fashion, Commercial, Events.'],
    ['contacts_phone', '+7 (900) 000-00-00'],
    ['contacts_email', 'info@nevesty-models.ru'],
    ['contacts_insta', '@nevesty_models'],
    ['contacts_addr',  'Москва, ул. Пресненская, 8'],
    ['pricing',        'Fashion/Commercial — от 5000₽/час\nEvents — от 8000₽/час\nRunway — от 10000₽/час'],
    ['notif_new_order',       '1'],
    ['notif_status',          '1'],
    ['notif_message',         '1'],
    ['wishlist_enabled',      '1'],
    ['quick_booking_enabled', '1'],
  ];
  for (const [key, value] of defaults) {
    await run('INSERT OR IGNORE INTO bot_settings (key,value) VALUES (?,?)', [key, value]);
  }

  // Blocked clients
  await run(`CREATE TABLE IF NOT EXISTS blocked_clients (
    chat_id INTEGER PRIMARY KEY,
    reason TEXT,
    blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    blocked_by INTEGER
  )`).catch(()=>{});

  // Client notification preferences
  await run(`CREATE TABLE IF NOT EXISTS client_prefs (
    chat_id INTEGER PRIMARY KEY,
    notify_status INTEGER DEFAULT 1,
    notify_promo INTEGER DEFAULT 1,
    notify_review INTEGER DEFAULT 1,
    language TEXT DEFAULT 'ru',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(() => {});

  // Audit log — admin actions journal
  await run(`CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_chat_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(()=>{});
  await run(`CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_log(admin_chat_id)`).catch(()=>{});

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

  // A/B experiments — synced from AI Factory ExperimentDesigner
  await run(`CREATE TABLE IF NOT EXISTS ab_experiments (
    id TEXT PRIMARY KEY,
    hypothesis TEXT NOT NULL,
    type TEXT DEFAULT 'both',
    metric TEXT,
    variant_a TEXT,
    variant_b TEXT,
    effort TEXT DEFAULT 'medium',
    expected_lift TEXT,
    status TEXT DEFAULT 'proposed',
    recommendation TEXT,
    eval_reason TEXT,
    department TEXT DEFAULT 'experiments',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(()=>{});

  // Scheduled broadcasts table
  await run(`CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    scheduled_at DATETIME NOT NULL,
    segment TEXT DEFAULT 'all',
    status TEXT DEFAULT 'pending',
    created_by TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )`).catch(() => {});
  await run(`CREATE INDEX IF NOT EXISTS idx_sched_bcast_status ON scheduled_broadcasts(status, scheduled_at)`).catch(() => {});

  // Migrations — add columns that may not exist in older DBs
  await run(`ALTER TABLE models ADD COLUMN city TEXT`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN featured INTEGER DEFAULT 0`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN phone TEXT`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN order_count INTEGER DEFAULT 0`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN view_count INTEGER DEFAULT 0`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN archived INTEGER DEFAULT 0`).catch(() => {});
  await run(`CREATE INDEX IF NOT EXISTS idx_models_archived ON models(archived)`).catch(() => {});
  await run(`ALTER TABLE models ADD COLUMN video_url TEXT DEFAULT NULL`).catch(() => {});

  // UTM tracking columns on orders
  await run(`ALTER TABLE orders ADD COLUMN utm_source TEXT DEFAULT ''`).catch(() => {});
  await run(`ALTER TABLE orders ADD COLUMN utm_medium TEXT DEFAULT ''`).catch(() => {});
  await run(`ALTER TABLE orders ADD COLUMN utm_campaign TEXT DEFAULT ''`).catch(() => {});

  // Payment columns on orders (migration v8)
  await run(`ALTER TABLE orders ADD COLUMN payment_id TEXT DEFAULT NULL`).catch(() => {});
  await run(`ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT NULL`).catch(() => {});
  await run(`ALTER TABLE orders ADD COLUMN paid_at DATETIME DEFAULT NULL`).catch(() => {});
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_payment_id ON orders(payment_id)`).catch(() => {});

  // Internal note column for quick manager notes (migration v9)
  await run(`ALTER TABLE orders ADD COLUMN internal_note TEXT`).catch(err => { if (err && !err.message.includes('duplicate')) console.error(err); });

  // Broadcast stats columns (migration v10)
  await run(`ALTER TABLE scheduled_broadcasts ADD COLUMN sent_count INTEGER DEFAULT 0`).catch(() => {});
  await run(`ALTER TABLE scheduled_broadcasts ADD COLUMN error_count INTEGER DEFAULT 0`).catch(() => {});
  await run(`ALTER TABLE scheduled_broadcasts ADD COLUMN sent_at TEXT`).catch(() => {});
  // Broadcast photo support
  await run(`ALTER TABLE scheduled_broadcasts ADD COLUMN photo_url TEXT`).catch(() => {});

  // Indexes for frequent queries
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_model_id ON orders(model_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_client_chat ON orders(client_chat_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_messages_order ON messages(order_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_category ON models(category)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_available ON models(available)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_featured ON models(featured DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_models_featured_active ON models(featured) WHERE featured=1`);
  await run(`CREATE INDEX IF NOT EXISTS idx_sessions_updated ON telegram_sessions(updated_at)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_findings_status ON agent_findings(status)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_findings_created ON agent_findings(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_discussions_created ON agent_discussions(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_agent_logs_created ON agent_logs(created_at DESC)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_order_status_history_order ON order_status_history(order_id)`);
  await run(`CREATE INDEX IF NOT EXISTS idx_order_status_history_created ON order_status_history(created_at DESC)`);

  // Additional performance indexes
  const perfIndexes = [
    ['idx_orders_chat_id',          'CREATE INDEX IF NOT EXISTS idx_orders_chat_id ON orders(chat_id)'],
    ['idx_orders_created_at',       'CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC)'],
    ['idx_orders_status_created',   'CREATE INDEX IF NOT EXISTS idx_orders_status_created ON orders(status, created_at DESC)'],
    ['idx_models_category_active',  'CREATE INDEX IF NOT EXISTS idx_models_category_active ON models(category) WHERE archived=0'],
    ['idx_models_city_active',      'CREATE INDEX IF NOT EXISTS idx_models_city_active ON models(city) WHERE archived=0'],
    ['idx_models_available_active', 'CREATE INDEX IF NOT EXISTS idx_models_available_active ON models(available) WHERE archived=0'],
    ['idx_sessions_state',          'CREATE INDEX IF NOT EXISTS idx_sessions_state ON telegram_sessions(state)'],
    ['idx_sessions_updated_at',     'CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON telegram_sessions(updated_at)'],
    ['idx_reviews_status',          'CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status)'],
    ['idx_factory_tasks_status_pri','CREATE INDEX IF NOT EXISTS idx_factory_tasks_status_pri ON factory_tasks(status, priority)'],
    ['idx_audit_created',           'CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC)'],
    // v9 indexes
    ['idx_orders_payment_status',   'CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status)'],
    ['idx_models_status',           'CREATE INDEX IF NOT EXISTS idx_models_status ON models(available)'],
    ['idx_reviews_approved',        'CREATE INDEX IF NOT EXISTS idx_reviews_approved ON reviews(approved)'],
    ['idx_reviews_model_id',        'CREATE INDEX IF NOT EXISTS idx_reviews_model_id ON reviews(model_id)'],
    ['idx_models_city',             'CREATE INDEX IF NOT EXISTS idx_models_city ON models(city)'],
  ];
  for (const [name, sql] of perfIndexes) {
    await run(sql).catch(e => console.log(`Index ${name}: ${e.message}`));
  }

  // Schema version 9 — indexes and schema versioning
  await run(`INSERT OR IGNORE INTO schema_versions (version, description) VALUES (9, 'indexes and schema versioning')`).catch(() => {});

  // Audit log extended columns (migration v10)
  await run(`ALTER TABLE audit_log ADD COLUMN admin_username TEXT`).catch(() => {});
  await run(`ALTER TABLE audit_log ADD COLUMN entity TEXT`).catch(() => {});
  await run(`ALTER TABLE audit_log ADD COLUMN ip TEXT`).catch(() => {});
  await run(`CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)`).catch(() => {});
  await run(`INSERT OR IGNORE INTO schema_versions (version, description) VALUES (10, 'audit_log extended columns for admin actions')`).catch(() => {});

  // Wishlists table — named alias for favorites (schema v11)
  await run(`CREATE TABLE IF NOT EXISTS wishlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    model_id INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, model_id),
    FOREIGN KEY(model_id) REFERENCES models(id) ON DELETE CASCADE
  )`).catch(() => {});
  await run(`CREATE INDEX IF NOT EXISTS idx_wishlists_chat_id ON wishlists(chat_id)`).catch(() => {});
  await run(`INSERT OR IGNORE INTO schema_versions (version, description) VALUES (11, 'wishlists table')`).catch(() => {});

  // FAQ table (schema v12)
  await run(`CREATE TABLE IF NOT EXISTS faq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
  )`).catch(() => {});
  await run(`INSERT OR IGNORE INTO schema_versions (version, description) VALUES (12, 'faq table')`).catch(() => {});

  // Schema v13 — wishlists index & ensure INTEGER chat_id compatibility
  await run(`CREATE INDEX IF NOT EXISTS idx_wishlists_chat_model ON wishlists(chat_id, model_id)`).catch(() => {});
  await run(`INSERT OR IGNORE INTO schema_versions (version, description) VALUES (13, 'wishlists composite index, quick_booking_enabled & wishlist_enabled defaults')`).catch(() => {});

  // Seed FAQ items if empty
  const faqCount = await get('SELECT COUNT(*) as n FROM faq').catch(() => ({ n: 0 }));
  if (!faqCount.n) {
    const faqItems = [
      ['Как забронировать модель?', 'Нажмите «📋 Забронировать» в меню, выберите категорию и заполните форму. Менеджер свяжется с вами в течение часа.'],
      ['Сколько стоят услуги?', 'Стоимость зависит от типа мероприятия и длительности. Минимальный бюджет — от 8 000 ₽. Точную цену уточните через форму заявки.'],
      ['Как долго рассматривается заявка?', 'Обычно 1–2 часа в рабочее время. Срочные запросы — в течение 30 минут при наличии свободных моделей.'],
      ['Можно ли выбрать конкретную модель?', 'Да! В каталоге выберите понравившуюся модель и нажмите «📋 Забронировать» прямо на её карточке.'],
      ['Какие гарантии качества?', 'Все модели прошли отбор. Средний рейтинг по отзывам — 4.8/5. При несоответствии — полный возврат средств.'],
      ['Работаете ли вы в выходные?', 'Да, мы принимаем заявки 7 дней в неделю. Менеджеры онлайн с 9:00 до 22:00.'],
    ];
    for (const [q, a] of faqItems) {
      await run('INSERT INTO faq (question, answer) VALUES (?, ?)', [q, a]).catch(() => {});
    }
  }

  // Seed admin if not exists
  const admin = await get('SELECT id FROM admins WHERE username = ?', [process.env.ADMIN_USERNAME || 'admin']);
  if (!admin) {
    const adminPassword = process.env.ADMIN_PASSWORD ||
      require('crypto').randomBytes(12).toString('base64').replace(/[+/=]/g, '').slice(0, 16);
    const hash = await bcrypt.hash(adminPassword, 10);
    await run(
      'INSERT INTO admins (username, email, password_hash, role) VALUES (?, ?, ?, ?)',
      [process.env.ADMIN_USERNAME || 'admin', process.env.AGENCY_EMAIL || 'admin@nevesty-models.ru', hash, 'superadmin']
    );
    if (!process.env.ADMIN_PASSWORD) {
      console.log(`[SETUP] Admin created. Temporary password: ${adminPassword}`);
      console.log('[SETUP] Set ADMIN_PASSWORD in .env to use a fixed password.');
    } else {
      console.log('[SETUP] Admin account created from ADMIN_PASSWORD env var.');
    }
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

// ─── Cached setting helpers ───────────────────────────────────────────────────
/**
 * Read one key from bot_settings, with in-memory TTL cache.
 * @param {string} key
 * @param {number} [ttlMs=TTL_SETTINGS]
 * @returns {Promise<string|null>}
 */
async function getSetting(key, ttlMs = TTL_SETTINGS) {
  const cacheKey = `setting:${key}`;
  const cached = cache.get(cacheKey);
  if (cached !== undefined) return cached;
  const row = await get('SELECT value FROM bot_settings WHERE key = ?', [key]);
  const value = row ? row.value : null;
  cache.set(cacheKey, value, ttlMs);
  return value;
}

/**
 * Write one key to bot_settings and invalidate its cache entry.
 * @param {string} key
 * @param {string} value
 */
async function setSetting(key, value) {
  await run(
    'INSERT OR REPLACE INTO bot_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
    [key, String(value ?? '')]
  );
  cache.del(`setting:${key}`);
}

module.exports = { initDatabase, initDB: initDatabase, query, run, get, generateOrderNumber, closeDatabase, getSetting, setSetting };
