require('dotenv').config();
const crypto = require('crypto');
const TelegramBot = require('node-telegram-bot-api');
const { query, run, get, generateOrderNumber } = require('./database');
const { RU } = require('./utils/strings');
const {
  STATUS_LABELS,
  VALID_STATUSES,
  EVENT_TYPES,
  CATEGORIES,
  MODEL_CATEGORIES,
  MODEL_HAIR_COLORS,
  MODEL_EYE_COLORS,
  DURATIONS,
} = require('./utils/constants');

const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
const SITE_URL  = process.env.SITE_URL || 'http://localhost:3000';
const WEBHOOK_URL    = process.env.WEBHOOK_URL || '';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || crypto.randomBytes(32).toString('hex');

let bot = null;

// ─── Session timers (in-memory, cleared on restart) ───────────────────────────
const sessionTimers = new Map();

const ACTIVE_BOOKING_STATES = new Set([
  'bk_s1', 'bk_s2_event', 'bk_s2_date', 'bk_s2_dur', 'bk_s2_loc',
  'bk_s2_budget', 'bk_s2_comments', 'bk_s3_name', 'bk_s3_phone',
  'bk_s3_email', 'bk_s3_tg', 'bk_s4',
  'leave_review_text', 'bk_quick_name', 'bk_quick_phone',
  'profile_edit_name', 'profile_edit_phone',
]);

function resetSessionTimer(chatId) {
  clearTimeout(sessionTimers.get(chatId));
  const timer = setTimeout(async () => {
    try {
      const sess = await getSession(chatId);
      const state = sess?.state;
      if (state && ACTIVE_BOOKING_STATES.has(state)) {
        await safeSend(chatId,
          '⏰ *Сесія завершилась через неактивність\\.*\n\nВи не завершили оформлення заявки\\. Хочете продовжити або почати заново?',
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [{ text: '▶ Продовжити',    callback_data: 'session_continue' }],
                [{ text: '🔄 Почати заново', callback_data: 'session_restart'  }],
              ]
            }
          }
        );
      }
    } catch {}
    sessionTimers.delete(chatId);
  }, 30 * 60 * 1000); // 30 minutes
  sessionTimers.set(chatId, timer);
}

// ─── Booking progress helper ──────────────────────────────────────────────────
function bookingProgress(step, total = 4) {
  const filled = '▓'.repeat(step);
  const empty  = '░'.repeat(total - step);
  return `${filled}${empty} Шаг ${step}/${total}`;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// ─── UTM link helper ──────────────────────────────────────────────────────────
function siteUrl(path, utmParams = {}) {
  const base = SITE_URL.replace(/\/$/, '') + path;
  const params = new URLSearchParams({
    utm_source: 'telegram',
    utm_medium: 'bot',
    ...utmParams
  });
  return `${base}?${params.toString()}`;
}

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, '\\$&');
}

function isAdmin(chatId) { return ADMIN_IDS.includes(String(chatId)); }

async function getAdminChatIds() {
  try {
    const rows = await query("SELECT telegram_id FROM admins WHERE telegram_id IS NOT NULL AND telegram_id != ''");
    return [...new Set([...ADMIN_IDS, ...rows.map(r => r.telegram_id)])];
  } catch { return [...ADMIN_IDS]; }
}

async function safeSend(chatId, text, opts = {}) {
  // Telegram hard limit is 4096 chars — truncate gracefully
  const MAX = 4096;
  if (text && text.length > MAX) text = text.slice(0, MAX - 3) + '…';
  try {
    return await bot.sendMessage(chatId, text, opts);
  } catch (e) {
    if (opts.parse_mode && /parse entities|can't parse/i.test(e.message)) {
      try { return await bot.sendMessage(chatId, text, { ...opts, parse_mode: undefined }); } catch {}
    }
    console.warn(`[Bot] send→${chatId}: ${e.message}`);
    return null;
  }
}

async function safePhoto(chatId, photo, opts = {}) {
  // Telegram caption limit is 1024 chars
  if (opts.caption && opts.caption.length > 1024) {
    opts = { ...opts, caption: opts.caption.slice(0, 1021) + '…' };
  }
  try { return await bot.sendPhoto(chatId, photo, opts); }
  catch { return safeSend(chatId, opts.caption || '📷', { parse_mode: opts.parse_mode }); }
}

// ─── Audit log ────────────────────────────────────────────────────────────────

async function logAdminAction(adminChatId, action, entityType = null, entityId = null, details = null) {
  await run(
    `INSERT INTO audit_log (admin_chat_id, action, entity_type, entity_id, details) VALUES (?,?,?,?,?)`,
    [adminChatId, action, entityType, entityId, details ? JSON.stringify(details) : null]
  ).catch(()=>{});
}

// ─── Session ──────────────────────────────────────────────────────────────────

// ─── In-memory session cache (write-through to SQLite) ───────────────────────
// Устраняет зависания когда SQLite занята агентами: чтение всегда из памяти,
// запись сначала в память, затем асинхронно в SQLite.
const _sessionCache = new Map(); // chatId → { state, data, updated_at }

async function getSession(chatId) {
  const key = String(chatId);
  if (_sessionCache.has(key)) return _sessionCache.get(key);
  try {
    const row = await get('SELECT * FROM telegram_sessions WHERE chat_id=?', [key]);
    if (row) _sessionCache.set(key, row);
    return row || null;
  } catch { return null; }
}

async function setSession(chatId, state, data = {}) {
  const key = String(chatId);
  const rec = { chat_id: key, state, data: JSON.stringify(data), updated_at: new Date().toISOString() };
  _sessionCache.set(key, rec);
  // Persist to SQLite in background — bot doesn't wait for it
  run(
    `INSERT OR REPLACE INTO telegram_sessions (chat_id,state,data,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)`,
    [key, state, JSON.stringify(data)]
  ).catch(e => console.error('[Bot] setSession persist:', e.message));
}

async function clearSession(chatId) { await setSession(chatId, 'idle', {}); }

function sessionData(session) {
  try {
    const d = session?.data;
    return typeof d === 'string' ? JSON.parse(d || '{}') : (d || {});
  } catch { return {}; }
}

// ─── In-memory settings cache (TTL 60s) ──────────────────────────────────────
const _settingsCache = new Map(); // key → { value, expiresAt }
const SETTINGS_TTL = 60_000;

async function getSetting(key) {
  const cached = _settingsCache.get(key);
  if (cached && Date.now() < cached.expiresAt) return cached.value;
  try {
    const r = await get('SELECT value FROM bot_settings WHERE key=?', [key]);
    const value = r?.value ?? null;
    _settingsCache.set(key, { value, expiresAt: Date.now() + SETTINGS_TTL });
    return value;
  } catch { return cached?.value ?? null; }
}

async function setSetting(key, value) {
  _settingsCache.set(key, { value, expiresAt: Date.now() + SETTINGS_TTL });
  await run('INSERT OR REPLACE INTO bot_settings (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)', [key, value]);
}

// ─── Admin Handlers module ────────────────────────────────────────────────────
const _adminHandlers = require('./handlers/admin');
_adminHandlers.init({ safeSend, isAdmin, esc });
const showAdminStats  = _adminHandlers.showAdminStats;
const showAdminModels = _adminHandlers.showAdminModels;
const showAdminOrders = _adminHandlers.showAdminOrders;

// ─── Keyboards ────────────────────────────────────────────────────────────────

// Persistent ReplyKeyboard — всегда показывается внизу чата вместо клавиатуры
const REPLY_KB_CLIENT = {
  keyboard: [
    [{ text: '⭐ Топ-модели' }, { text: '💃 Каталог' }],
    [{ text: '📝 Подать заявку' }, { text: '⚡ Быстрая заявка' }],
    [{ text: '❤️ Избранное' }, { text: '💬 Менеджер' }],
    [{ text: '📋 Мои заявки' }, { text: '🔍 Статус заявки' }],
    [{ text: '💰 Прайс' }, { text: '📞 Контакты' }, { text: '❓ FAQ' }],
  ],
  resize_keyboard: true,
  persistent: true,
};

const REPLY_KB_ADMIN = {
  keyboard: [
    [{ text: '📋 Заявки' }, { text: '💃 Модели' }, { text: '📊 Статистика' }],
    [{ text: '🤖 Организм' }, { text: '📡 Фид агентов' }, { text: '💬 Обсуждения' }],
    [{ text: '⚙️ Настройки' }, { text: '📢 Рассылка' }, { text: '📤 Экспорт' }],
  ],
  resize_keyboard: true,
  persistent: true,
};

async function buildClientKeyboard() {
  const [tgChannel, calcEnabled, wishlistEnabled, quickBookingEnabled, searchEnabled, reviewsEnabled] = await Promise.all([
    getSetting('tg_channel').catch(() => null),
    getSetting('calc_enabled').catch(() => null),
    getSetting('wishlist_enabled', '1').catch(() => '1'),
    getSetting('quick_booking_enabled', '1').catch(() => '1'),
    getSetting('search_enabled', '1').catch(() => '1'),
    getSetting('reviews_enabled', '1').catch(() => '1'),
  ]);

  // Row 1: always
  const rows = [
    [{ text: '💃 Каталог',         callback_data: 'cat_cat__0' },
     { text: '⭐ Топ-модели',      callback_data: 'cat_top_0'  }],
  ];

  // Row 2: booking (quick booking is gated)
  const bookingRow = [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }];
  if (quickBookingEnabled !== '0') bookingRow.push({ text: '⚡ Быстрая заявка', callback_data: 'bk_quick' });
  rows.push(bookingRow);

  // Row 3: orders + profile
  rows.push([{ text: '📋 Мои заявки', callback_data: 'my_orders' },
             { text: '👤 Мой профиль', callback_data: 'profile' }]);

  // Row 4: wishlist + calculator (wishlist gated)
  const favRow = [];
  if (wishlistEnabled !== '0') favRow.push({ text: '❤️ Избранное', callback_data: 'fav_list_0' });
  if (calcEnabled === '1') favRow.push({ text: '🧮 Калькулятор', callback_data: 'calculator' });
  if (favRow.length) rows.push(favRow);

  // Row 5: reviews + FAQ (reviews gated)
  const reviewRow = [];
  if (reviewsEnabled !== '0') reviewRow.push({ text: '⭐ Отзывы', callback_data: 'show_reviews' });
  reviewRow.push({ text: '❓ FAQ', callback_data: 'faq' });
  rows.push(reviewRow);

  // Row 6: loyalty + referral
  rows.push([{ text: '🎁 Реферальная программа', callback_data: 'referral' },
             { text: '💫 Баллы лояльности', callback_data: 'loyalty' }]);

  // Row 7: category filters
  rows.push([{ text: '👗 Fashion', callback_data: 'cat_filter_fashion' },
             { text: '📷 Commercial', callback_data: 'cat_filter_commercial' }]);

  // Row 8: search (gated)
  if (searchEnabled !== '0') {
    rows.push([{ text: '🔍 Поиск по параметрам', callback_data: 'cat_search' },
               { text: '📏 Поиск по росту', callback_data: 'search_height_input' }]);
  }

  // Row 9: pricing + manager
  rows.push([{ text: '💰 Прайс-лист', callback_data: 'pricing' },
             { text: '💬 Написать менеджеру', callback_data: 'contact_mgr' }]);

  // Row 10: about + contacts
  rows.push([{ text: 'ℹ️ О нас', callback_data: 'about_us' },
             { text: '📞 Контакты', callback_data: 'contacts' }]);

  if (tgChannel) {
    rows.push([{ text: '📣 Наш канал', callback_data: 'tg_channel' }]);
  }
  if (SITE_URL.startsWith('https://')) {
    const webappUrl = SITE_URL.replace(/\/$/, '') + '/webapp.html';
    rows.unshift([{ text: '📱 Открыть Mini App', web_app: { url: webappUrl } }]);
    rows.push([{ text: '🌐 Наш сайт', url: siteUrl('/', { utm_campaign: 'main_menu' }) }]);
  }
  return { inline_keyboard: rows };
}

const KB_MAIN_ADMIN = (badge, score) => {
  const health = score != null ? ` 💚${score}%` : '';
  return {
    inline_keyboard: [
      [{ text: `📋 Заявки${badge}`,          callback_data: 'adm_orders__0'  },
       { text: '💃 Модели',                  callback_data: 'adm_models_0'   }],
      [{ text: '📊 Статистика',              callback_data: 'adm_stats'      },
       { text: '📈 Дашборд',                callback_data: 'adm_dashboard'  },
       { text: '⚡ Кратко',                 callback_data: 'adm_quick_stats'}],
      [{ text: `🤖 Организм${health}`,       callback_data: 'adm_organism'   },
       { text: '⚙️ Настройки',              callback_data: 'adm_settings'   }],
      [{ text: '📢 Рассылка',               callback_data: 'adm_broadcast'  },
       { text: '📅 Рассылки',              callback_data: 'adm_sched_bcast'},
       { text: '📤 Экспорт заявок',         callback_data: 'adm_export'     }],
      [{ text: '➕ Добавить модель',         callback_data: 'adm_addmodel'   },
       { text: '👑 Администраторы',          callback_data: 'adm_admins'     }],
      [{ text: '📡 Фид агентов',            callback_data: 'agent_feed_0'   },
       { text: '⭐ Отзывы',                 callback_data: 'adm_reviews'    },
       { text: '💬 Обсуждения',            callback_data: 'adm_discussions'}],
      [{ text: '🔍 Найти заявку',           callback_data: 'adm_search_order'     },
       { text: '🏭 AI Factory',             callback_data: 'adm_factory'          }],
      [{ text: '💡 Growth Actions',         callback_data: 'adm_factory_growth' },
       { text: '🎯 AI Задачи',             callback_data: 'adm_factory_tasks'  }],
      [{ text: '👥 Клиенты',                callback_data: 'adm_clients'         },
       { text: '📋 Журнал',                 callback_data: 'adm_audit_log'       }],
      ...(SITE_URL.startsWith('https://') ? [[
        { text: '📱 Mini App', web_app: { url: SITE_URL.replace(/\/$/, '') + '/webapp.html' } },
        { text: '🌐 Сайт', url: siteUrl('/', { utm_campaign: 'admin_menu' }) },
      ]] : []),
    ]
  };
};

// ─── Client screens ───────────────────────────────────────────────────────────

async function showMainMenu(chatId, name) {
  await clearSession(chatId);
  const [greeting, menuText, clientKb] = await Promise.all([
    getSetting('greeting').catch(() => null),
    getSetting('main_menu_text').catch(() => null),
    buildClientKeyboard(),
  ]);
  // Сначала показываем persistent ReplyKeyboard
  await safeSend(chatId,
    `💎 Nevesty Models — меню активировано`,
    { reply_markup: REPLY_KB_CLIENT }
  );
  if (greeting) {
    const rawGreeting = greeting.replace('{name}', name || 'гость');
    return safeSend(chatId, esc(rawGreeting), { parse_mode: 'MarkdownV2', reply_markup: clientKb });
  }
  const greetingText = menuText || 'Выберите действие:';
  return safeSend(chatId,
    `💎 *Nevesty Models*\n\nДобро пожаловать${name ? ', ' + esc(name) : ''}\\!\n\n_Агентство профессиональных моделей — Fashion, Commercial, Events_\n\n${esc(greetingText)}`,
    { parse_mode: 'MarkdownV2', reply_markup: clientKb }
  );
}

// ─── Admin: Client Management ─────────────────────────────────────────────────

async function showAdminClients(chatId, page = 0) {
  if (!isAdmin(chatId)) return;
  const LIMIT = 8;
  const clients = await query(`
    SELECT
      o.client_chat_id as chat_id,
      MAX(o.client_name) as name,
      MAX(o.client_phone) as phone,
      COUNT(*) as total_orders,
      SUM(CASE WHEN o.status='completed' THEN 1 ELSE 0 END) as completed,
      MAX(o.created_at) as last_order
    FROM orders o
    WHERE o.client_chat_id IS NOT NULL AND o.client_chat_id != '' AND CAST(o.client_chat_id AS INTEGER) > 0
    GROUP BY o.client_chat_id
    ORDER BY last_order DESC
    LIMIT ? OFFSET ?`, [LIMIT, page * LIMIT]);

  const total = (await get(`SELECT COUNT(DISTINCT client_chat_id) as cnt FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND CAST(client_chat_id AS INTEGER) > 0`))?.cnt || 0;

  if (!clients.length) return safeSend(chatId, '👥 Клиентов пока нет\\.', { parse_mode: 'MarkdownV2' });

  const keyboard = clients.map(c => [{
    text: `${c.name || 'Без имени'} (${c.total_orders} зак.)`,
    callback_data: `adm_client_${c.chat_id}`
  }]);

  // Pagination
  const nav = [];
  if (page > 0) nav.push({ text: '← Назад', callback_data: `adm_clients_${page - 1}` });
  if ((page + 1) * LIMIT < total) nav.push({ text: 'Вперёд →', callback_data: `adm_clients_${page + 1}` });
  if (nav.length) keyboard.push(nav);
  keyboard.push([{ text: '🔙 Admin панель', callback_data: 'adm_panel' }]);

  await safeSend(chatId, `👥 *Клиенты* \\(${total} всего\\)\nСтраница ${page + 1}`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard }
  });
}

async function showAdminClientCard(chatId, clientId) {
  if (!isAdmin(chatId)) return;

  const orders = await query(`SELECT * FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 10`, [String(clientId)]);
  if (!orders.length) return safeSend(chatId, '❌ Клиент не найден или нет заявок\\.', { parse_mode: 'MarkdownV2' });

  const client = orders[0];
  const stats = await get(`SELECT
    COUNT(*) as total,
    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
    SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled
  FROM orders WHERE client_chat_id=?`, [String(clientId)]);

  const isBlocked = !!(await get(`SELECT chat_id FROM blocked_clients WHERE chat_id=?`, [clientId]).catch(()=>null));
  const loyalty = await get(`SELECT points, total_earned, level FROM loyalty_points WHERE chat_id=?`, [clientId]).catch(()=>null);

  const recentOrders = orders.slice(0, 5).map(o =>
    `• #${esc(o.order_number || String(o.id))} — ${esc(o.status)}`
  ).join('\n');

  const text = [
    `👤 *Клиент: ${esc(client.client_name || 'Без имени')}*`,
    `📞 ${esc(client.client_phone || '—')}`,
    client.client_email ? `📧 ${esc(client.client_email)}` : null,
    `🆔 Chat ID: \`${clientId}\``,
    ``,
    `📊 *Статистика:*`,
    `Заявок всего: *${stats.total}*`,
    `✅ Завершено: *${stats.completed}*`,
    `❌ Отменено: *${stats.cancelled}*`,
    loyalty ? `💫 Баллов: *${loyalty.points}* \\(${loyalty.total_earned} всего\\)` : null,
    ``,
    `📋 *Последние заявки:*`,
    recentOrders,
    ``,
    isBlocked ? `⛔ *Клиент ЗАБЛОКИРОВАН*` : null,
  ].filter(Boolean).join('\n');

  await safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '✉️ Написать клиенту', callback_data: `adm_msg_client_${clientId}` }],
      [{
        text: isBlocked ? '✅ Разблокировать' : '⛔ Заблокировать',
        callback_data: isBlocked ? `adm_unblock_${clientId}` : `adm_block_${clientId}`
      }],
      [{ text: '← Список клиентов', callback_data: 'adm_clients' }]
    ]}
  });
}

async function showAdminMenu(chatId, name) {
  if (!isAdmin(chatId)) return;
  await clearSession(chatId);
  try {
    const [ordersRow, scoreRow] = await Promise.all([
      get("SELECT COUNT(*) as n FROM orders WHERE status='new'").catch(()=>({n:0})),
      get("SELECT message FROM agent_logs WHERE from_name='Orchestrator' ORDER BY created_at DESC LIMIT 1").catch(()=>null),
    ]);
    const badge = ordersRow.n > 0 ? ` 🔴${ordersRow.n}` : '';
    const scoreMatch = scoreRow?.message?.match(/Health Score:\s*(\d+)%/);
    const score = scoreMatch ? parseInt(scoreMatch[1]) : null;
    // Сначала показываем persistent ReplyKeyboard для быстрого доступа
    await safeSend(chatId,
      `👑 Панель администратора — меню активировано`,
      { reply_markup: REPLY_KB_ADMIN }
    );
    return safeSend(chatId,
      `👑 *Панель администратора*${name ? `\n_${esc(name)}_` : ''}\n\nЗаявок в очереди: *${ordersRow.n}*`,
      { parse_mode: 'MarkdownV2', reply_markup: KB_MAIN_ADMIN(badge, score) }
    );
  } catch (e) { console.error('[Bot] showAdminMenu:', e.message); }
}

// ── Catalog with category filter ──────────────────────────────────────────────

// Per-user sort preferences for catalog (in-memory)
const catalogSortPrefs = new Map(); // chatId → 'featured' | 'alpha'

async function showCatalog(chatId, cat, page, filter) {
  bot.sendChatAction(chatId, 'typing').catch(() => {});
  try {
    // Normalise: support showCatalog(chatId, page, filter) new-style calls
    if (typeof cat === 'number' && (typeof page === 'object' || page === undefined)) {
      filter = page || {};
      page   = cat;
      cat    = filter.category || '';
    }
    if (!filter) filter = {};
    if (!cat) cat = filter.category || '';
    page = page || 0;

    // Per-user sort preference
    const sortPref = catalogSortPrefs.get(String(chatId)) || 'featured';
    const orderClause = sortPref === 'alpha'
      ? 'ORDER BY name ASC'
      : 'ORDER BY featured DESC, id ASC';

    // Build WHERE clause
    const conditions = ['available=1', "COALESCE(archived,0)=0"];
    const params = [];
    if (cat) { conditions.push('category=?'); params.push(cat); }
    if (filter.city) { conditions.push('city=?'); params.push(filter.city); }
    const where = conditions.join(' AND ');
    const models = await query(`SELECT * FROM models WHERE ${where} ${orderClause}`, params);

    if (!models.length) {
      return safeSend(chatId, '📭 Моделей по выбранному фильтру нет\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'main_menu' }]] }
      });
    }

    const perPage = parseInt(await getSetting('catalog_per_page') || '5');
    const total   = models.length;
    const slice   = models.slice(page * perPage, page * perPage + perPage);

    // Category filter buttons (fashion / commercial / events)
    const catFilterRow = [
      { text: (cat === 'fashion'    ? '✅ ' : '') + '💄 Фэшн',        callback_data: 'cat_filter_fashion'    },
      { text: (cat === 'commercial' ? '✅ ' : '') + '📸 Коммерческая', callback_data: 'cat_filter_commercial' },
      { text: (cat === 'events'     ? '✅ ' : '') + '🎉 Мероприятия',  callback_data: 'cat_filter_events'     },
    ];

    // Sort row
    const sortRow = [
      { text: (sortPref === 'featured' ? '✅ ' : '') + '⭐ Сначала топ', callback_data: 'cat_sort_featured' },
      { text: (sortPref === 'alpha'    ? '✅ ' : '') + '🔤 По алфавиту', callback_data: 'cat_sort_alpha'    },
    ];

    // Dynamic city buttons from settings, fallback to DB distinct cities
    const citiesSetting = await getSetting('cities_list').catch(() => '');
    let cityList = citiesSetting
      ? citiesSetting.split(',').map(c => c.trim()).filter(Boolean).slice(0, 8)
      : [];
    if (!cityList.length) {
      const cityRows2 = await query(
        "SELECT DISTINCT city FROM models WHERE available=1 AND city IS NOT NULL AND city != '' ORDER BY city LIMIT 8"
      ).catch(() => []);
      cityList = cityRows2.map(r => r.city);
    }
    const cityRows = [];
    for (let i = 0; i < cityList.length; i += 2) {
      const row = [{ text: '🏙 ' + cityList[i], callback_data: 'cat_city_' + encodeURIComponent(cityList[i]) + '_0' }];
      if (cityList[i+1]) row.push({ text: '🏙 ' + cityList[i+1], callback_data: 'cat_city_' + encodeURIComponent(cityList[i+1]) + '_0' });
      cityRows.push(row);
    }

    // Model buttons (show ⭐ for featured models)
    const modelBtns = slice.map(m => [{
      text: `${m.available ? '🟢' : '🔴'}${m.featured ? '⭐' : ''} ${m.name}  ·  ${m.height}см  ·  ${m.hair_color || ''}`,
      callback_data: `cat_model_${m.id}`
    }]);

    // Pagination
    const nav = [];
    if (page > 0)                         nav.push({ text: '◀️',  callback_data: `cat_cat_${cat}_${page-1}` });
    if ((page+1)*perPage < total)         nav.push({ text: '▶️',  callback_data: `cat_cat_${cat}_${page+1}` });

    const keyboard = [
      catFilterRow,
      sortRow,
      ...cityRows,
      ...modelBtns,
      ...(nav.length ? [nav] : []),
      [{ text: '🔍 Поиск',           callback_data: 'cat_search' },
       { text: '📝 Оформить заявку',  callback_data: 'bk_start'  }],
      [{ text: '🏠 Главное меню',     callback_data: 'main_menu' }],
    ];

    const label = CATEGORIES[cat] || 'Все';
    const cityLabel = filter.city ? ` — 🏙 ${esc(filter.city)}` : '';
    return safeSend(chatId,
      `💃 *Каталог моделей — ${esc(label)}${cityLabel}*\n\nНайдено: ${total} ${ru_plural(total,'модель','модели','моделей')}\n\nВыберите модель для просмотра:`,
      { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } }
    );
  } catch (e) { console.error('[Bot] showCatalog:', e.message); }
}

async function showModel(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] }
    });

    // Increment view counter (fire-and-forget)
    run('UPDATE models SET view_count = COALESCE(view_count,0) + 1 WHERE id=?', [modelId]).catch(() => {});

    // Reviews count
    const reviewRow = await get('SELECT COUNT(*) as cnt FROM reviews WHERE model_id=? AND approved=1', [modelId]).catch(() => null);
    const reviewCount = reviewRow ? (reviewRow.cnt || 0) : 0;

    // Completed orders count
    const orderCountRow = await get('SELECT COUNT(*) as n FROM orders WHERE model_id=? AND status="completed"', [m.id]).catch(() => ({ n: 0 }));
    const completedOrders = orderCountRow ? (orderCountRow.n || 0) : 0;

    const lines = [];
    if (m.featured)                    lines.push(`⭐ Топ\\-модель`);
    if (m.age)                         lines.push(`📅 Возраст: *${m.age}* лет`);
    if (m.height)                      lines.push(`📏 Рост: *${m.height}* см`);
    if (m.weight)                      lines.push(`⚖️ Вес: *${m.weight}* кг`);
    if (m.bust && m.waist && m.hips)   lines.push(`📐 Параметры: *${m.bust}/${m.waist}/${m.hips}*`);
    if (m.shoe_size)                   lines.push(`👟 Обувь: *${esc(m.shoe_size)}*`);
    if (m.hair_color)                  lines.push(`💇 Волосы: *${esc(m.hair_color)}*`);
    if (m.eye_color)                   lines.push(`👁 Глаза: *${esc(m.eye_color)}*`);
    if (m.category)                    lines.push(`🏷 Категория: *${esc(m.category)}*`);
    if (m.city)                        lines.push(`🏙 Город: *${esc(m.city)}*`);
    if (m.instagram)                   lines.push(`📸 @${esc(m.instagram)}`);
    if (reviewCount > 0)               lines.push(`⭐ Отзывов: *${reviewCount}*`);
    if (completedOrders > 0)           lines.push(`📋 Завершено заявок: *${esc(String(completedOrders))}*`);
    const viewCount = (m.view_count || 0) + 1; // +1 for the just-incremented count
    if (viewCount > 0)                 lines.push(`👁 Просмотров: *${viewCount}*`);

    const avail   = m.available ? '🟢 Доступна для заказа' : '🔴 Временно недоступна';
    const star    = m.featured ? '⭐ ' : '';
    // Caption ≤ 1024 chars (Telegram limit for media)
    const bioEsc  = m.bio ? esc(m.bio) : '';
    const bioFits = bioEsc.slice(0, 180) + (bioEsc.length > 180 ? '…' : '');
    const breadcrumb = `🏠 Главная › 💃 Каталог › ${esc(m.name)}`;
    const captionParts = [breadcrumb, `💃 ${star}*${esc(m.name)}*`, '', ...lines, '', avail];
    if (bioFits) captionParts.push('', `_${bioFits}_`);
    const caption = captionParts.join('\n').slice(0, 1020);

    const contactBtn = m.phone || m.instagram
      ? [{ text: '📱 Получить контакт', callback_data: `model_contact_${m.id}` }]
      : [];
    const profileUrl = siteUrl(`/model/${m.id}`, { utm_campaign: 'model_card', utm_content: String(m.id) });
    const shareUrl  = `https://t.me/share/url?url=${encodeURIComponent(siteUrl('/model/' + m.id, { utm_campaign: 'share' }))}&text=${encodeURIComponent('Посмотри эту модель: ' + m.name)}`;
    const keyboard = {
      inline_keyboard: [
        m.available ? [{ text: '📝 Заказать эту модель', callback_data: `bk_model_${m.id}` }] : [],
        contactBtn,
        [{ text: m.available ? '📅 Уточнить доступность' : '📞 Узнать о доступности', callback_data: `ask_availability_${m.id}` }],
        [{ text: '❤️ В избранное', callback_data: `fav_add_${m.id}` },
         { text: '💔 Убрать',      callback_data: `fav_remove_${m.id}` }],
        [{ text: '⚖️ Сравнить', callback_data: `compare_add_${m.id}` }],
        [{ text: '🌐 Профиль', url: profileUrl },
         { text: '📤 Поделиться', url: shareUrl }],
        [{ text: '← Каталог', callback_data: 'cat_cat__0' }, { text: '🏠 Меню', callback_data: 'main_menu' }],
      ].filter(r => r.length)
    };

    // Собираем все фото: photo_main + галерея
    let galleryUrls = [];
    try { galleryUrls = JSON.parse(m.photos || '[]'); } catch {}
    if (m.photo_main && !galleryUrls.includes(m.photo_main)) {
      galleryUrls.unshift(m.photo_main);
    }

    if (galleryUrls.length >= 2) {
      // Медиагруппа — caption только на первом фото (лимит 1024 chars)
      const media = galleryUrls.slice(0, 8).map((url, i) => {
        const item = { type: 'photo', media: url };
        if (i === 0) { item.caption = caption; item.parse_mode = 'MarkdownV2'; }
        return item;
      });
      try {
        await bot.sendMediaGroup(chatId, media);
      } catch (e) {
        console.warn('[Bot] sendMediaGroup failed, fallback:', e.message);
        await safePhoto(chatId, galleryUrls[0], { caption, parse_mode: 'MarkdownV2' });
      }
      // Если bio обрезалось — показываем полностью
      if (bioEsc.length > 180) {
        await safeSend(chatId, `📝 *Описание:*\n\n_${bioEsc}_`, { parse_mode: 'MarkdownV2' });
      }
      // Кнопки отдельным сообщением (медиагруппы не поддерживают reply_markup)
      return safeSend(chatId, `📸 Фото: ${galleryUrls.length} шт\\.`, { parse_mode: 'MarkdownV2', reply_markup: keyboard });
    }

    if (m.photo_main) {
      await safePhoto(chatId, m.photo_main, { caption, parse_mode: 'MarkdownV2', reply_markup: keyboard });
      if (bioEsc.length > 180) {
        await safeSend(chatId, `📝 *Описание:*\n\n_${bioEsc}_`, { parse_mode: 'MarkdownV2' });
      }
      return;
    }
    // Нет фото — полная карточка текстом (лимит 4096)
    const fullCaption = [`💃 ${star}*${esc(m.name)}*`, '', ...lines, '', avail,
      ...(bioEsc ? ['', `📝 *Описание:*\n_${bioEsc}_`] : [])].join('\n');
    return safeSend(chatId, fullCaption, { parse_mode: 'MarkdownV2', reply_markup: keyboard });
  } catch (e) { console.error('[Bot] showModel:', e.message); }
}

function ru_plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m100 >= 11 && m100 <= 19) return many;
  if (m10 === 1) return one;
  if (m10 >= 2 && m10 <= 4) return few;
  return many;
}

// ── My orders ─────────────────────────────────────────────────────────────────

async function showMyOrders(chatId, page = 0) {
  try {
    page = parseInt(page) || 0;
    const PER_PAGE = 5;
    const totalRow = await get(
      'SELECT COUNT(*) as n FROM orders WHERE client_chat_id=?',
      [String(chatId)]
    ).catch(() => ({ n: 0 }));
    const total = totalRow.n;

    if (!total) {
      return safeSend(chatId,
        '📭 *Ваши заявки*\n\nУ вас пока нет заявок\\. Оформите первую прямо сейчас\\!',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '📝 Оформить заявку', callback_data: 'bk_start'   }],
            [{ text: '🏠 Главное меню',    callback_data: 'main_menu'   }],
          ]}
        }
      );
    }

    const orders = await query(
      `SELECT o.*,m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE o.client_chat_id=? ORDER BY o.created_at DESC LIMIT ? OFFSET ?`,
      [String(chatId), PER_PAGE, page * PER_PAGE]
    );

    let text = `📋 *Ваши заявки* \\(${total}\\):\n\n`;
    const btns = [];
    for (const o of orders) {
      text += `${STATUS_LABELS[o.status]||o.status} *${esc(o.order_number)}*\n`;
      text += `${esc(EVENT_TYPES[o.event_type]||o.event_type)}`;
      if (o.event_date) text += ` · ${esc(o.event_date)}`;
      text += '\n\n';
      const row = [{ text: `${o.order_number}  ${STATUS_LABELS[o.status]||o.status}`, callback_data: `client_order_${o.id}` }];
      if (o.status === 'completed' || o.status === 'cancelled') {
        row.push({ text: '🔁', callback_data: `repeat_order_${o.id}` });
      }
      btns.push(row);
    }

    const nav = [];
    if (page > 0)                         nav.push({ text: '◀️', callback_data: `my_orders_page_${page - 1}` });
    if ((page + 1) * PER_PAGE < total)    nav.push({ text: '▶️', callback_data: `my_orders_page_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...btns,
        ...(nav.length ? [nav] : []),
        [{ text: '📝 Новая заявка', callback_data: 'bk_start'   }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu'  }],
      ]}
    });
  } catch (e) { console.error('[Bot] showMyOrders:', e.message); }
}

async function showClientOrder(chatId, orderId) {
  try {
    const o = await get(
      `SELECT o.*,m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id=m.id WHERE o.id=?`,
      [orderId]
    );
    if (!o || o.client_chat_id !== String(chatId)) {
      return safeSend(chatId, RU.ORDER_NOT_FOUND, {
        reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] }
      });
    }
    const msgs = await query(
      'SELECT * FROM messages WHERE order_id=? ORDER BY created_at DESC LIMIT 3',
      [orderId]
    );
    const timeline = await showOrderTimeline(o);
    let text = `📋 *Заявка ${esc(o.order_number)}*\n\n`;
    text += `*Статус заявки:*\n${timeline}\n\n`;
    text += `Мероприятие: *${esc(EVENT_TYPES[o.event_type]||o.event_type)}*\n`;
    if (o.event_date)   text += `Дата: ${esc(o.event_date)}\n`;
    if (o.event_duration) text += `Продолжительность: ${o.event_duration} ч\\.\n`;
    if (o.location)     text += `Место: ${esc(o.location)}\n`;
    if (o.model_name)   text += `Модель: ${esc(o.model_name)}\n`;
    if (o.budget)       text += `Бюджет: ${esc(o.budget)}\n`;
    if (msgs.length) {
      text += `\n💬 *Последние сообщения:*\n`;
      msgs.reverse().forEach(m => {
        const who = m.sender_type === 'admin' ? '👤 Менеджер' : '🙋 Вы';
        text += `${who}: ${esc(m.content)}\n`;
      });
    }
    const repeatBtn = (o.status === 'completed' || o.status === 'cancelled')
      ? [{ text: '🔁 Повторить заявку', callback_data: `repeat_order_${o.id}` }]
      : [];
    const reviewBtn = o.status === 'completed'
      ? [{ text: '⭐ Оставить отзыв', callback_data: `leave_review_${o.id}` }]
      : [];
    // Payment info in message
    if (o.payment_status === 'paid') {
      text += `\n💳 *Оплата:* ✅ Оплачено\n`;
    } else if (o.payment_id && o.payment_status === 'pending') {
      text += `\n💳 *Оплата:* ⏳ Ожидает оплаты\n`;
    }
    // Show Pay button for confirmed orders that are not yet paid
    const payBtn = (o.status === 'confirmed' && o.payment_status !== 'paid')
      ? [{ text: '💳 Оплатить', callback_data: `pay_order_${o.id}` }]
      : [];

    const kb = [
      [{ text: '← Мои заявки', callback_data: 'my_orders' }],
      [{ text: '🏠 Меню',      callback_data: 'main_menu' }],
    ];
    if (payBtn.length)    kb.unshift(payBtn);
    if (repeatBtn.length) kb.unshift(repeatBtn);
    if (reviewBtn.length) kb.unshift(reviewBtn);

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: kb }
    });
  } catch (e) { console.error('[Bot] showClientOrder:', e.message); }
}

// ── Status check ──────────────────────────────────────────────────────────────

async function showStatusInput(chatId) {
  await setSession(chatId, 'check_status', {});
  return safeSend(chatId,
    '🔍 *Проверка статуса заявки*\n\nВведите номер вашей заявки \\(например: *NM\\-2025\\-ABCDEF*\\):',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'main_menu' }]] }
    }
  );
}

async function showOrderStatus(chatId, orderNum) {
  try {
    const o = await get(
      'SELECT o.*,m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id=m.id WHERE o.order_number=?',
      [orderNum.toUpperCase()]
    );
    if (!o) {
      return safeSend(chatId,
        `❌ Заявка *${esc(orderNum)}* не найдена\\. Проверьте номер\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '🔄 Ввести другой номер', callback_data: 'check_status' }],
            [{ text: '🏠 Главное меню',        callback_data: 'main_menu'    }],
          ]}
        }
      );
    }
    let text = `📋 *Заявка ${esc(o.order_number)}*\n\n`;
    text += `Статус: *${STATUS_LABELS[o.status]||o.status}*\n`;
    text += `Мероприятие: ${esc(EVENT_TYPES[o.event_type]||o.event_type)}\n`;
    if (o.event_date)  text += `Дата: ${esc(o.event_date)}\n`;
    if (o.model_name)  text += `Модель: ${esc(o.model_name)}\n`;
    if (o.admin_notes) text += `\n📝 Примечание: ${esc(o.admin_notes)}\n`;
    await clearSession(chatId);
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '🔄 Проверить другой', callback_data: 'check_status' }],
        [{ text: '🏠 Главное меню',     callback_data: 'main_menu'    }],
      ]}
    });
  } catch (e) { console.error('[Bot] showOrderStatus:', e.message); }
}

async function showContacts(chatId) {
  const [phone, email, addr] = await Promise.all([
    getSetting('contacts_phone').catch(() => null),
    getSetting('contacts_email').catch(() => null),
    getSetting('contacts_addr').catch(() => null),
  ]);
  const lines = [
    `📞 *Контакты Nevesty Models*`,
    ``,
    phone ? `Телефон: ${esc(phone)}` : null,
    email ? `Email: ${esc(email)}` : null,
    addr  ? `Адрес: ${esc(addr)}` : null,
    `Сайт: ${esc(SITE_URL)}`,
    ``,
    `Пн\\-Вс: 09:00 — 21:00`,
  ].filter(l => l !== null).join('\n');
  return safeSend(chatId, lines, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
  });
}

// ─── Booking wizard — 4 steps (mirrors website exactly) ──────────────────────
//
// Step 1: Choose model (optional)
// Step 2: Event details — type → date → duration → location → budget → comments
// Step 3: Client info  — name → phone → email → telegram
// Step 4: Confirm & submit

function stepHeader(step, title) {
  const dots = ['●','●','●','●'].map((d,i) => i < step ? '●' : '○').join(' ');
  return `📝 *Бронирование · Шаг ${step}/4*\n${dots}\n\n*${title}*\n\n`;
}

// STEP 1 — model selection (пропускается если модель уже выбрана)
async function bkStep1(chatId, data = {}) {
  // Если модель уже выбрана (например через кнопку «Заказать эту модель») — пропускаем
  if (data.model_id && data.model_name) {
    await safeSend(chatId,
      `✅ Модель выбрана: *${esc(data.model_name)}*`,
      { parse_mode: 'MarkdownV2' }
    );
    return bkStep2EventType(chatId, data);
  }

  await setSession(chatId, 'bk_s1', data);
  resetSessionTimer(chatId);
  try {
    const models = await query('SELECT id,name,height,hair_color FROM models WHERE available=1 ORDER BY id LIMIT 12');
    const btns = models.map(m => [{
      text: `${m.name}  ·  ${m.height}см  ·  ${m.hair_color||''}`,
      callback_data: `bk_pick_${m.id}`
    }]);
    return safeSend(chatId,
      stepHeader(1,'Выберите модель') + 'Выберите из списка или нажмите «Менеджер подберёт»:',
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          ...btns,
          [{ text: '✨ Менеджер подберёт', callback_data: 'bk_pick_any' }],
          [{ text: '❌ Отменить',           callback_data: 'bk_cancel'   }],
        ]}
      }
    );
  } catch (e) { console.error('[Bot] bkStep1:', e.message); }
}

// STEP 2a — event type
async function bkStep2EventType(chatId, data) {
  await setSession(chatId, 'bk_s2_event', data);
  resetSessionTimer(chatId);
  const btns = Object.entries(EVENT_TYPES).map(([k,v]) => [{ text: v, callback_data: `bk_etype_${k}` }]);
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') + 'Выберите тип мероприятия:',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [...btns, [{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 2b — date
async function bkStep2Date(chatId, data) {
  await setSession(chatId, 'bk_s2_date', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') +
    `✅ Тип: *${esc(EVENT_TYPES[data.event_type]||data.event_type)}*\n\nВведите дату мероприятия:\n_Пример: 25\\.06\\.2025_`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 2c — duration
async function bkStep2Duration(chatId, data) {
  await setSession(chatId, 'bk_s2_dur', data);
  resetSessionTimer(chatId);
  const row1 = DURATIONS.slice(0,4).map(h => ({ text: `${h} ч.`, callback_data: `bk_dur_${h}` }));
  const row2 = DURATIONS.slice(4).map(h => ({ text: `${h} ч.`, callback_data: `bk_dur_${h}` }));
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') + 'Выберите продолжительность мероприятия:',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [row1, row2, [{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 2d — location
async function bkStep2Location(chatId, data) {
  await setSession(chatId, 'bk_s2_loc', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') + 'Введите место проведения \\(город, адрес\\):\n_Пример: Москва, ул\\. Арбат 15_\n\n_/cancel — отменить_',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 2e — budget (optional)
async function bkStep2Budget(chatId, data) {
  await setSession(chatId, 'bk_s2_budget', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') + 'Укажите бюджет \\(необязательно\\):\n_Пример: 50 000 руб\\. или от 30 000_',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '⏭ Пропустить', callback_data: 'bk_skip_budget' }],
        [{ text: '❌ Отменить',   callback_data: 'bk_cancel'      }],
      ]}
    }
  );
}

// STEP 2f — comments (optional)
async function bkStep2Comments(chatId, data) {
  await setSession(chatId, 'bk_s2_comments', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') + 'Дополнительные пожелания \\(необязательно\\):',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '⏭ Пропустить', callback_data: 'bk_skip_comments' }],
        [{ text: '❌ Отменить',   callback_data: 'bk_cancel'        }],
      ]}
    }
  );
}

// STEP 3a — name
async function bkStep3Name(chatId, data) {
  await setSession(chatId, 'bk_s3_name', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + `_${esc(bookingProgress(1, 4))}_\n\nВведите ваше имя и фамилию:\n_Пример: Мария Иванова_\n\n_/cancel — отменить_`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 3b — phone
async function bkStep3Phone(chatId, data) {
  await setSession(chatId, 'bk_s3_phone', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + `_${esc(bookingProgress(2, 4))}_\n\nВведите номер телефона:\n_Пример: \\+7\\(999\\)123\\-45\\-67_`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '← Назад', callback_data: 'bk_back_to_name' }],
        [{ text: '❌ Отменить', callback_data: 'bk_cancel'    }],
      ]}
    }
  );
}

// STEP 3c — email (optional)
async function bkStep3Email(chatId, data) {
  await setSession(chatId, 'bk_s3_email', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + `_${esc(bookingProgress(3, 4))}_\n\nВведите email \\(необязательно\\):`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '← Назад',      callback_data: 'bk_back_to_phone' }],
        [{ text: '⏭ Пропустить', callback_data: 'bk_skip_email'    }],
        [{ text: '❌ Отменить',   callback_data: 'bk_cancel'        }],
      ]}
    }
  );
}

// STEP 3d — telegram username (optional)
async function bkStep3Telegram(chatId, data, tgUsername) {
  await setSession(chatId, 'bk_s3_tg', data);
  resetSessionTimer(chatId);
  const hint = tgUsername
    ? `_Ваш username в Telegram: @${esc(tgUsername)}_\n\n`
    : '';
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + `_${esc(bookingProgress(4, 4))}_\n\n` + hint + 'Введите Telegram username для связи \\(необязательно\\):\n_Пример: @username_',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        tgUsername ? [{ text: `✅ Использовать @${tgUsername}`, callback_data: `bk_use_tg_${tgUsername}` }] : [],
        [{ text: '← Назад',      callback_data: 'bk_back_to_email' }],
        [{ text: '⏭ Пропустить', callback_data: 'bk_skip_tg'       }],
        [{ text: '❌ Отменить',   callback_data: 'bk_cancel'        }],
      ].filter(r => r.length) }
    }
  );
}

// STEP 4 — confirmation summary (mirrors website's step 4)
async function bkStep4Confirm(chatId, data) {
  await setSession(chatId, 'bk_s4', data);
  let text = stepHeader(4,'Подтвердите заявку');
  text += `💃 Модель: *${data.model_name ? esc(data.model_name) : 'Менеджер подберёт'}*\n`;
  text += `🎭 Мероприятие: *${esc(EVENT_TYPES[data.event_type]||data.event_type)}*\n`;
  if (data.event_date)     text += `📅 Дата: ${esc(data.event_date)}\n`;
  text += `⏱ Продолжительность: ${data.event_duration||4} ч\\.\n`;
  if (data.location)       text += `📍 Место: ${esc(data.location)}\n`;
  if (data.budget)         text += `💰 Бюджет: ${esc(data.budget)}\n`;
  if (data.comments)       text += `💬 Пожелания: ${esc(data.comments)}\n`;
  text += `\n👤 Имя: *${esc(data.client_name)}*\n`;
  text += `📞 Телефон: *${esc(data.client_phone)}*\n`;
  if (data.client_email)   text += `📧 Email: ${esc(data.client_email)}\n`;
  if (data.client_telegram) text += `💬 Telegram: @${esc(data.client_telegram)}\n`;
  text += '\nВсё верно?';
  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '✅ Отправить заявку', callback_data: 'bk_submit'  }],
      [{ text: '← Изменить',          callback_data: 'bk_start'   }],
      [{ text: '❌ Отменить',          callback_data: 'bk_cancel'  }],
    ]}
  });
}

async function bkSubmit(chatId, data) {
  try {
    const orderNum = generateOrderNumber();
    await run(
      `INSERT INTO orders
        (order_number,client_name,client_phone,client_email,client_telegram,
         client_chat_id,model_id,event_type,event_date,event_duration,
         location,budget,comments,status)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'new')`,
      [
        orderNum,
        data.client_name, data.client_phone,
        data.client_email||null, data.client_telegram||null,
        String(chatId),
        data.model_id||null,
        data.event_type, data.event_date||null,
        parseInt(data.event_duration)||4,
        data.location||null, data.budget||null, data.comments||null,
      ]
    );
    const order = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]);
    await clearSession(chatId);

    // Grant "precise_choice" achievement if booking has a specific date set from the start
    if (data.event_date) {
      await grantAchievement(chatId, 'precise_choice').catch(()=>{});
    }

    await safeSend(chatId,
      `🎉 *Заявка принята\\!*\n\nНомер: *${esc(orderNum)}*\n\nМенеджер свяжется с вами в течение 1 часа для подтверждения\\.\n\nСохраните номер — по нему можно проверить статус в любое время\\.`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '📋 Мои заявки',  callback_data: 'my_orders'   }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu'  }],
        ]}
      }
    );
    if (order) notifyNewOrder(order);
  } catch (e) {
    console.error('[Bot] bkSubmit:', e.message);
    await clearSession(chatId);
    return safeSend(chatId,
      '❌ *Не удалось создать заявку\\.* Попробуйте позже или напишите менеджеру\\.',
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '💬 Написать менеджеру', callback_data: 'contact_mgr' }],
          [{ text: '🏠 Главное меню',        callback_data: 'main_menu'  }],
        ]}
      }
    );
  }
}

// ─── Admin screens ────────────────────────────────────────────────────────────

// [MOVED TO handlers/admin.js]
// async function showAdminOrders(chatId, statusFilter, page = 0) { ... }

async function showAdminOrder(chatId, orderId) {
  try {
    const o = await get(
      `SELECT o.*,m.name as model_name,a.username as manager_name
       FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       LEFT JOIN admins a ON o.manager_id=a.id
       WHERE o.id=?`,
      [orderId]
    );
    if (!o) return safeSend(chatId, RU.ORDER_NOT_FOUND);

    const [msgs, notes] = await Promise.all([
      query('SELECT * FROM messages WHERE order_id=? ORDER BY created_at DESC LIMIT 3', [orderId]),
      query('SELECT * FROM order_notes WHERE order_id=? ORDER BY created_at DESC LIMIT 3', [orderId]),
    ]);

    const breadcrumb = `🔧 Админ › 📋 Заявки › #${esc(o.order_number || String(o.id))}`;
    let text = `${breadcrumb}\n\n📋 *${esc(o.order_number)}*\nСтатус: ${esc(STATUS_LABELS[o.status]||o.status)}\n`;
    if (o.manager_name) text += `👤 Менеджер: *${esc(o.manager_name)}*\n`;
    text += `\n`;
    text += `👤 ${esc(o.client_name)}\n📞 ${esc(o.client_phone)}\n`;
    if (o.client_email)    text += `📧 ${esc(o.client_email)}\n`;
    if (o.client_telegram) text += `💬 @${esc(o.client_telegram.replace('@',''))}\n`;
    text += `\n🎭 ${esc(EVENT_TYPES[o.event_type]||o.event_type)}\n`;
    if (o.event_date)      text += `📅 ${esc(o.event_date)}\n`;
    if (o.event_duration)  text += `⏱ ${esc(o.event_duration)} ч\\.\n`;
    if (o.location)        text += `📍 ${esc(o.location)}\n`;
    if (o.model_name)      text += `💃 ${esc(o.model_name)}\n`;
    if (o.budget)          text += `💰 ${esc(o.budget)}\n`;
    if (o.comments)        text += `💬 ${esc(o.comments)}\n`;
    if (msgs.length) {
      text += `\n📨 Последние сообщения:\n`;
      msgs.reverse().forEach(m => {
        const who = m.sender_type==='admin' ? '👤' : '🙋';
        text += `${who} ${esc(m.content)}\n`;
      });
    }
    if (notes.length) {
      text += `\n📝 Заметки:\n`;
      [...notes].reverse().forEach(n => {
        const dt = n.created_at ? new Date(n.created_at).toLocaleString('ru', { timeZone: 'Europe/Moscow', day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '';
        text += `_${esc(dt)}_ ${esc(n.admin_note)}\n`;
      });
    }
    if (o.internal_note) {
      text += `\n📝 *Заметка:* ${esc(o.internal_note)}`;
    }

    const actions = [];
    if (!['confirmed','completed','cancelled'].includes(o.status))
      actions.push({ text: '✅ Подтвердить', callback_data: `adm_confirm_${orderId}` });
    if (!['reviewing','completed','cancelled'].includes(o.status))
      actions.push({ text: '🔍 В работу',    callback_data: `adm_review_${orderId}`  });
    if (!['cancelled','completed'].includes(o.status))
      actions.push({ text: '❌ Отклонить',   callback_data: `adm_reject_${orderId}`  });

    const keyboard = [];
    if (actions.length) keyboard.push(actions);
    keyboard.push([
      { text: '💬 Написать клиенту', callback_data: `adm_contact_${orderId}` },
      { text: '🏁 Завершить',        callback_data: `adm_complete_${orderId}` },
    ]);
    keyboard.push([
      { text: '👤 Назначить менеджера', callback_data: `adm_assign_mgr_${orderId}` },
      { text: '📝 Добавить заметку',    callback_data: `adm_note_${orderId}` },
    ]);
    keyboard.push([
      { text: '📋 Все заметки',         callback_data: `adm_notes_${orderId}_0` },
      { text: '🕐 История статусов',    callback_data: `adm_order_history_${orderId}` },
    ]);
    keyboard.push([
      { text: '📝 Заметка', callback_data: `adm_order_note_${orderId}` },
    ]);
    // Quick replies button — shown when order has a client chat ID
    if (o.client_chat_id) {
      keyboard.push([{ text: '⚡ Быстрые ответы', callback_data: `adm_qr_${o.client_chat_id}` }]);
    }
    keyboard.push([{ text: '← К заявкам', callback_data: 'adm_orders__0' }]);

    return safeSend(chatId, text, { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } });
  } catch (e) { console.error('[Bot] showAdminOrder:', e.message); }
}

async function showOrderStatusHistory(chatId, orderId) {
  if (!isAdmin(chatId)) return;
  try {
    const [order, history] = await Promise.all([
      get('SELECT order_number FROM orders WHERE id=?', [orderId]),
      query('SELECT * FROM order_status_history WHERE order_id=? ORDER BY created_at ASC', [orderId]),
    ]);
    if (!order) return safeSend(chatId, RU.ORDER_NOT_FOUND);

    let text = `*🕐 История статусов*\n*Заявка ${esc(order.order_number)}*\n\n`;
    if (!history.length) {
      text += '_Изменений статуса не зафиксировано_';
    } else {
      for (const h of history) {
        const dt = h.created_at ? new Date(h.created_at).toLocaleString('ru', { timeZone: 'Europe/Moscow' }) : '—';
        const oldLbl = esc(STATUS_LABELS[h.old_status] || h.old_status || '—');
        const newLbl = esc(STATUS_LABELS[h.new_status] || h.new_status || '—');
        text += `📌 ${esc(dt)}\n  ${oldLbl} → *${newLbl}*\n`;
      }
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '← К заявке', callback_data: `adm_order_${orderId}` }],
        [{ text: '← К заявкам', callback_data: 'adm_orders__0' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showOrderStatusHistory:', e.message); }
}

// [MOVED TO handlers/admin.js]
// async function showAdminStats(chatId) { ... }

async function showOrganismStatus(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [lastRun, critCount, highCount, okCount] = await Promise.all([
      get("SELECT message, created_at FROM agent_logs WHERE from_name='Orchestrator' ORDER BY created_at DESC LIMIT 1").catch(()=>null),
      get("SELECT COUNT(*) as n FROM agent_logs WHERE message LIKE '%🔴%' AND created_at > datetime('now','-1 hour')").catch(()=>({n:0})),
      get("SELECT COUNT(*) as n FROM agent_logs WHERE message LIKE '%🟠%' AND created_at > datetime('now','-1 hour')").catch(()=>({n:0})),
      get("SELECT COUNT(*) as n FROM agent_logs WHERE message LIKE '%✅%' AND created_at > datetime('now','-1 hour')").catch(()=>({n:0})),
    ]);
    const scoreMatch = lastRun?.message?.match(/Health Score:\s*(\d+)%/);
    const score = scoreMatch ? parseInt(scoreMatch[1]) : null;
    const scoreIcon = score == null ? '❓' : score >= 80 ? '💚' : score >= 60 ? '🟡' : '🔴';
    const lastTime = lastRun?.created_at ? new Date(lastRun.created_at).toLocaleString('ru') : 'Ещё не запускался';
    let text = `🌿 Живой организм агентов\n\n`;
    text += `${scoreIcon} Health Score: ${score != null ? score + '%' : 'нет данных'}\n`;
    text += `Последний запуск: ${lastTime}\n\n`;
    text += `За последний час:\n`;
    text += `🔴 Критических: ${critCount.n}\n`;
    text += `🟠 Важных: ${highCount.n}\n`;
    text += `✅ Ок: ${okCount.n}\n\n`;
    text += `25 агентов-органов непрерывно следят за здоровьем системы`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '🚀 Запустить проверку',              callback_data: 'adm_run_organism' }],
        [{ text: '🔧 Исправить всё и перепроверить',   callback_data: 'adm_fix_organism' }],
        [{ text: '📡 Фид агентов',                     callback_data: 'agent_feed_0'     }],
        [{ text: '← Панель',                           callback_data: 'admin_menu'       }],
      ]}
    });
  } catch (e) { console.error('[Bot] showOrganismStatus:', e.message); }
}

// [MOVED TO handlers/admin.js]
// async function showAdminModels(chatId, page, opts = {}) { ... }

async function showAdminModel(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена.');

    // Full order stats
    const [stats] = await query(`
      SELECT
        COUNT(*) as total_orders,
        SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
        SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled,
        AVG(CASE WHEN status='completed' THEN 1.0 ELSE NULL END) * 100 as success_rate
      FROM orders WHERE model_id=?
    `, [modelId]);

    const total      = stats?.total_orders || 0;
    const completed  = stats?.completed || 0;
    const cancelled  = stats?.cancelled || 0;
    const successRate = Math.round(stats?.success_rate || 0);
    const viewCount  = m.view_count || 0;

    let text = `💃 *${esc(m.name)}*\n\n`;
    if (m.age)        text += `🎂 Возраст: ${m.age} лет\n`;
    if (m.height)     text += `📏 Рост: ${m.height} см\n`;
    if (m.weight)     text += `⚖️ Вес: ${m.weight} кг\n`;
    if (m.bust)       text += `📐 Параметры: ${m.bust}/${m.waist}/${m.hips}\n`;
    if (m.shoe_size)  text += `👟 Обувь: ${esc(m.shoe_size)}\n`;
    if (m.hair_color) text += `💇 Волосы: ${esc(m.hair_color)}\n`;
    if (m.eye_color)  text += `👁 Глаза: ${esc(m.eye_color)}\n`;
    if (m.instagram)  text += `📸 @${esc(m.instagram)}\n`;
    text += `🏷 Категория: ${esc(MODEL_CATEGORIES[m.category]||m.category)}\n`;
    text += `Статус: ${m.available ? '🟢 Доступна' : '🔴 Недоступна'}\n`;
    text += `\n📊 Заказов: ${total} \\| ✅ Завершено: ${completed} \\| ❌ Отменено: ${cancelled}\n`;
    text += `📈 Успешность: ${successRate}%  👁 Просмотров: ${viewCount}\n`;
    if (m.bio) text += `\n_${esc(m.bio)}_`;

    const archiveBtn = m.archived
      ? { text: '📤 Восстановить', callback_data: `adm_restore_${m.id}` }
      : { text: '📦 В архив',      callback_data: `adm_archive_${m.id}` };

    const keyboard = { inline_keyboard: [
      [{ text: '✏️ Редактировать', callback_data: `adm_editmodel_${m.id}` },
       { text: m.available ? '🔴 Недоступна' : '🟢 Доступна', callback_data: `adm_toggle_${m.id}` }],
      [{ text: '📋 Дублировать', callback_data: `adm_duplicate_${m.id}` },
       { text: '⭐ ' + (m.featured ? 'Убрать из топа' : 'В топ'), callback_data: `adm_featured_${m.id}` }],
      [{ text: '📊 Статистика модели', callback_data: `adm_model_stats_${m.id}` }],
      [archiveBtn],
      [{ text: '← К моделям', callback_data: 'adm_models_p_0_name_0' }],
    ]};

    // Галерея: photo_main + photos[]
    let galleryUrls = [];
    try { galleryUrls = JSON.parse(m.photos || '[]'); } catch {}
    if (m.photo_main && !galleryUrls.includes(m.photo_main)) galleryUrls.unshift(m.photo_main);

    if (galleryUrls.length >= 2) {
      const media = galleryUrls.slice(0, 8).map((url, i, arr) => {
        const item = { type: 'photo', media: url };
        if (i === arr.length - 1) { item.caption = text; item.parse_mode = 'MarkdownV2'; }
        return item;
      });
      try {
        await bot.sendMediaGroup(chatId, media);
      } catch {
        await safePhoto(chatId, galleryUrls[0], { caption: text, parse_mode: 'MarkdownV2' });
      }
      return safeSend(chatId, `📸 Фото: ${galleryUrls.length}`, { reply_markup: keyboard });
    }
    if (m.photo_main) {
      return safePhoto(chatId, m.photo_main, { caption: text, parse_mode: 'MarkdownV2', reply_markup: keyboard });
    }
    return safeSend(chatId, text, { parse_mode: 'MarkdownV2', reply_markup: keyboard });
  } catch (e) { console.error('[Bot] showAdminModel:', e.message); }
}

async function showAgentFeed(chatId, page) {
  try {
    const total = (await get('SELECT COUNT(*) as n FROM agent_logs')).n;
    if (!total) return safeSend(chatId, '🤖 Фид агентов пуст.', {
      reply_markup: { inline_keyboard: [[{ text:'← Меню', callback_data:'admin_menu' }]] }
    });
    const logs = await query('SELECT * FROM agent_logs ORDER BY created_at DESC LIMIT 10 OFFSET ?', [page*10]);
    let text = `🤖 *Фид агентов* \\(${total}\\)\n\n`;
    logs.reverse().forEach(l => {
      const ts = new Date(l.created_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
      const msg = (l.message||'').length > 100 ? l.message.slice(0,100)+'…' : l.message;
      text += `\\[${esc(ts)}\\] *${esc(l.from_name||'Claude')}*\n${esc(msg)}\n\n`;
    });
    const nav = [];
    if (page > 0)            nav.push({ text:'◀️', callback_data:`agent_feed_${page-1}` });
    if ((page+1)*10 < total) nav.push({ text:'▶️', callback_data:`agent_feed_${page+1}` });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...(nav.length ? [nav] : []),
        [{ text:'← Меню', callback_data:'admin_menu' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAgentFeed:', e.message); }
}

async function showAgentDiscussions(chatId, period = '24h', page = 0) {
  try {
    const periodMap = { '1h': '-1 hours', '24h': '-24 hours', '7d': '-7 days', '30d': '-30 days' };
    const since = periodMap[period] || '-24 hours';
    const PAGE_SIZE = 8;

    const [totalRow, rows] = await Promise.all([
      get(`SELECT COUNT(*) as n FROM agent_discussions WHERE created_at > datetime('now', ?)`, [since]),
      query(`SELECT * FROM agent_discussions WHERE created_at > datetime('now', ?) ORDER BY created_at DESC LIMIT ? OFFSET ?`,
        [since, PAGE_SIZE, page * PAGE_SIZE]),
    ]);
    const total = totalRow?.n || 0;

    if (!rows.length) return safeSend(chatId, `💬 Обсуждений за ${period} нет — агенты ещё не запускались.`, {
      reply_markup: { inline_keyboard: [
        [{ text:'📡 Фид агентов', callback_data:'agent_feed_0' }, { text:'← Меню', callback_data:'admin_menu' }],
      ]}
    });

    const now = Date.now();
    let text = `💬 *Обсуждения агентов* \\(${period}, ${total} записей\\)\n\n`;
    rows.forEach(d => {
      const mins = Math.round((now - new Date(d.created_at).getTime()) / 60000);
      const timeStr = mins < 60 ? `${mins}м` : `${Math.round(mins/60)}ч`;
      const snippet = esc((d.message || '').slice(0, 120));
      text += `*${esc(d.from_agent||'?')}* \\(${esc(timeStr)}\\):\n_${snippet}_\n\n`;
    });

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `adm_disc_${period}_${page - 1}` });
    if ((page + 1) * PAGE_SIZE < total) nav.push({ text: '▶️', callback_data: `adm_disc_${period}_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [
          { text: period === '1h'  ? '✓1ч' : '1ч',   callback_data: 'adm_disc_1h_0'  },
          { text: period === '24h' ? '✓24ч': '24ч',  callback_data: 'adm_disc_24h_0' },
          { text: period === '7d'  ? '✓7д' : '7д',   callback_data: 'adm_disc_7d_0'  },
          { text: period === '30d' ? '✓30д': '30д',  callback_data: 'adm_disc_30d_0' },
        ],
        ...(nav.length ? [nav] : []),
        [{ text:'🔄 Обновить', callback_data:`adm_disc_${period}_${page}` }, { text:'← Меню', callback_data:'admin_menu' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAgentDiscussions:', e.message); }
}

// ─── Settings menu ────────────────────────────────────────────────────────────

async function showAdminSettings(chatId, section) {
  if (!isAdmin(chatId)) return;
  section = section || 'main';

  // ── Главное меню настроек ──────────────────────────────────────────────────
  if (section === 'main') {
    const [notifNew, notifSt, revEnabled, quickEnabled] = await Promise.all([
      getSetting('notif_new_order'), getSetting('notif_status'),
      getSetting('reviews_enabled'), getSetting('quick_booking_enabled'),
    ]);
    const text =
      `⚙️ Настройки бота и агентства\n\n` +
      `🔔 Уведомления: ${notifNew==='1'?'✅':'❌'} заявки  ${notifSt==='1'?'✅':'❌'} статусы\n` +
      `⭐ Отзывы: ${revEnabled==='0'?'❌ Выкл':'✅ Вкл'}  ⚡ Быстрая заявка: ${quickEnabled==='0'?'❌ Выкл':'✅ Вкл'}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '💬 Контакты и тексты',  callback_data: 'adm_settings_contacts' },
         { text: '🔔 Уведомления',        callback_data: 'adm_settings_notifs'   }],
        [{ text: '📋 Каталог и модели',   callback_data: 'adm_settings_catalog'  },
         { text: '🛒 Бронирование',       callback_data: 'adm_settings_booking'  }],
        [{ text: '⭐ Отзывы',             callback_data: 'adm_settings_reviews'  },
         { text: '🏙 Города',             callback_data: 'adm_settings_cities'   }],
        [{ text: '🤖 Бот и интерфейс',   callback_data: 'adm_settings_bot'      },
         { text: '📊 Лимиты и доступ',   callback_data: 'adm_settings_limits'   }],
        [{ text: '💰 Прайс-лист',         callback_data: 'adm_set_pricing'       }],
        [{ text: '← Меню',               callback_data: 'admin_menu'            }],
      ]}
    });
  }

  // ── Контакты и тексты ──────────────────────────────────────────────────────
  if (section === 'contacts') {
    const [phone, email, insta, addr, greeting, about, mgrHours, mgrReply, wa] = await Promise.all([
      getSetting('contacts_phone'), getSetting('contacts_email'),
      getSetting('contacts_insta'), getSetting('contacts_addr'),
      getSetting('greeting'), getSetting('about'),
      getSetting('manager_hours'), getSetting('manager_reply'), getSetting('contacts_whatsapp'),
    ]);
    const trunc = (s, n=40) => s ? ((s.length>n ? s.slice(0,n)+'…' : s)) : '—';
    const text =
      `💬 Контакты и тексты\n\n` +
      `📞 Телефон: ${phone||'—'}\n` +
      `📧 Email: ${email||'—'}\n` +
      `📸 Instagram: ${insta||'—'}\n` +
      `📱 WhatsApp: ${wa||'—'}\n` +
      `📍 Адрес: ${trunc(addr)}\n` +
      `📝 Приветствие: ${trunc(greeting)}\n` +
      `ℹ️ О нас: ${trunc(about)}\n` +
      `🕐 Часы менеджера: ${mgrHours||'—'}\n` +
      `💬 Авто-ответ: ${trunc(mgrReply,30)}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '📞 Телефон',      callback_data: 'adm_set_phone'     },
         { text: '📧 Email',        callback_data: 'adm_set_email'     }],
        [{ text: '📸 Instagram',    callback_data: 'adm_set_insta'     },
         { text: '📱 WhatsApp',     callback_data: 'adm_set_whatsapp'  }],
        [{ text: '📍 Адрес',        callback_data: 'adm_set_addr'      },
         { text: '🌐 Сайт URL',     callback_data: 'adm_set_site_url'  }],
        [{ text: '📝 Приветствие',  callback_data: 'adm_set_greeting'  },
         { text: 'ℹ️ О нас',        callback_data: 'adm_set_about'     }],
        [{ text: '🕐 Часы работы',  callback_data: 'adm_set_mgr_hours' },
         { text: '💬 Авто-ответ',   callback_data: 'adm_set_mgr_reply' }],
        [{ text: '← Настройки',     callback_data: 'adm_settings'      }],
      ]}
    });
  }

  // ── Уведомления ───────────────────────────────────────────────────────────
  if (section === 'notifs') {
    const [notifNew, notifSt, notifRev, notifMsg] = await Promise.all([
      getSetting('notif_new_order'), getSetting('notif_status'),
      getSetting('notif_new_review'), getSetting('notif_new_message'),
    ]);
    const on = v => v === '1' ? '✅' : '❌';
    const text =
      `🔔 Уведомления\n\n` +
      `${on(notifNew)} Новые заявки\n` +
      `${on(notifSt)} Изменения статуса\n` +
      `${on(notifRev)} Новые отзывы\n` +
      `${on(notifMsg)} Сообщения клиентов`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: notifNew==='1' ? '🔕 Заявки ВЫКЛ'    : '🔔 Заявки ВКЛ',
           callback_data: notifNew==='1' ? 'adm_notif_new_off'    : 'adm_notif_new_on'    }],
        [{ text: notifSt==='1'  ? '🔕 Статусы ВЫКЛ'   : '🔔 Статусы ВКЛ',
           callback_data: notifSt==='1'  ? 'adm_notif_st_off'     : 'adm_notif_st_on'     }],
        [{ text: notifRev==='1' ? '🔕 Отзывы ВЫКЛ'    : '🔔 Отзывы ВКЛ',
           callback_data: notifRev==='1' ? 'adm_notif_review_off' : 'adm_notif_review_on' }],
        [{ text: notifMsg==='1' ? '🔕 Сообщения ВЫКЛ' : '🔔 Сообщения ВКЛ',
           callback_data: notifMsg==='1' ? 'adm_notif_msg_off'    : 'adm_notif_msg_on'    }],
        [{ text: '← Настройки', callback_data: 'adm_settings' }],
      ]}
    });
  }

  // ── Каталог и модели ──────────────────────────────────────────────────────
  if (section === 'catalog') {
    const [perPage, sort, showCity, showBadge, catTitle] = await Promise.all([
      getSetting('catalog_per_page'), getSetting('catalog_sort'),
      getSetting('catalog_show_city'), getSetting('catalog_show_featured_badge'),
      getSetting('catalog_title'),
    ]);
    const text =
      `📋 Каталог и модели\n\n` +
      `📄 Моделей на странице: ${perPage||'5'}\n` +
      `🔃 Сортировка: ${sort==='date'?'По дате':'По рейтингу'}\n` +
      `🏙 Показывать город: ${showCity==='0'?'❌':'✅'}\n` +
      `⭐ Бейдж «Топ»: ${showBadge==='0'?'❌':'✅'}\n` +
      `📌 Заголовок: ${catTitle||'Наши модели'}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '📄 Кол-во на странице', callback_data: 'adm_set_catalog_per_page'   },
         { text: '📌 Заголовок',          callback_data: 'adm_set_catalog_title'       }],
        [{ text: sort==='date' ? '🔃 Сорт: По рейтингу' : '🔃 Сорт: По дате',
           callback_data: sort==='date' ? 'adm_catalog_sort_featured' : 'adm_catalog_sort_date' }],
        [{ text: showCity==='0' ? '🏙 Показать город' : '🏙 Скрыть город',
           callback_data: showCity==='0' ? 'adm_catalog_city_on' : 'adm_catalog_city_off' }],
        [{ text: showBadge==='0' ? '⭐ Показать бейдж' : '⭐ Скрыть бейдж',
           callback_data: showBadge==='0' ? 'adm_catalog_badge_on' : 'adm_catalog_badge_off' }],
        [{ text: '← Настройки', callback_data: 'adm_settings' }],
      ]}
    });
  }

  // ── Бронирование ──────────────────────────────────────────────────────────
  if (section === 'booking') {
    const [quickEnabled, autoConfirm, minBudget, bookingMsg, requireEmail] = await Promise.all([
      getSetting('quick_booking_enabled'), getSetting('booking_auto_confirm'),
      getSetting('booking_min_budget'), getSetting('booking_confirm_msg'),
      getSetting('booking_require_email'),
    ]);
    const text =
      `🛒 Бронирование\n\n` +
      `⚡ Быстрая заявка: ${quickEnabled==='0'?'❌ Выкл':'✅ Вкл'}\n` +
      `✅ Авто-подтверждение: ${autoConfirm==='1'?'✅ Вкл':'❌ Выкл'}\n` +
      `💰 Мин. бюджет: ${minBudget||'не задан'}\n` +
      `📧 Требовать email: ${requireEmail==='1'?'✅':'❌'}\n` +
      `💬 Сообщение после брони: ${(bookingMsg||'').slice(0,40)||'—'}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: quickEnabled==='0' ? '⚡ Быстрая заявка ВКЛ' : '⚡ Быстрая заявка ВЫКЛ',
           callback_data: quickEnabled==='0' ? 'adm_booking_quick_on' : 'adm_booking_quick_off' }],
        [{ text: autoConfirm==='1' ? '✅ Авто-подтвержд. ВЫКЛ' : '✅ Авто-подтвержд. ВКЛ',
           callback_data: autoConfirm==='1' ? 'adm_booking_autoconfirm_off' : 'adm_booking_autoconfirm_on' }],
        [{ text: requireEmail==='1' ? '📧 Email необязателен' : '📧 Email обязателен',
           callback_data: requireEmail==='1' ? 'adm_booking_email_off' : 'adm_booking_email_on' }],
        [{ text: '💰 Мин. бюджет',      callback_data: 'adm_set_booking_min_budget'  },
         { text: '💬 Сообщение',        callback_data: 'adm_set_booking_confirm_msg' }],
        [{ text: '← Настройки', callback_data: 'adm_settings' }],
      ]}
    });
  }

  // ── Отзывы ────────────────────────────────────────────────────────────────
  if (section === 'reviews') {
    const [revEnabled, revAuto, revMin, revPrompt] = await Promise.all([
      getSetting('reviews_enabled'), getSetting('reviews_auto_approve'),
      getSetting('reviews_min_completed'), getSetting('reviews_prompt_text'),
    ]);
    const text =
      `⭐ Отзывы\n\n` +
      `💬 Включены: ${revEnabled==='0'?'❌':'✅'}\n` +
      `✅ Авто-одобрение: ${revAuto==='1'?'✅':'❌'}\n` +
      `📋 Мин. завершённых заявок: ${revMin||'1'}\n` +
      `📝 Приглашение: ${(revPrompt||'').slice(0,40)||'—'}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: revEnabled==='0' ? '💬 Отзывы ВКЛ' : '💬 Отзывы ВЫКЛ',
           callback_data: revEnabled==='0' ? 'adm_reviews_on' : 'adm_reviews_off' }],
        [{ text: revAuto==='1' ? '✅ Авто-одобр. ВЫКЛ' : '✅ Авто-одобр. ВКЛ',
           callback_data: revAuto==='1' ? 'adm_reviews_auto_off' : 'adm_reviews_auto_on' }],
        [{ text: '🔢 Мин. заявок',   callback_data: 'adm_set_reviews_min'    },
         { text: '📝 Приглашение',   callback_data: 'adm_set_reviews_prompt' }],
        [{ text: '📋 Управление отзывами', callback_data: 'adm_reviews'      }],
        [{ text: '← Настройки', callback_data: 'adm_settings' }],
      ]}
    });
  }

  // ── Города ────────────────────────────────────────────────────────────────
  if (section === 'cities') {
    const cities = await getSetting('cities_list').catch(() => '');
    const cityList = cities ? cities.split(',').map(c => `• ${c.trim()}`).join('\n') : 'Не задано — фильтр по городу скрыт';
    return safeSend(chatId, `🏙 Города\n\nДоступные города для фильтра:\n\n${cityList}`, {
      reply_markup: { inline_keyboard: [
        [{ text: '✏️ Изменить список городов', callback_data: 'adm_set_cities_list' }],
        [{ text: '← Настройки', callback_data: 'adm_settings' }],
      ]}
    });
  }

  // ── Бот и интерфейс ───────────────────────────────────────────────────────
  if (section === 'bot') {
    const [welcomePhoto, menuText, wishlistEnabled, searchEnabled, botLang,
           quickBooking, reviewsEnabled, loyaltyEnabled, referralEnabled, modelStatsEnabled,
           faqEnabled, calcEnabled, bookingThanks, tgChannel] = await Promise.all([
      getSetting('welcome_photo_url'), getSetting('main_menu_text'),
      getSetting('wishlist_enabled'), getSetting('search_enabled'), getSetting('bot_language'),
      getSetting('quick_booking_enabled'), getSetting('reviews_enabled'),
      getSetting('loyalty_enabled'), getSetting('referral_enabled'), getSetting('model_stats_enabled'),
      getSetting('faq_enabled'), getSetting('calc_enabled'),
      getSetting('booking_thanks_text'), getSetting('tg_channel'),
    ]);
    const onOff = v => v === '0' ? '❌' : '✅';
    const trunc = (s, n=35) => s ? (s.length > n ? s.slice(0,n)+'…' : s) : '—';
    const text =
      `🤖 Бот и интерфейс\n\n` +
      `🌐 Язык: ${botLang||'ru'}\n` +
      `🖼 Фото приветствия: ${welcomePhoto ? '✅ Задано' : '❌ Нет'}\n` +
      `📋 Текст главного меню: ${trunc(menuText)}\n` +
      `⚡ Быстрая заявка: ${onOff(quickBooking)}  ❤️ Wishlist: ${onOff(wishlistEnabled)}\n` +
      `🔍 Поиск: ${onOff(searchEnabled)}  ⭐ Отзывы: ${onOff(reviewsEnabled)}\n` +
      `💫 Баллы: ${onOff(loyaltyEnabled)}  🎁 Реферальная: ${onOff(referralEnabled)}\n` +
      `📊 Статистика моделей: ${onOff(modelStatsEnabled)}\n` +
      `❓ FAQ: ${onOff(faqEnabled)}  🧮 Калькулятор: ${onOff(calcEnabled)}\n` +
      `📣 Telegram канал: ${tgChannel||'—'}\n` +
      `🎉 Текст после бронирования: ${trunc(bookingThanks)}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: `⚡ Быстрая заявка: ${onOff(quickBooking)}`, callback_data: 'adm_toggle_quick_booking' }],
        [{ text: `❤️ Wishlist: ${onOff(wishlistEnabled)}`,    callback_data: 'adm_toggle_wishlist'      }],
        [{ text: `🔍 Поиск: ${onOff(searchEnabled)}`,        callback_data: 'adm_toggle_search'         }],
        [{ text: `⭐ Отзывы: ${onOff(reviewsEnabled)}`,      callback_data: 'adm_toggle_reviews'        }],
        [{ text: `💫 Баллы лояльности: ${onOff(loyaltyEnabled)}`,      callback_data: 'adm_toggle_loyalty'    }],
        [{ text: `🎁 Реферальная: ${onOff(referralEnabled)}`,          callback_data: 'adm_toggle_referral'   }],
        [{ text: `📊 Статистика моделей: ${onOff(modelStatsEnabled)}`, callback_data: 'adm_toggle_model_stats'}],
        [{ text: `❓ FAQ: ${onOff(faqEnabled)}`,              callback_data: 'adm_toggle_faq'            },
         { text: `🧮 Калькулятор: ${onOff(calcEnabled)}`,    callback_data: 'adm_toggle_calc'           }],
        [{ text: '🖼 Фото приветствия', callback_data: 'adm_set_welcome_photo'  },
         { text: '📋 Текст меню',      callback_data: 'adm_set_main_menu_text'  }],
        [{ text: '🎉 Текст после бронирования', callback_data: 'adm_set_booking_thanks' }],
        [{ text: '📣 Telegram канал',           callback_data: 'adm_set_tg_channel'     }],
        [{ text: '🔙 Назад', callback_data: 'adm_settings_main' }],
      ]}
    });
  }

  // ── Лимиты и доступ ───────────────────────────────────────────────────────
  if (section === 'limits') {
    const [maxPhotos, maxOrders, msgDelay, rateLimit] = await Promise.all([
      getSetting('model_max_photos'), getSetting('client_max_active_orders'),
      getSetting('client_msg_delay_sec'), getSetting('api_rate_limit'),
    ]);
    const text =
      `📊 Лимиты и доступ\n\n` +
      `🖼 Макс. фото у модели: ${maxPhotos||'8'}\n` +
      `📋 Макс. активных заявок у клиента: ${maxOrders||'3'}\n` +
      `⏱ Мин. интервал сообщений клиента (сек): ${msgDelay||'10'}\n` +
      `🔒 API rate limit (req/min): ${rateLimit||'60'}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '🖼 Макс. фото',         callback_data: 'adm_set_model_max_photos'   },
         { text: '📋 Макс. заявок',       callback_data: 'adm_set_client_max_orders'  }],
        [{ text: '⏱ Интервал сообщений',  callback_data: 'adm_set_client_msg_delay'  },
         { text: '🔒 Rate limit',          callback_data: 'adm_set_api_rate_limit'    }],
        [{ text: '← Настройки', callback_data: 'adm_settings' }],
      ]}
    });
  }
}

// ─── Add Model wizard ─────────────────────────────────────────────────────────

async function showAddModelStep(chatId, d) {
  const step = d._step || 'name';
  const progress = { name:1, age:2, height:3, params:4, shoe:5, hair:6, eye:7, category:8, instagram:9, bio:10, photo:11 };
  const pct = Math.round((progress[step]||1)/11*100);
  const bar = '█'.repeat(Math.round(pct/10)) + '░'.repeat(10-Math.round(pct/10));

  const header = `➕ *Добавление модели* [${bar}]\n\n`;

  if (step === 'name') {
    await setSession(chatId, 'adm_mdl_name', d);
    return safeSend(chatId, header + '👤 Введите имя модели:', {
      reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'age') {
    await setSession(chatId, 'adm_mdl_age', d);
    return safeSend(chatId, header + `Имя: ${d.name}\n\n🎂 Введите возраст (лет):`, {
      reply_markup: { inline_keyboard: [[{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_age' }, { text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'height') {
    await setSession(chatId, 'adm_mdl_height', d);
    return safeSend(chatId, header + `Имя: ${d.name}\n\n📏 Введите рост (см, например: 176):`, {
      reply_markup: { inline_keyboard: [[{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_height' }, { text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'params') {
    await setSession(chatId, 'adm_mdl_params', d);
    return safeSend(chatId, header + `📐 Введите параметры в формате ОГ/ОТ/ОБ (например: 86/60/88)\nили пропустите:`, {
      reply_markup: { inline_keyboard: [[{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_params' }, { text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'shoe') {
    await setSession(chatId, 'adm_mdl_shoe', d);
    return safeSend(chatId, header + `👟 Введите размер обуви:`, {
      reply_markup: { inline_keyboard: [[{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_shoe' }, { text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'hair') {
    await setSession(chatId, 'adm_mdl_hair', d);
    const btns = MODEL_HAIR_COLORS.map(c => [{ text: c, callback_data: `adm_mdl_hair_${c}` }]);
    btns.push([{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_hair' }]);
    return safeSend(chatId, header + `💇 Выберите цвет волос:`, { reply_markup: { inline_keyboard: btns } });
  }
  if (step === 'eye') {
    await setSession(chatId, 'adm_mdl_eye', d);
    const btns = MODEL_EYE_COLORS.map(c => [{ text: c, callback_data: `adm_mdl_eye_${c}` }]);
    btns.push([{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_eye' }]);
    return safeSend(chatId, header + `👁 Выберите цвет глаз:`, { reply_markup: { inline_keyboard: btns } });
  }
  if (step === 'category') {
    await setSession(chatId, 'adm_mdl_category', d);
    const btns = Object.entries(MODEL_CATEGORIES).map(([k,v]) => [{ text: v, callback_data: `adm_mdl_cat_${k}` }]);
    return safeSend(chatId, header + `🏷 Выберите категорию:`, { reply_markup: { inline_keyboard: btns } });
  }
  if (step === 'instagram') {
    await setSession(chatId, 'adm_mdl_instagram', d);
    return safeSend(chatId, header + `📸 Введите Instagram (без @, например: anna_model):`, {
      reply_markup: { inline_keyboard: [[{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_instagram' }, { text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'bio') {
    await setSession(chatId, 'adm_mdl_bio', d);
    return safeSend(chatId, header + `📝 Введите описание/портфолио модели:`, {
      reply_markup: { inline_keyboard: [[{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_bio' }, { text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'photo') {
    await setSession(chatId, 'adm_mdl_photo', d);
    return safeSend(chatId, header + `📷 Отправьте фото модели (главное фото карточки):`, {
      reply_markup: { inline_keyboard: [[{ text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_photo' }, { text: '❌ Отмена', callback_data: 'admin_menu' }]] } });
  }
  if (step === 'confirm') {
    await setSession(chatId, 'adm_mdl_confirm', d);
    const params = d.bust ? `${d.bust}/${d.waist}/${d.hips}` : '—';
    let summary = `✅ Подтвердите добавление модели:\n\n`;
    summary += `👤 Имя: ${d.name}\n`;
    if (d.age)        summary += `🎂 Возраст: ${d.age} лет\n`;
    if (d.height)     summary += `📏 Рост: ${d.height} см\n`;
    if (d.bust)       summary += `📐 Параметры: ${params}\n`;
    if (d.shoe_size)  summary += `👟 Обувь: ${d.shoe_size}\n`;
    if (d.hair_color) summary += `💇 Волосы: ${d.hair_color}\n`;
    if (d.eye_color)  summary += `👁 Глаза: ${d.eye_color}\n`;
    if (d.category)   summary += `🏷 Категория: ${MODEL_CATEGORIES[d.category]||d.category}\n`;
    if (d.instagram)  summary += `📸 Instagram: @${d.instagram}\n`;
    if (d.bio)        summary += `📝 Описание: ${d.bio.slice(0,80)}${d.bio.length>80?'...':''}\n`;
    if (d.photo_id)   summary += `📷 Фото: ✅ загружено\n`;
    return safeSend(chatId, summary, {
      reply_markup: { inline_keyboard: [
        [{ text: '✅ Добавить модель', callback_data: 'adm_mdl_save' }],
        [{ text: '❌ Отмена',          callback_data: 'admin_menu'   }],
      ]}
    });
  }
}

async function saveNewModel(chatId, d) {
  try {
    const res = await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,instagram,category,photo_main,available)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)`,
      [d.name, d.age||null, d.height||null, d.weight||null, d.bust||null, d.waist||null, d.hips||null,
       d.shoe_size||null, d.hair_color||null, d.eye_color||null, d.bio||null, d.instagram||null,
       d.category||'fashion', d.photo_file_id||null]
    );
    await logAdminAction(chatId, 'add_model', 'model', res.id, { name: d.name });
    await clearSession(chatId);
    return safeSend(chatId, `✅ Модель «${d.name}» добавлена!\n\nID: ${res.id}`, {
      reply_markup: { inline_keyboard: [
        [{ text: '👁 Просмотреть карточку', callback_data: `adm_model_${res.id}` }],
        [{ text: '➕ Добавить ещё',          callback_data: 'adm_addmodel'         }],
        [{ text: '← Меню',                  callback_data: 'admin_menu'            }],
      ]}
    });
  } catch (e) { return safeSend(chatId, `❌ Ошибка сохранения: ${e.message}`); }
}

// ─── Edit Model ───────────────────────────────────────────────────────────────

async function showModelEditMenu(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');
  return safeSend(chatId, `✏️ *Редактировать: ${m.name}*\n\nВыберите поле:`, {
    reply_markup: { inline_keyboard: [
      [{ text: '👤 Имя',         callback_data: `adm_ef_${modelId}_name`       },
       { text: '🎂 Возраст',    callback_data: `adm_ef_${modelId}_age`        }],
      [{ text: '📏 Рост',        callback_data: `adm_ef_${modelId}_height`     },
       { text: '⚖️ Вес',        callback_data: `adm_ef_${modelId}_weight`     }],
      [{ text: '📐 Параметры',  callback_data: `adm_ef_${modelId}_params`     },
       { text: '👟 Обувь',       callback_data: `adm_ef_${modelId}_shoe_size`  }],
      [{ text: '💇 Волосы',     callback_data: `adm_ef_${modelId}_hair_color`  },
       { text: '👁 Глаза',       callback_data: `adm_ef_${modelId}_eye_color`  }],
      [{ text: '📸 Instagram',  callback_data: `adm_ef_${modelId}_instagram`  },
       { text: '🏷 Категория',  callback_data: `adm_ef_${modelId}_category`   }],
      [{ text: '📞 Телефон',    callback_data: `adm_ef_${modelId}_phone`      },
       { text: '🏙 Город',      callback_data: `adm_ef_${modelId}_city`       }],
      [{ text: '📝 Описание',   callback_data: `adm_ef_${modelId}_bio`        }],
      [{ text: '🎬 Видео URL',  callback_data: `adm_ef_${modelId}_video_url`  }],
      [{ text: '🤖 AI описание', callback_data: `adm_ai_bio_${modelId}`        }],
      [{ text: '📷 Галерея фото', callback_data: `adm_gallery_${modelId}`      }],
      [{ text: m.available ? '🔴 Недоступна' : '🟢 Доступна', callback_data: `adm_toggle_${modelId}` },
       { text: m.featured ? '⭐ Убрать из топа' : '⭐ В топ', callback_data: `adm_featured_${modelId}` }],
      [{ text: '🗑 Удалить модель', callback_data: `adm_del_model_${modelId}` }],
      [{ text: '← Карточка',   callback_data: `adm_model_${modelId}`          }],
    ]}
  });
}

// ─── Model Comparison ─────────────────────────────────────────────────────────

// In-memory compare lists per chat (up to 3 models)
const _compareLists = new Map(); // chatId → Set of modelIds

async function addToCompare(chatId, modelId) {
  const key = String(chatId);
  if (!_compareLists.has(key)) _compareLists.set(key, new Set());
  const list = _compareLists.get(key);
  if (list.has(modelId)) {
    return safeSend(chatId, '⚖️ Эта модель уже в списке сравнения\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '⚖️ Показать сравнение', callback_data: 'compare_show' }],
        [{ text: '💃 Каталог', callback_data: 'cat_cat__0' }],
      ]}
    });
  }
  if (list.size >= 3) {
    return safeSend(chatId, '⚖️ Можно сравнивать не более 3 моделей\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '⚖️ Показать сравнение', callback_data: 'compare_show' }],
        [{ text: '🗑 Очистить список',     callback_data: 'compare_clear' }],
      ]}
    });
  }
  list.add(modelId);
  const m = await get('SELECT name FROM models WHERE id=?', [modelId]).catch(() => null);
  return safeSend(chatId,
    `✅ *${esc(m?.name || String(modelId))}* добавлена в сравнение \\(${list.size}/3\\)`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '⚖️ Показать сравнение', callback_data: 'compare_show' }],
        [{ text: '💃 Продолжить каталог',  callback_data: 'cat_cat__0'   }],
        [{ text: '🗑 Очистить список',     callback_data: 'compare_clear' }],
      ]}
    }
  );
}

async function showComparison(chatId) {
  const key = String(chatId);
  const list = _compareLists.get(key);
  if (!list || list.size === 0) {
    return safeSend(chatId, '⚖️ Список сравнения пуст\\. Добавьте модели из каталога\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] }
    });
  }
  const modelIds = [...list];
  const models = await Promise.all(modelIds.map(id => get('SELECT * FROM models WHERE id=?', [id]).catch(() => null)));
  const valid = models.filter(Boolean);
  if (!valid.length) {
    _compareLists.delete(key);
    return safeSend(chatId, '⚖️ Список сравнения пуст\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] }
    });
  }

  const catMap = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };
  const pad = (s, n) => { const str = String(s ?? '—'); return str.length >= n ? str.slice(0, n) : str + ' '.repeat(n - str.length); };
  const COL = 11;
  const LABEL = 11;

  let table = '';
  table += pad('', LABEL);
  for (const m of valid) table += pad(m.name.split(' ')[0], COL);
  table += '\n';
  table += pad('Рост:', LABEL);
  for (const m of valid) table += pad(m.height ? m.height + ' см' : '—', COL);
  table += '\n';
  table += pad('Возраст:', LABEL);
  for (const m of valid) table += pad(m.age ? m.age + ' г' : '—', COL);
  table += '\n';
  table += pad('Параметры:', LABEL);
  for (const m of valid) table += pad((m.bust && m.waist && m.hips) ? `${m.bust}/${m.waist}/${m.hips}` : '—', COL);
  table += '\n';
  table += pad('Категория:', LABEL);
  for (const m of valid) table += pad(catMap[m.category] || m.category || '—', COL);
  table += '\n';
  table += pad('Статус:', LABEL);
  for (const m of valid) table += pad(m.available ? 'Свободна' : 'Занята', COL);

  const text = `⚖️ *Сравнение моделей*\n\n\`\`\`\n${table}\n\`\`\``;
  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      valid.map(m => ({ text: m.name.split(' ')[0], callback_data: `cat_model_${m.id}` })),
      [{ text: '🗑 Очистить список', callback_data: 'compare_clear' }],
      [{ text: '💃 Каталог',         callback_data: 'cat_cat__0'   }],
    ]}
  });
}

// ─── AI Bio Generator ──────────────────────────────────────────────────────────

async function generateAiBio(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT * FROM models WHERE id=?', [modelId]).catch(() => null);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');

  await safeSend(chatId, '🤖 Генерирую AI описание\\.\\.\\. Подождите 10\\-30 секунд\\.', { parse_mode: 'MarkdownV2' });

  const prompt = `Напиши профессиональное описание для модели агентства. Данные: имя ${m.name}, возраст ${m.age} лет, рост ${m.height} см, параметры ${m.bust}/${m.waist}/${m.hips}, категория ${m.category}. Напиши 2-3 предложения красиво и профессионально. Только текст описания, без заголовков.`;

  const { spawn } = require('child_process');
  let output = '';
  let errorOut = '';

  const proc = spawn('claude', ['-p', prompt, '--output-format', 'text'], {
    cwd: '/home/user/Pablo/nevesty-models',
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  proc.stdout.on('data', d => { output += d.toString(); });
  proc.stderr.on('data', d => { errorOut += d.toString(); });

  proc.on('close', async (code) => {
    const bio = output.trim();
    if (!bio || code !== 0) {
      console.error('[Bot] AI bio error:', errorOut);
      return safeSend(chatId, '❌ Ошибка генерации AI описания\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Редактировать', callback_data: `adm_editmodel_${modelId}` }]] }
      });
    }
    await setSession(chatId, `adm_ai_bio_preview_${modelId}`, { ai_bio: bio });
    return safeSend(chatId,
      `🤖 *AI описание для ${esc(m.name)}:*\n\n_${esc(bio)}_\n\nПрименить это описание?`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '✅ Применить', callback_data: `adm_ai_bio_apply_${modelId}` }],
          [{ text: '🔄 Сгенерировать ещё', callback_data: `adm_ai_bio_${modelId}` }],
          [{ text: '← Отмена', callback_data: `adm_editmodel_${modelId}` }],
        ]}
      }
    );
  });

  proc.on('error', async (err) => {
    console.error('[Bot] AI bio spawn error:', err.message);
    return safeSend(chatId, `❌ Не удалось запустить AI: ${esc(err.message)}`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Редактировать', callback_data: `adm_editmodel_${modelId}` }]] }
    });
  });
}

// ─── Photo Gallery Manager ─────────────────────────────────────────────────────

async function showPhotoGalleryManager(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT id, name, photo_main, photos FROM models WHERE id=?', [modelId]);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');
  let gallery = [];
  try { gallery = JSON.parse(m.photos || '[]'); } catch {}
  const all = m.photo_main ? [m.photo_main, ...gallery] : gallery;
  const count = all.length;
  await setSession(chatId, `adm_gallery_${modelId}`, {});
  return safeSend(chatId,
    `📷 Галерея: *${esc(m.name)}*\nФото: *${count}/8* загружено\n\nОтправляйте фото одно за другим \\(до 8 штук\\)\\.\nПервое фото станет главным\\.`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '🗑 Очистить все фото', callback_data: `adm_gallery_clear_${modelId}` }],
        [{ text: '✅ Готово',           callback_data: `adm_model_${modelId}`           }],
        [{ text: '← Редактировать',    callback_data: `adm_editmodel_${modelId}`       }],
      ]}
    }
  );
}

// ─── Audit log viewer ─────────────────────────────────────────────────────────

async function showAuditLog(chatId, page = 0) {
  if (!isAdmin(chatId)) return;
  const logs = await query(`
    SELECT al.*, a.username FROM audit_log al
    LEFT JOIN admins a ON al.admin_chat_id = a.chat_id
    ORDER BY al.created_at DESC LIMIT 10 OFFSET ?`, [page * 10]).catch(()=>[]);

  if (!logs.length) return safeSend(chatId, '📋 Журнал действий пуст\\.', { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] } });

  const actionLabels = {
    'change_order_status': '🔄 Статус заявки',
    'delete_model': '🗑 Удаление модели',
    'update_setting': '⚙️ Настройки',
    'broadcast': '📢 Рассылка',
    'add_model': '➕ Добавление модели',
    'archive_model': '📦 Архивация',
    'toggle_availability': '🟢 Доступность модели',
  };

  const lines = logs.map(l => {
    const dt = new Date(l.created_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    const action = actionLabels[l.action] || esc(l.action);
    const who = l.username ? ` \\(${esc(l.username)}\\)` : '';
    return `• *${esc(dt)}*${who} — ${action}`;
  });

  await safeSend(chatId, `📋 *Журнал действий*\n\n${lines.join('\n')}`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '← Назад', callback_data: 'admin_menu' }]
    ]}
  });
}

// ─── Broadcast ────────────────────────────────────────────────────────────────

function _bcSegmentLabel(segment) {
  if (segment === 'all')       return 'Все клиенты';
  if (segment === 'completed') return 'Завершённые заявки';
  if (segment === 'new')       return 'Новые (без заявок)';
  if (segment && segment.startsWith('city:')) return `Город: ${segment.slice(5)}`;
  return 'Все клиенты';
}

async function _getBroadcastClients(segment) {
  try {
    if (segment === 'completed') {
      return await query(
        "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND status='completed'"
      ).catch(() => []);
    }
    if (segment && segment.startsWith('city:')) {
      const city = segment.slice(5);
      return await query(
        "SELECT DISTINCT o.client_chat_id FROM orders o JOIN models m ON o.model_id=m.id WHERE m.city=? AND o.client_chat_id IS NOT NULL AND o.client_chat_id != ''",
        [city]
      ).catch(() => []);
    }
    if (segment === 'new') {
      return await query(
        "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND client_chat_id NOT IN (SELECT DISTINCT client_chat_id FROM orders WHERE status IN ('confirmed','in_progress','completed') AND client_chat_id IS NOT NULL AND client_chat_id != '')"
      ).catch(() => []);
    }
    // default: all
    return await query(
      "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != ''"
    ).catch(() => []);
  } catch { return []; }
}

async function _bcCountRecipients(segment) {
  const clients = await _getBroadcastClients(segment);
  return clients.length;
}

async function showBroadcast(chatId) {
  if (!isAdmin(chatId)) return;
  const [allCount, completedCount, newCount] = await Promise.all([
    _bcCountRecipients('all'),
    _bcCountRecipients('completed'),
    _bcCountRecipients('new'),
  ]);

  return safeSend(chatId,
    `📢 *Рассылка* — Выберите аудиторию:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [
          { text: `👥 Всем клиентам (${allCount})`,     callback_data: 'adm_bc_seg_all'       },
          { text: `✅ Завершённые (${completedCount})`,  callback_data: 'adm_bc_seg_completed' },
        ],
        [
          { text: '🏙 По городу',                       callback_data: 'adm_bc_seg_city'      },
          { text: `🆕 Новые (${newCount})`,              callback_data: 'adm_bc_seg_new'       },
        ],
        [{ text: '← Назад', callback_data: 'admin_menu' }],
      ]}
    }
  );
}

async function showBroadcastCitySelection(chatId) {
  if (!isAdmin(chatId)) return;
  const citiesSetting = await getSetting('cities_list').catch(() => '');
  let cityList = citiesSetting
    ? citiesSetting.split(',').map(c => c.trim()).filter(Boolean).slice(0, 8)
    : [];
  if (!cityList.length) {
    const rows = await query(
      "SELECT DISTINCT city FROM models WHERE city IS NOT NULL AND city != '' ORDER BY city LIMIT 8"
    ).catch(() => []);
    cityList = rows.map(r => r.city);
  }
  if (!cityList.length) {
    return safeSend(chatId, '❌ Нет городов для выбора. Добавьте города в настройках или добавьте моделям города.', {
      reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_broadcast' }]] }
    });
  }
  const cityButtons = [];
  for (let i = 0; i < cityList.length; i += 2) {
    const row = [{ text: `🏙 ${cityList[i]}`, callback_data: `adm_bc_city_${cityList[i]}` }];
    if (cityList[i + 1]) row.push({ text: `🏙 ${cityList[i + 1]}`, callback_data: `adm_bc_city_${cityList[i + 1]}` });
    cityButtons.push(row);
  }
  cityButtons.push([{ text: '← Назад', callback_data: 'adm_broadcast' }]);
  return safeSend(chatId, `🏙 *Рассылка по городу*\n\nВыберите город:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: cityButtons }
  });
}

async function _askBroadcastText(chatId, segment) {
  const label = _bcSegmentLabel(segment);
  const count = await _bcCountRecipients(segment);
  const sess  = await getSession(chatId);
  const sd    = sessionData(sess);
  await setSession(chatId, 'adm_broadcast_msg', { ...sd, broadcastSegment: segment });
  return safeSend(chatId,
    `📢 *Рассылка*\nАудитория: *${esc(label)}* \\(${count} получ\\.\\)\n\nВведите текст сообщения:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] }
    }
  );
}

async function _askBroadcastPhoto(chatId) {
  return safeSend(chatId,
    `✅ *Текст получен\\!*\n\nДобавить фото к рассылке?`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [
          { text: '🖼 Добавить фото',    callback_data: 'adm_bc_photo'    },
          { text: '▶ Отправить без фото', callback_data: 'adm_bc_send_now' },
        ],
        [{ text: '❌ Отмена', callback_data: 'adm_broadcast' }],
      ]}
    }
  );
}

async function previewBroadcast(chatId) {
  const sess = await getSession(chatId);
  const sd   = sessionData(sess);
  const segment    = sd.broadcastSegment || 'all';
  const text       = sd.broadcastText || '';
  const photoId    = sd.broadcastPhotoId || null;
  const label      = _bcSegmentLabel(segment);
  const recipients = (sd.broadcastRecipients || []);
  const count      = recipients.length;

  const headerText = `📢 *Предпросмотр рассылки:*\nАудитория: *${esc(label)}* \\(${count} получ\\.\\)\n─────`;
  await safeSend(chatId, headerText, { parse_mode: 'MarkdownV2' });

  const msgBody = text ? `📢 *Сообщение от Nevesty Models*\n\n${esc(text)}` : '📢 *Nevesty Models*';
  if (photoId) {
    await safePhoto(chatId, photoId, { caption: msgBody.slice(0, 1020), parse_mode: 'MarkdownV2' }).catch(() => {});
  } else {
    await safeSend(chatId, msgBody, { parse_mode: 'MarkdownV2' }).catch(() => {});
  }

  return safeSend(chatId, '─────\n📤 Отправить рассылку?', {
    reply_markup: { inline_keyboard: [
      [
        { text: '✅ Отправить',      callback_data: 'adm_bc_confirm'        },
        { text: '✏️ Изменить текст', callback_data: 'adm_bc_edit'           },
      ],
      [{ text: '❌ Отменить',        callback_data: 'adm_bc_cancel_preview' }],
    ]}
  });
}

async function sendBroadcast(chatId, text) {
  const sess = await getSession(chatId);
  const sd   = sessionData(sess);
  const segment = sd.broadcastSegment || 'all';
  const clients = await _getBroadcastClients(segment);
  if (!clients.length) return safeSend(chatId, '❌ Нет клиентов для рассылки.', {
    reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
  });
  const newSd = { ...sd, broadcastRecipients: clients.map(c => c.client_chat_id), broadcastText: text, broadcastPhotoId: null };
  await setSession(chatId, 'adm_broadcast_preview', newSd);
  return _askBroadcastPhoto(chatId);
}

async function doSendBroadcast(chatId) {
  const sess = await getSession(chatId);
  const sd   = sessionData(sess);
  const recipients = sd.broadcastRecipients || [];
  const text = sd.broadcastText || '';
  const photoId = sd.broadcastPhotoId || null;
  let sent = 0, failed = 0;
  for (const cid of recipients) {
    try {
      if (photoId) {
        await bot.sendPhoto(cid, photoId, {
          caption: text ? `📢 *Сообщение от Nevesty Models*\n\n${esc(text)}` : '📢 *Nevesty Models*',
          parse_mode: 'MarkdownV2',
        });
      } else {
        await bot.sendMessage(cid, `📢 *Сообщение от Nevesty Models*\n\n${esc(text)}`, { parse_mode: 'MarkdownV2' });
      }
      sent++;
    } catch { failed++; }
    await new Promise(r => setTimeout(r, 60)); // rate limit
  }
  await logAdminAction(chatId, 'broadcast', null, null, { recipients: sent });
  await clearSession(chatId);
  return safeSend(chatId,
    `📊 *Рассылка завершена\\!*\n\n✅ Доставлено: *${sent}*\n❌ Ошибок: *${failed}*`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
    }
  );
}

async function sendBroadcastWithPhoto(chatId, photoFileId, caption) {
  const sess = await getSession(chatId);
  const sd   = sessionData(sess);
  const segment = sd.broadcastSegment || 'all';
  const clients = await _getBroadcastClients(segment);
  if (!clients.length) return safeSend(chatId, '❌ Нет клиентов для рассылки.', {
    reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
  });
  const newSd = { ...sd, broadcastRecipients: clients.map(c => c.client_chat_id), broadcastPhotoId: photoFileId, broadcastText: caption };
  await setSession(chatId, 'adm_broadcast_preview', newSd);
  return previewBroadcast(chatId);
}

async function doSendBroadcastWithPhoto(chatId) {
  // Delegate to unified doSendBroadcast
  return doSendBroadcast(chatId);
}

// ─── Scheduled Broadcasts ────────────────────────────────────────────────────

async function showScheduledBroadcasts(chatId) {
  if (!isAdmin(chatId)) return;
  const broadcasts = await query(
    `SELECT * FROM scheduled_broadcasts ORDER BY scheduled_at ASC LIMIT 20`
  ).catch(() => []);

  let text = `📅 *Запланированные рассылки*\n\n`;
  if (!broadcasts.length) {
    text += '_Нет запланированных рассылок_';
  } else {
    for (const b of broadcasts) {
      const dt = b.scheduled_at ? new Date(b.scheduled_at).toLocaleString('ru', { timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
      const statusEmoji = b.status === 'sent' ? '✅' : b.status === 'cancelled' ? '❌' : '⏳';
      const segLabel = b.segment === 'completed' ? 'Завершившие' : b.segment === 'active' ? 'Активные' : 'Все';
      const stats = b.sent_count ? ` ✅${b.sent_count}` : '';
      const errStats = b.error_count ? ` ❌${b.error_count}` : '';
      text += `${statusEmoji} *${esc(dt)}* \\[${esc(segLabel)}\\]${esc(stats)}${esc(errStats)}\n${esc(String(b.text || '').slice(0, 60))}${(b.text || '').length > 60 ? '…' : ''}\n\n`;
    }
  }

  const keyboard = [];
  for (const b of broadcasts.filter(b => b.status === 'pending')) {
    keyboard.push([{ text: `❌ Отменить #${b.id} (${new Date(b.scheduled_at).toLocaleString('ru', { timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })})`, callback_data: `sched_bcast_cancel_${b.id}` }]);
  }
  keyboard.push([{ text: '➕ Создать рассылку', callback_data: 'adm_new_sched_bcast' }]);
  keyboard.push([{ text: '← Назад', callback_data: 'admin_menu' }]);

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard }
  });
}

// ─── Model Stats (admin) ──────────────────────────────────────────────────────

async function showModelStats(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT * FROM models WHERE id=?', [modelId]).catch(() => null);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');

  const [totalOrders, completedOrders, cancelledOrders, avgBudget, avgRating, topCities] = await Promise.all([
    get('SELECT COUNT(*) as n FROM orders WHERE model_id=?', [modelId]).catch(() => ({ n: 0 })),
    get("SELECT COUNT(*) as n FROM orders WHERE model_id=? AND status='completed'", [modelId]).catch(() => ({ n: 0 })),
    get("SELECT COUNT(*) as n FROM orders WHERE model_id=? AND status='cancelled'", [modelId]).catch(() => ({ n: 0 })),
    get('SELECT AVG(CAST(REPLACE(REPLACE(budget,\' \',\'\'),\'₽\',\'\') AS REAL)) as avg FROM orders WHERE model_id=? AND budget IS NOT NULL AND budget != \'\'', [modelId]).catch(() => ({ avg: null })),
    get('SELECT AVG(rating) as avg, COUNT(*) as cnt FROM reviews WHERE model_id=? AND approved=1', [modelId]).catch(() => ({ avg: null, cnt: 0 })),
    query('SELECT location, COUNT(*) as cnt FROM orders WHERE model_id=? AND location IS NOT NULL AND location != \'\' GROUP BY location ORDER BY cnt DESC LIMIT 3', [modelId]).catch(() => []),
  ]);

  let text = `📊 *Статистика модели*\n\n`;
  text += `💃 *${esc(m.name)}*\n`;
  if (m.city) text += `📍 ${esc(m.city)}\n`;
  text += `\n`;
  text += `📋 Заявок всего: *${totalOrders?.n || 0}*\n`;
  text += `✅ Завершено: *${completedOrders?.n || 0}*\n`;
  text += `❌ Отменено: *${cancelledOrders?.n || 0}*\n`;
  text += `👁 Просмотров: *${m.view_count || 0}*\n`;
  if (avgBudget?.avg) text += `💰 Средний бюджет: *${esc(Math.round(avgBudget.avg).toLocaleString('ru'))} ₽*\n`;
  if (avgRating?.cnt > 0) {
    const stars = '⭐'.repeat(Math.round(avgRating.avg));
    text += `${stars} Рейтинг: *${esc(Number(avgRating.avg).toFixed(1))}* \\(${avgRating.cnt} отзывов\\)\n`;
  } else {
    text += `⭐ Отзывов пока нет\n`;
  }
  if (topCities.length) {
    text += `\n🏙 *Топ городов:*\n`;
    for (const c of topCities) {
      text += `• ${esc(c.location)} \\(${c.cnt}\\)\n`;
    }
  }

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '← К карточке модели', callback_data: `adm_model_${modelId}` }],
      [{ text: '← Модели', callback_data: 'adm_models_0' }],
    ]}
  });
}

// ─── Quick Replies ────────────────────────────────────────────────────────────

const QUICK_REPLY_TEMPLATES = [
  '✅ Ваша заявка принята! Менеджер свяжется с вами в ближайшее время.',
  '📞 Уточним детали в ближайшее время. Пожалуйста, будьте на связи.',
  '🕐 Свяжемся с вами сегодня — ждите звонка или сообщения.',
  '💃 Предложим вам подходящую модель — уже подбираем варианты!',
];

async function showQuickReplies(chatId, clientChatId) {
  if (!isAdmin(chatId)) return;
  const keyboard = QUICK_REPLY_TEMPLATES.map((t, i) => [{
    text: t.slice(0, 50),
    callback_data: `qr_send_${i}_${clientChatId}`
  }]);
  keyboard.push([{ text: '❌ Закрыть', callback_data: 'adm_orders__0' }]);

  return safeSend(chatId, `💬 *Быстрые ответы*\nВыберите шаблон для клиента \\(ID: ${esc(String(clientChatId))}\\):`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard }
  });
}

// ─── All order notes (paginated) ──────────────────────────────────────────────

async function showAllOrderNotes(chatId, orderId, page = 0) {
  if (!isAdmin(chatId)) return;
  const LIMIT = 5;
  const [order, notes, total] = await Promise.all([
    get('SELECT order_number FROM orders WHERE id=?', [orderId]).catch(() => null),
    query('SELECT * FROM order_notes WHERE order_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?', [orderId, LIMIT, page * LIMIT]).catch(() => []),
    get('SELECT COUNT(*) as n FROM order_notes WHERE order_id=?', [orderId]).catch(() => ({ n: 0 })),
  ]);
  if (!order) return safeSend(chatId, RU.ORDER_NOT_FOUND);

  let text = `📝 *Все заметки*\nЗаявка *${esc(order.order_number)}*\n\n`;
  if (!notes.length) {
    text += '_Заметок пока нет_';
  } else {
    for (const n of notes) {
      const dt = n.created_at ? new Date(n.created_at).toLocaleString('ru', { timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
      text += `_${esc(dt)}_\n${esc(n.admin_note)}\n\n`;
    }
  }

  const nav = [];
  if (page > 0) nav.push({ text: '◀ Назад', callback_data: `adm_notes_${orderId}_${page - 1}` });
  if ((page + 1) * LIMIT < (total?.n || 0)) nav.push({ text: 'Вперёд ▶', callback_data: `adm_notes_${orderId}_${page + 1}` });
  const keyboard = [];
  if (nav.length) keyboard.push(nav);
  keyboard.push([{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]);

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard }
  });
}

// ─── Admin search orders ──────────────────────────────────────────────────────

async function showAdminSearchOrder(chatId) {
  if (!isAdmin(chatId)) return;
  await setSession(chatId, 'adm_search_order_input', {});
  return safeSend(chatId,
    `🔍 *Поиск заявки*\n\nВведите номер заявки, имя клиента или телефон:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_orders__0' }]] }
    }
  );
}

async function searchAdminOrders(chatId, query_text) {
  if (!isAdmin(chatId)) return;
  try {
    const q = query_text.trim();
    const rows = await query(
      `SELECT o.*,m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE o.order_number LIKE ? OR o.client_name LIKE ? OR o.client_phone LIKE ?
       ORDER BY o.created_at DESC LIMIT 10`,
      [`%${q}%`, `%${q}%`, `%${q}%`]
    );
    await clearSession(chatId);
    if (!rows.length) {
      return safeSend(chatId,
        `🔍 По запросу *«${esc(q)}»* заявок не найдено\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '🔍 Искать снова', callback_data: 'adm_search_order' }],
            [{ text: '← Заявки',        callback_data: 'adm_orders__0'    }],
          ]}
        }
      );
    }
    let text = `🔍 *Результаты поиска «${esc(q)}»*\n\nНайдено: ${rows.length}\n\n`;
    const btns = rows.map(o => {
      const icon = STATUS_LABELS[o.status]?.split(' ')[0] || '';
      text += `${icon} *${esc(o.order_number)}* — ${esc(o.client_name)}, ${esc(o.client_phone)}\n`;
      return [{ text: `${o.order_number}  ·  ${o.client_name}`, callback_data: `adm_order_${o.id}` }];
    });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...btns,
        [{ text: '🔍 Новый поиск', callback_data: 'adm_search_order' },
         { text: '← Заявки',       callback_data: 'adm_orders__0'    }],
      ]}
    });
  } catch (e) { console.error('[Bot] searchAdminOrders:', e.message); }
}

// ─── Admin management ─────────────────────────────────────────────────────────

async function showAdminManagement(chatId) {
  if (!isAdmin(chatId)) return;
  const dbAdmins = await query("SELECT username, telegram_id, role FROM admins").catch(()=>[]);
  let text = `👑 *Управление администраторами*\n\n`;
  text += `*Из .env (ADMIN_TELEGRAM_IDS):*\n`;
  ADMIN_IDS.forEach(id => { text += `• \`${id}\`\n`; });
  text += `\n*В базе данных:*\n`;
  dbAdmins.forEach(a => { text += `• ${a.username} (\`${a.telegram_id||'—'}\`) — ${a.role}\n`; });
  text += `\n_Чтобы добавить admin — нажмите «Добавить Telegram ID»_`;
  return safeSend(chatId, text, {
    reply_markup: { inline_keyboard: [
      [{ text: '➕ Добавить Telegram ID', callback_data: 'adm_add_admin_id' }],
      [{ text: '← Меню',                 callback_data: 'admin_menu'        }],
    ]}
  });
}

// ─── Export ───────────────────────────────────────────────────────────────────

async function showExportMenu(chatId) {
  if (!isAdmin(chatId)) return;
  return safeSend(chatId,
    `📥 *Экспорт данных*\n\nВыберите тип экспорта:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📋 Заявки (CSV)',   callback_data: 'adm_export_orders_csv'  },
         { text: '💃 Модели (CSV)',   callback_data: 'adm_export_models_csv'  }],
        [{ text: '👥 Клиенты (CSV)', callback_data: 'adm_export_clients_csv' }],
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ]}
    }
  );
}

// Keep legacy alias for existing KB_MAIN_ADMIN button
async function exportOrders(chatId) {
  return showExportMenu(chatId);
}

async function showExportOrdersMenu(chatId) {
  if (!isAdmin(chatId)) return;
  return safeSend(chatId,
    `📋 *Экспорт заявок*\n\nВыберите период:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📅 За сегодня',  callback_data: 'adm_export_today' },
         { text: '📅 За неделю',   callback_data: 'adm_export_week'  }],
        [{ text: '📅 За месяц',    callback_data: 'adm_export_month' },
         { text: '📋 Все заявки',  callback_data: 'adm_export_all'   }],
        [{ text: '← Экспорт', callback_data: 'adm_export' }],
      ]}
    }
  );
}

async function doExportOrders(chatId, period) {
  if (!isAdmin(chatId)) return;
  try {
    let dateFilter = '';
    let periodLabel = 'Все';
    if (period === 'today') {
      dateFilter = `AND date(o.created_at) = date('now')`;
      periodLabel = 'Сегодня';
    } else if (period === 'week') {
      dateFilter = `AND o.created_at >= datetime('now', '-7 days')`;
      periodLabel = 'За неделю';
    } else if (period === 'month') {
      dateFilter = `AND o.created_at >= datetime('now', '-30 days')`;
      periodLabel = 'За месяц';
    }

    const orders = await query(
      `SELECT o.order_number,o.client_name,o.client_phone,o.client_email,o.client_telegram,
              o.event_type,o.event_date,o.event_duration,o.location,o.budget,o.comments,
              o.status,o.created_at,o.manager_id,
              m.name as model_name,
              (SELECT admin_note FROM order_notes WHERE order_id=o.id ORDER BY created_at ASC LIMIT 1) as first_note
       FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE 1=1 ${dateFilter}
       ORDER BY o.created_at DESC`
    );
    const SEP = ';';
    const header = ['Номер','Клиент','Телефон','Email','Telegram','Тип события','Дата','Длит(ч)','Место','Бюджет','Комментарий','Статус','Создан','Модель','ID менеджера','Первая заметка'];
    const rows = orders.map(o => [
      o.order_number, o.client_name, o.client_phone, o.client_email||'', o.client_telegram||'',
      o.event_type, o.event_date||'', o.event_duration||'', o.location||'', o.budget||'',
      (o.comments||'').replace(/"/g,'""'), o.status,
      new Date(o.created_at).toLocaleString('ru'), o.model_name||'',
      o.manager_id||'',
      (o.first_note||'').replace(/"/g,'""'),
    ].map(v => `"${v}"`).join(SEP));
    const csv = [header.join(SEP), ...rows].join('\n');
    const buf = Buffer.from('﻿' + csv, 'utf8'); // UTF-8 BOM для Excel
    await bot.sendDocument(chatId, buf, {
      caption: `📤 Экспорт заявок (${periodLabel}) — ${orders.length} записей\n${new Date().toLocaleString('ru')}`,
    }, { filename: `orders_${period}_${Date.now()}.csv`, contentType: 'text/csv' });
  } catch (e) { return safeSend(chatId, `❌ Ошибка экспорта: ${e.message}`); }
}

async function exportModelsCSV(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const models = await query(`
      SELECT m.id, m.name, m.age, m.height, m.params, m.category, m.instagram,
             m.available, m.featured, m.view_count, m.created_at,
             COUNT(o.id) as orders_count
      FROM models m
      LEFT JOIN orders o ON o.model_id = m.id
      GROUP BY m.id
      ORDER BY m.id`);
    const SEP = ';';
    const header = ['ID','Имя','Возраст','Рост','Параметры','Категория','Instagram','Доступна','Топ','Просмотры','Заявок','Создана'];
    const rows = models.map(m => [
      m.id, m.name||'', m.age||'', m.height||'', m.params||'', m.category||'',
      m.instagram||'', m.available ? 'Да' : 'Нет', m.featured ? 'Да' : 'Нет',
      m.view_count||0, m.orders_count||0,
      m.created_at ? new Date(m.created_at).toLocaleString('ru') : '',
    ].map(v => `"${String(v).replace(/"/g,'""')}"`).join(SEP));
    const csv = [header.join(SEP), ...rows].join('\n');
    const buf = Buffer.from('﻿' + csv, 'utf8');
    await bot.sendDocument(chatId, buf, {
      caption: `💃 Экспорт моделей — ${models.length} записей\n${new Date().toLocaleString('ru')}`,
    }, { filename: `models_${Date.now()}.csv`, contentType: 'text/csv' });
  } catch (e) { return safeSend(chatId, `❌ Ошибка экспорта моделей: ${e.message}`); }
}

async function exportClientsCSV(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const clients = await query(`
      SELECT
        o.client_chat_id as chat_id,
        MAX(o.client_name) as name,
        MAX(o.client_phone) as phone,
        MAX(o.client_email) as email,
        MAX(o.client_telegram) as telegram,
        COUNT(*) as total_orders,
        SUM(CASE WHEN o.status='completed' THEN 1 ELSE 0 END) as completed,
        SUM(CASE WHEN o.status='cancelled' THEN 1 ELSE 0 END) as cancelled,
        MAX(o.created_at) as last_order
      FROM orders o
      WHERE o.client_chat_id IS NOT NULL AND o.client_chat_id != ''
      GROUP BY o.client_chat_id
      ORDER BY last_order DESC`);
    const SEP = ';';
    const header = ['Chat ID','Имя','Телефон','Email','Telegram','Всего заявок','Завершено','Отменено','Последняя заявка'];
    const rows = clients.map(c => [
      c.chat_id||'', c.name||'', c.phone||'', c.email||'', c.telegram||'',
      c.total_orders||0, c.completed||0, c.cancelled||0,
      c.last_order ? new Date(c.last_order).toLocaleString('ru') : '',
    ].map(v => `"${String(v).replace(/"/g,'""')}"`).join(SEP));
    const csv = [header.join(SEP), ...rows].join('\n');
    const buf = Buffer.from('﻿' + csv, 'utf8');
    await bot.sendDocument(chatId, buf, {
      caption: `👥 Экспорт клиентов — ${clients.length} записей\n${new Date().toLocaleString('ru')}`,
    }, { filename: `clients_${Date.now()}.csv`, contentType: 'text/csv' });
  } catch (e) { return safeSend(chatId, `❌ Ошибка экспорта клиентов: ${e.message}`); }
}

// ─── Loyalty system ───────────────────────────────────────────────────────────

const LOYALTY_LEVELS = [
  { key: 'platinum', label: '💎 Платиновый', minEarned: 5000, discount: 15 },
  { key: 'gold',     label: '🥇 Золотой',    minEarned: 2000, discount: 10 },
  { key: 'silver',   label: '🥈 Серебряный', minEarned: 500,  discount: 5  },
  { key: 'bronze',   label: '🥉 Бронзовый',  minEarned: 0,    discount: 0  },
];

function getLoyaltyLevel(totalEarned) {
  for (const lvl of LOYALTY_LEVELS) {
    if (totalEarned >= lvl.minEarned) return lvl;
  }
  return LOYALTY_LEVELS[LOYALTY_LEVELS.length - 1];
}

async function addLoyaltyPoints(chatId, points, type, description, orderId = null) {
  // Get previous state before update
  const prevLp = await get(`SELECT total_earned FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(()=>null);
  const prevLevel = prevLp ? getLoyaltyLevel(prevLp.total_earned) : null;

  await run(`INSERT INTO loyalty_points (chat_id, points, total_earned) VALUES (?,?,?)
    ON CONFLICT(chat_id) DO UPDATE SET
      points = points + excluded.points,
      total_earned = total_earned + excluded.points,
      updated_at = CURRENT_TIMESTAMP`,
    [chatId, points, points]).catch(()=>{});
  await run(`INSERT INTO loyalty_transactions (chat_id, points, type, description, order_id) VALUES (?,?,?,?,?)`,
    [chatId, points, type, description, orderId]).catch(()=>{});

  // Check for level-up notification
  if (points > 0) {
    const newLp = await get(`SELECT total_earned FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(()=>null);
    if (newLp) {
      const newLevel = getLoyaltyLevel(newLp.total_earned);
      if (prevLevel && newLevel.key !== prevLevel.key && newLevel.minEarned > prevLevel.minEarned) {
        const discountText = newLevel.discount > 0 ? ` Теперь вам доступна скидка ${newLevel.discount}% на следующую заявку\\.` : '';
        await safeSend(chatId,
          `🎉 *Поздравляем\\!* Вы достигли уровня *${esc(newLevel.label)}*\\!${discountText}`,
          { parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '💫 Мои баллы', callback_data: 'loyalty' }]] } }
        ).catch(()=>{});
      }
    }
  }
}

// ─── Achievements ─────────────────────────────────────────────────────────────

const ACHIEVEMENTS_LIST = [
  { key: 'first_order',    icon: '🥇', title: 'Первая заявка',      desc: 'Оформил первую успешную заявку' },
  { key: 'loyal_client',   icon: '🔥', title: 'Постоянный клиент',  desc: '3+ завершённых заявки' },
  { key: 'vip_client',     icon: '💎', title: 'VIP клиент',         desc: '10+ завершённых заявок' },
  { key: 'first_review',   icon: '⭐', title: 'Критик',             desc: 'Оставил первый отзыв' },
  { key: 'talkative',      icon: '💬', title: 'Общительный',        desc: 'Написал менеджеру более 5 раз' },
  { key: 'precise_choice', icon: '🎯', title: 'Точный выбор',       desc: 'Забронировал без изменений даты' },
  { key: 'traveler',       icon: '🌍', title: 'Путешественник',     desc: 'Заявки из 2+ разных городов' },
];

async function grantAchievement(chatId, achievementKey) {
  try {
    const result = await run(
      `INSERT OR IGNORE INTO achievements (chat_id, achievement_key) VALUES (?,?)`,
      [chatId, achievementKey]
    );
    if (result.changes > 0) {
      const ach = ACHIEVEMENTS_LIST.find(a => a.key === achievementKey);
      if (ach) {
        await safeSend(chatId,
          `🏆 *Новое достижение\\!*\n\n${esc(ach.icon)} *${esc(ach.title)}*\n_${esc(ach.desc)}_`,
          { parse_mode: 'MarkdownV2' }
        ).catch(()=>{});
      }
    }
  } catch {}
}

async function checkAndGrantAchievements(chatId) {
  try {
    // First order achievement
    const completedOrders = await get(
      `SELECT COUNT(*) as cnt FROM orders WHERE client_chat_id=? AND status='completed'`,
      [String(chatId)]
    ).catch(()=>null);
    const cnt = completedOrders?.cnt || 0;
    if (cnt >= 1)  await grantAchievement(chatId, 'first_order');
    if (cnt >= 3)  await grantAchievement(chatId, 'loyal_client');
    if (cnt >= 10) await grantAchievement(chatId, 'vip_client');

    // Traveler achievement — orders from 2+ different cities
    const cities = await query(
      `SELECT DISTINCT location FROM orders WHERE client_chat_id=? AND location IS NOT NULL AND location != ''`,
      [String(chatId)]
    ).catch(()=>[]);
    if (cities.length >= 2) await grantAchievement(chatId, 'traveler');

    // Talkative — 5+ messages sent to manager
    const msgCount = await get(
      `SELECT COUNT(*) as cnt FROM messages m
       JOIN orders o ON o.id = m.order_id
       WHERE o.client_chat_id=? AND m.sender_type='client'`,
      [String(chatId)]
    ).catch(()=>null);
    if ((msgCount?.cnt || 0) >= 5) await grantAchievement(chatId, 'talkative');
  } catch {}
}

async function showAchievements(chatId) {
  const earned = await query(
    `SELECT achievement_key, achieved_at FROM achievements WHERE chat_id=? ORDER BY achieved_at ASC`,
    [chatId]
  ).catch(()=>[]);

  const earnedKeys = new Set(earned.map(a => a.achievement_key));
  const earnedMap  = Object.fromEntries(earned.map(a => [a.achievement_key, a.achieved_at]));

  let text = `🏆 *Мои достижения*\n\n`;
  text += `Получено: *${earnedKeys.size}* из *${ACHIEVEMENTS_LIST.length}*\n\n`;

  for (const ach of ACHIEVEMENTS_LIST) {
    if (earnedKeys.has(ach.key)) {
      const dt = earnedMap[ach.key]
        ? new Date(earnedMap[ach.key]).toLocaleDateString('ru')
        : '';
      text += `${esc(ach.icon)} *${esc(ach.title)}* ✅\n_${esc(ach.desc)}_${dt ? `\n📅 ${esc(dt)}` : ''}\n\n`;
    } else {
      text += `🔒 *${esc(ach.title)}*\n_${esc(ach.desc)}_\n\n`;
    }
  }

  return safeSend(chatId, text.trim(), {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '💫 Мои баллы', callback_data: 'loyalty' }],
      [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
    ]}
  });
}

// ─── Loyalty Leaderboard ──────────────────────────────────────────────────────

async function showLoyaltyLeaderboard(chatId) {
  const top = await query(
    `SELECT lp.chat_id, lp.points, lp.total_earned,
            (SELECT o.client_name FROM orders o WHERE o.client_chat_id=CAST(lp.chat_id AS TEXT) ORDER BY o.created_at DESC LIMIT 1) as client_name
     FROM loyalty_points lp
     ORDER BY lp.points DESC LIMIT 10`
  ).catch(()=>[]);

  const myRankRow = await get(
    `SELECT COUNT(*) as pos FROM loyalty_points WHERE points > (SELECT COALESCE(points,0) FROM loyalty_points WHERE chat_id=?)`,
    [chatId]
  ).catch(()=>null);
  const myPos = (myRankRow?.pos ?? 0) + 1;
  const myLp  = await get(`SELECT points FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(()=>null);

  function maskName(name) {
    if (!name) return 'Клиент';
    const parts = name.trim().split(/\s+/);
    if (parts.length === 1) return parts[0][0] + '***';
    return parts[0][0] + '***' + parts[parts.length - 1][0] + '\\.';
  }

  const medals = ['🥇', '🥈', '🥉'];
  let text = `🏆 *Топ клиентов по баллам*\n\n`;

  top.forEach((row, i) => {
    const medal  = medals[i] || `${i + 1}\\.`;
    const masked = esc(maskName(row.client_name));
    const isMe   = String(row.chat_id) === String(chatId);
    const meTag  = isMe ? ' \\(вы\\)' : '';
    text += `${medal} ${masked}${meTag} — *${row.points} баллов*\n`;
  });

  text += `\n📌 Ваша позиция: *${myPos}*`;
  if (myLp) text += ` — *${myLp.points} баллов*`;

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '💫 Мои баллы', callback_data: 'loyalty' }],
      [{ text: '🏆 Мои достижения', callback_data: 'my_achievements' }],
      [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
    ]}
  });
}

async function showLoyaltyProfile(chatId) {
  const lp = await get(`SELECT * FROM loyalty_points WHERE chat_id=?`, [chatId]);
  if (!lp) {
    return safeSend(chatId, '💫 У вас пока нет баллов лояльности\\.\n\nЗаработайте баллы, оформив первую заявку\\!', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] }
    });
  }
  const level = getLoyaltyLevel(lp.total_earned);

  const transactions = await query(`SELECT * FROM loyalty_transactions WHERE chat_id=? ORDER BY created_at DESC LIMIT 5`, [chatId]);
  const txText = transactions.map(t => `${t.points > 0 ? '\\+' : ''}${esc(String(t.points))} — ${esc(t.description)}`).join('\n') || 'Нет операций';

  // Next level info
  const nextLevelIndex = LOYALTY_LEVELS.findIndex(l => l.key === level.key) - 1;
  const nextLevel = nextLevelIndex >= 0 ? LOYALTY_LEVELS[nextLevelIndex] : null;
  const toNextLine = nextLevel
    ? `До уровня *${esc(nextLevel.label)}*: *${nextLevel.minEarned - lp.total_earned} баллов*\n`
    : ``;

  const text = [
    `💫 *Ваши бонусные баллы*`,
    ``,
    `Уровень: *${esc(level.label)}*`,
    `Текущий баланс: *${lp.points} баллов*`,
    `Всего заработано: *${lp.total_earned} баллов*`,
    toNextLine.trim(),
    ``,
    `*Последние операции:*`,
    txText
  ].filter(l => l !== '').join('\n');

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '🏆 Мои достижения',  callback_data: 'my_achievements'     }],
      [{ text: '🏆 Топ клиентов',    callback_data: 'loyalty_leaderboard' }],
      [{ text: '🏠 Главное меню',    callback_data: 'main_menu'           }],
    ]}
  });
}

// ─── Referral Program ─────────────────────────────────────────────────────────

async function showReferralProgram(chatId) {
  try {
    const refCode = String(chatId);
    const botInfo = await bot.getMe();
    const refLink = `https://t.me/${botInfo.username}?start=ref${refCode}`;

    const refs = await query(`SELECT COUNT(*) as cnt FROM referrals WHERE referrer_chat_id=?`, [chatId])
      .catch(() => [{ cnt: 0 }]);
    const refCount = refs[0]?.cnt || 0;
    const points = await get(`SELECT points FROM loyalty_points WHERE chat_id=?`, [chatId])
      .catch(() => null) || { points: 0 };

    const text = [
      `🎁 *Реферальная программа*`,
      ``,
      `Приглашайте друзей и получайте бонусные баллы\\!`,
      ``,
      `*Ваша реферальная ссылка:*`,
      `\`${esc(refLink)}\``,
      ``,
      `👥 Приглашено друзей: *${refCount}*`,
      `💫 Ваш баланс: *${points.points} баллов*`,
      ``,
      `_За каждого приглашённого друга вы получаете 500 баллов, а ваш друг \\— 200 баллов\\!_`,
    `_Если реферал создаёт первую заявку \\— вам дополнительно 300 баллов\\._`
    ].join('\n');

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📋 Поделиться ссылкой', switch_inline_query: `Используй мою ссылку для записи модели: ${refLink}` }],
        [{ text: '💫 Мои баллы', callback_data: 'loyalty' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
      ]}
    });
  } catch (e) { console.error('[Bot] showReferralProgram:', e.message); }
}

// ─── Price Calculator ──────────────────────────────────────────────────────────

async function showPriceCalculator(chatId, params = {}) {
  const { models = 1, hours = 4, eventType = 'other' } = params;

  const baseRates = {
    wedding: 5000, corporate: 4000, fashion: 6000, commercial: 5000, other: 4000
  };
  const baseRate = baseRates[eventType] || 4000;
  const total = baseRate * models * (hours / 4);

  const eventLabels = {
    wedding: 'Свадьба', corporate: 'Корпоратив', fashion: 'Показ мод',
    commercial: 'Коммерческая съёмка', other: 'Другое'
  };

  const text = [
    `🧮 *Калькулятор стоимости*`,
    ``,
    `📌 Тип события: *${esc(eventLabels[eventType] || eventType)}*`,
    `👤 Моделей: *${models}*`,
    `⏱ Часов: *${hours}*`,
    ``,
    `💰 *Примерная стоимость: от ${total.toLocaleString('ru-RU')} ₽*`,
    ``,
    `_Цена ориентировочная\\. Точная стоимость обсуждается с менеджером\\._`
  ].join('\n');

  const modelsButtons = [1, 2, 3, 5].map(n => ({
    text: models === n ? `✓ ${n}` : String(n),
    callback_data: `calc_models_${n}_${hours}_${eventType}`
  }));
  const hoursButtons = [4, 8, 12, 16].map(h => ({
    text: hours === h ? `✓ ${h}ч` : `${h}ч`,
    callback_data: `calc_hours_${models}_${h}_${eventType}`
  }));
  const typeEntries = Object.entries(eventLabels);
  const typeButtons = typeEntries.slice(0, 3).map(([key, label]) => ({
    text: eventType === key ? `✓ ${label}` : label,
    callback_data: `calc_type_${models}_${hours}_${key}`
  }));
  const typeButtons2 = typeEntries.slice(3).map(([key, label]) => ({
    text: eventType === key ? `✓ ${label}` : label,
    callback_data: `calc_type_${models}_${hours}_${key}`
  }));

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '👤 Кол-во моделей:', callback_data: 'noop' }],
      modelsButtons,
      [{ text: '⏱ Часов:', callback_data: 'noop' }],
      hoursButtons,
      [{ text: '📌 Тип события:', callback_data: 'noop' }],
      typeButtons,
      ...(typeButtons2.length ? [typeButtons2] : []),
      [{ text: '📋 Оформить заявку', callback_data: 'bk_start' }],
      [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
    ]}
  });
}

// ─── Order Timeline ────────────────────────────────────────────────────────────

async function showOrderTimeline(order) {
  const statuses = ['new', 'reviewing', 'confirmed', 'in_progress', 'completed'];
  const statusEmoji = {
    new: '🆕', reviewing: '🔍', confirmed: '✅', in_progress: '🔄', completed: '🏁', cancelled: '❌'
  };
  const statusName = {
    new: 'Новая', reviewing: 'На рассмотрении', confirmed: 'Подтверждена',
    in_progress: 'В работе', completed: 'Завершена', cancelled: 'Отменена'
  };

  if (order.status === 'cancelled') {
    return `❌ Заявка отменена`;
  }

  const currentIdx = statuses.indexOf(order.status);
  const timelineLines = statuses.map((st, i) => {
    const done = i <= currentIdx;
    const current = i === currentIdx;
    const prefix = done ? (current ? '➡️' : '✅') : '⬜';
    return `${prefix} ${statusEmoji[st]} ${esc(statusName[st])}`;
  });

  return timelineLines.join('\n');
}

// ─── Admin order actions ──────────────────────────────────────────────────────

async function adminChangeStatus(chatId, orderId, newStatus) {
  try {
    // Read old status before updating
    const oldOrder = await get('SELECT status FROM orders WHERE id=?', [orderId]);
    const oldStatus = oldOrder?.status || null;

    let result;
    if (newStatus === 'confirmed') {
      result = await run("UPDATE orders SET status='confirmed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",[orderId]);
    } else if (newStatus === 'reviewing') {
      result = await run("UPDATE orders SET status='reviewing',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",[orderId]);
    } else if (newStatus === 'cancelled') {
      result = await run("UPDATE orders SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('completed','cancelled')",[orderId]);
    } else if (newStatus === 'completed') {
      result = await run("UPDATE orders SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status!='cancelled'",[orderId]);
    }

    if (!result || result.changes === 0) return safeSend(chatId,'⚠️ Заявка уже обработана.');

    // Log status change to history
    await run(
      'INSERT INTO order_status_history (order_id, old_status, new_status, changed_by) VALUES (?,?,?,?)',
      [orderId, oldStatus, newStatus, String(chatId)]
    ).catch(e => console.warn('[Bot] history log:', e.message));

    // Audit log
    await logAdminAction(chatId, 'change_order_status', 'order', orderId, { from: oldStatus, to: newStatus });

    const order = await get('SELECT * FROM orders WHERE id=?', [orderId]);
    if (order?.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, newStatus);

    // Send custom booking confirmation message from settings
    if (newStatus === 'confirmed' && order?.client_chat_id) {
      const confirmMsg = await getSetting('booking_confirm_msg');
      if (confirmMsg) {
        await bot.sendMessage(order.client_chat_id, esc(confirmMsg), { parse_mode: 'MarkdownV2' }).catch(()=>{});
      }
    }

    // Award loyalty points on order completion
    if (newStatus === 'completed' && order?.client_chat_id) {
      await addLoyaltyPoints(order.client_chat_id, 100, 'order_complete', 'Завершена заявка #' + orderId, orderId);

      // Referral first-order bonus: if this client was referred, give 300 extra points to referrer
      const refRow = await get(
        `SELECT referrer_chat_id FROM referrals WHERE referred_chat_id=?`,
        [order.client_chat_id]
      ).catch(()=>null);
      if (refRow) {
        // Only give first-order bonus once (check if referrer already got it for this client)
        const alreadyGiven = await get(
          `SELECT id FROM loyalty_transactions WHERE chat_id=? AND type='referral_first_order' AND description LIKE ?`,
          [refRow.referrer_chat_id, `%${order.client_chat_id}%`]
        ).catch(()=>null);
        if (!alreadyGiven) {
          await addLoyaltyPoints(refRow.referrer_chat_id, 300, 'referral_first_order',
            `Реферал ${order.client_chat_id} создал первую заявку`).catch(()=>{});
          await safeSend(refRow.referrer_chat_id,
            `👥 Ваш реферал создал заявку\\! *\\+300 бонусов* зачислено\\.`,
            { parse_mode: 'MarkdownV2' }).catch(()=>{});
        }
      }

      // Check and grant achievements for this client
      await checkAndGrantAchievements(order.client_chat_id).catch(()=>{});
    }

    return showAdminOrder(chatId, orderId);
  } catch (e) { console.error('[Bot] adminChangeStatus:', e.message); }
}

// ─── Init bot ─────────────────────────────────────────────────────────────────

function initBot(app) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token || token === 'your_bot_token_here') {
    console.warn('⚠️  TELEGRAM_BOT_TOKEN not set — bot disabled');
    return null;
  }

  if (WEBHOOK_URL) {
    bot = new TelegramBot(token, { webHook: false });
    const path  = '/api/tg-webhook';
    const full  = WEBHOOK_URL.replace(/\/$/, '') + path;
    bot.setWebHook(full, { secret_token: WEBHOOK_SECRET })
      .then(() => console.log(`🤖 Bot (webhook: ${full})`))
      .catch(e  => console.error('[Bot] setWebHook:', e.message));
    if (app) {
      app.post(path, (req, res) => {
        if (req.headers['x-telegram-bot-api-secret-token'] !== WEBHOOK_SECRET) return res.sendStatus(403);
        bot.processUpdate(req.body);
        res.sendStatus(200);
      });
    }
  } else {
    bot = new TelegramBot(token, { polling: true });
    console.log('🤖 Bot started (polling)');
    bot.on('polling_error', err => {
      const code = err.code || (err.response?.statusCode) || 'UNKNOWN';
      console.error(`[Bot] Polling error (${code}): ${err.message}`);
    });
  }

  // Регистрация команд в меню "/" Telegram
  bot.setMyCommands([
    { command: 'start',      description: '🏠 Главное меню' },
    { command: 'catalog',    description: '💃 Каталог моделей' },
    { command: 'booking',    description: '📋 Оформить заявку' },
    { command: 'orders',     description: '📂 Мои заявки' },
    { command: 'profile',    description: '👤 Мой профиль' },
    { command: 'wishlist',   description: '❤️ Избранные модели' },
    { command: 'calculator', description: '🧮 Калькулятор стоимости' },
    { command: 'reviews',    description: '⭐ Отзывы' },
    { command: 'faq',        description: '❓ Частые вопросы' },
    { command: 'help',       description: '🆘 Помощь' },
    { command: 'cancel',     description: '❌ Отменить действие' },
  ]).catch(e => console.warn('[Bot] setMyCommands:', e.message));

  // ── /start ─────────────────────────────────────────────────────────────────
  bot.onText(/\/start(.*)/, async (msg, match) => {
    const chatId    = msg.chat.id;
    const firstName = msg.from.first_name;
    await setSession(chatId, 'idle', {});

    // Deep-link: /start model_NNN  — прямая ссылка на карточку модели
    const ref = match[1]?.trim();
    if (ref) {
      const modelMatch = ref.match(/^model_(\d+)$/);
      if (modelMatch) {
        const modelId = parseInt(modelMatch[1]);
        const m = await get('SELECT id FROM models WHERE id=? AND available=1', [modelId]).catch(()=>null);
        if (m) return showModel(chatId, modelId);
      }
      // Deep-link: /start booking_NNN — начать бронирование модели NNN
      const bookingDeepMatch = ref.match(/^booking_(\d+)$/);
      if (bookingDeepMatch) {
        const modelId = parseInt(bookingDeepMatch[1]);
        const bm = await get('SELECT id,name FROM models WHERE id=? AND available=1', [modelId]).catch(()=>null);
        const initData = bm ? { model_id: bm.id, model_name: bm.name } : {};
        return bkStep1(chatId, initData);
      }
      // Deep-link: /start ref{code} — referral link
      const refMatch = ref.match(/^ref(\d+)$/);
      if (refMatch) {
        const referrerId = parseInt(refMatch[1]);
        if (referrerId !== chatId) {
          const existing = await get(`SELECT id FROM referrals WHERE referred_chat_id=?`, [chatId]).catch(() => null);
          if (!existing) {
            await run(`INSERT INTO referrals (referrer_chat_id, referred_chat_id) VALUES (?,?)`, [referrerId, chatId]).catch(() => {});
            await addLoyaltyPoints(referrerId, 500, 'referral', `Приглашён новый пользователь`).catch(() => {});
            await addLoyaltyPoints(chatId, 200, 'referral_welcome', `Приветственный бонус по реферальной ссылке`).catch(() => {});
            await bot.sendMessage(referrerId, `👥 По вашей реферальной ссылке зарегистрировался новый пользователь\\! *\\+500 баллов* зачислено\\.`,
              { parse_mode: 'MarkdownV2' }).catch(() => {});
          }
        }
        // Fall through to show main menu
      }
      // Deep-link: /start ORDER_NUMBER
      const order = await get('SELECT * FROM orders WHERE order_number=?', [ref]).catch(()=>null);
      if (order) {
        if (order.client_chat_id && order.client_chat_id !== String(chatId))
          return safeSend(chatId, '❌ Эта заявка уже привязана к другому чату.');
        await run('UPDATE orders SET client_chat_id=? WHERE order_number=?', [String(chatId), ref]);
        return safeSend(chatId,
          `✅ Заявка *${esc(ref)}* привязана к вашему чату\\!\n\nВы будете получать уведомления о статусе\\.`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [
              [{ text: '📋 Статус заявки', callback_data: `client_order_${order.id}` }],
              [{ text: '🏠 Главное меню',  callback_data: 'main_menu'                }],
            ]}
          }
        );
      }
    }

    if (isAdmin(chatId)) return showAdminMenu(chatId, firstName);
    await showMainMenu(chatId, firstName);
    const welcomePhoto = await getSetting('welcome_photo_url').catch(() => null);
    if (welcomePhoto) {
      await safePhoto(chatId, welcomePhoto, { caption: 'Nevesty Models' }).catch(() => {});
    }

    // Welcome follow-up for new clients (no orders yet)
    const hasOrders = await get('SELECT id FROM orders WHERE client_chat_id=? LIMIT 1', [String(chatId)]).catch(() => null);
    if (!hasOrders && !isAdmin(chatId)) {
      // Notify admins about new user
      const username = msg.from.username ? `@${msg.from.username}` : (firstName || String(chatId));
      const adminIds = await getAdminChatIds().catch(() => [...ADMIN_IDS]);
      for (const adminId of adminIds) {
        safeSend(adminId, `👤 Новый пользователь: ${esc(username)} открыл бота\\.`, { parse_mode: 'MarkdownV2' }).catch(()=>{});
      }

      // Schedule welcome follow-up hint in 1 hour
      setTimeout(async () => {
        try {
          const stillNew = await get('SELECT id FROM orders WHERE client_chat_id=? LIMIT 1', [String(chatId)]).catch(() => null);
          if (!stillNew) {
            await bot.sendMessage(chatId,
              `💡 *Подсказка*: Нажмите *Каталог* чтобы посмотреть наших моделей, или воспользуйтесь *Калькулятором* чтобы оценить стоимость вашего события\\.`, {
              parse_mode: 'MarkdownV2',
              reply_markup: { inline_keyboard: [
                [{ text: '💃 Каталог', callback_data: 'cat_cat__0' }, { text: '🧮 Калькулятор', callback_data: 'calculator' }]
              ]}
            });
          }
        } catch {}
      }, 60 * 60 * 1000); // 1 hour
    }
  });

  // ── /admin ─────────────────────────────────────────────────────────────────
  bot.onText(/\/admin/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    return showAdminMenu(msg.chat.id, msg.from.first_name);
  });

  // ── /cancel ────────────────────────────────────────────────────────────────
  bot.onText(/\/cancel/, async (msg) => {
    const chatId = msg.chat.id;
    await clearSession(chatId);
    if (isAdmin(chatId)) {
      return safeSend(chatId, '❌ Действие отменено\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
      });
    }
    const clientKb = await buildClientKeyboard();
    await safeSend(chatId, '❌ Дія скасована\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: REPLY_KB_CLIENT,
    });
    return safeSend(chatId, '↩️ *Повертаємось до головного меню\\.*\n\n_Оберіть дію нижче або скористайтесь кнопками клавіатури\\._', {
      parse_mode: 'MarkdownV2',
      reply_markup: clientKb,
    });
  });

  // ── /status ────────────────────────────────────────────────────────────────
  bot.onText(/\/status (.+)/, async (msg, match) => {
    await showOrderStatus(msg.chat.id, match[1].trim());
  });

  // ── /help ──────────────────────────────────────────────────────────────────
  bot.onText(/\/help/, async (msg) => {
    const chatId = msg.chat.id;
    const text = isAdmin(chatId)
      ? `📖 *Команды администратора:*\n\n/start — главное меню\n/cancel — отменить действие\n/help — помощь\n\nДля управления ботом используйте меню 👆`
      : `📖 *Доступні команди:*\n\n` +
        `/start — Головне меню\n` +
        `/catalog — Каталог моделей\n` +
        `/booking — Оформити заявку\n` +
        `/myorders — Мої заявки\n` +
        `/wishlist — Вибране\n` +
        `/faq — Часті запитання\n` +
        `/profile — Мій профіль\n` +
        `/cancel — Скасувати поточну дію\n` +
        `/help — Ця довідка\n\n` +
        `_Якщо щось не працює — натисніть «💬 Менеджер» в меню\\._`;
    return safeSend(chatId, text, { parse_mode: 'MarkdownV2' });
  });

  // ── /faq ───────────────────────────────────────────────────────────────────
  bot.onText(/\/faq/, async (msg) => {
    return showFaq(msg.chat.id);
  });

  // ── /profile ───────────────────────────────────────────────────────────────
  bot.onText(/\/profile/, async (msg) => {
    const chatId    = msg.chat.id;
    const firstName = msg.from.first_name;
    return showUserProfile(chatId, firstName);
  });

  // ── /catalog ───────────────────────────────────────────────────────────────
  bot.onText(/\/catalog/, async (msg) => {
    return showCatalog(msg.chat.id, null, 0);
  });

  // ── /booking ───────────────────────────────────────────────────────────────
  bot.onText(/\/booking/, async (msg) => {
    return bkStep1(msg.chat.id);
  });

  // ── /orders ────────────────────────────────────────────────────────────────
  bot.onText(/\/orders/, async (msg) => {
    return showMyOrders(msg.chat.id);
  });

  // ── /myorders (alias for /orders) ──────────────────────────────────────────
  bot.onText(/\/myorders/, async (msg) => {
    return showMyOrders(msg.chat.id);
  });

  // ── /contacts ──────────────────────────────────────────────────────────────
  bot.onText(/\/contacts/, async (msg) => {
    return showContacts(msg.chat.id);
  });

  // ── /wishlist ──────────────────────────────────────────────────────────────
  bot.onText(/^\/wishlist/, async (msg) => {
    return showFavorites(msg.chat.id, 0);
  });

  // ── /calculator ────────────────────────────────────────────────────────────
  bot.onText(/^\/calculator/, async (msg) => {
    return showPriceCalculator(msg.chat.id);
  });

  // ── /reviews ───────────────────────────────────────────────────────────────
  bot.onText(/^\/reviews/, async (msg) => {
    return showPublicReviews(msg.chat.id, 0);
  });

  // ── /msg (admin direct reply) ──────────────────────────────────────────────
  bot.onText(/\/msg (\S+) (.+)/, async (msg, match) => {
    if (!isAdmin(msg.chat.id)) return;
    const chatId   = msg.chat.id;
    const orderNum = match[1].trim().toUpperCase();
    const text     = match[2].trim();
    const order    = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]).catch(()=>null);
    if (!order) return safeSend(chatId, `❌ Заявка *${esc(orderNum)}* не найдена.`, {});
    const admin = await get('SELECT username FROM admins WHERE telegram_id=?', [String(chatId)]).catch(()=>null);
    await run('INSERT INTO messages (order_id,sender_type,sender_name,content) VALUES (?,?,?,?)',
      [order.id, 'admin', admin?.username||'Менеджер', text]);
    if (order.client_chat_id) {
      await sendMessageToClient(order.client_chat_id, order.order_number, text);
      return safeSend(chatId, `✅ Отправлено клиенту ${order.client_name}.`);
    }
    return safeSend(chatId, `⚠️ Сообщение сохранено, но клиент ещё не подключил бот.`);
  });

  // ── Callback query router ──────────────────────────────────────────────────
  bot.on('callback_query', async (q) => {
    const chatId = q.message.chat.id;
    const data   = q.data;
    try { await bot.answerCallbackQuery(q.id); } catch {}

    // ── Navigation
    if (data === 'main_menu') return isAdmin(chatId) ? showAdminMenu(chatId, q.from.first_name) : showMainMenu(chatId, q.from.first_name);
    if (data === 'admin_menu') { if (!isAdmin(chatId)) return; return showAdminMenu(chatId, q.from.first_name); }
    if (data === 'contacts')   return showContacts(chatId);
    if (data === 'faq')        return showFaq(chatId);
    if (data === 'about_us')   return showAboutUs(chatId);
    if (data === 'pricing')    return showPricing(chatId);
    if (data === 'show_pricing') {
      const pricingText = await getSetting('pricing_text').catch(() => '');
      const siteUrl = await getSetting('site_url').catch(() => 'https://nevesty-models.ru') || 'https://nevesty-models.ru';
      const msg = pricingText
        ? esc(pricingText)
        : `💰 *Стоимость услуг*\n\nПодробный прайс\\-лист доступен на сайте:\n[Смотреть цены](${esc(siteUrl + '/pricing.html')})`;
      return safeSend(chatId, msg, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '🌐 Открыть прайс', url: siteUrl + '/pricing.html' }],
          [{ text: '← Меню', callback_data: 'main_menu' }],
        ]}
      });
    }
    if (data === 'profile')    return showUserProfile(chatId, q.from.first_name);
    if (data === 'loyalty')              return showLoyaltyProfile(chatId);
    if (data === 'my_achievements')      return showAchievements(chatId);
    if (data === 'loyalty_leaderboard')  return showLoyaltyLeaderboard(chatId);
    if (data === 'referral')             return showReferralProgram(chatId);
    if (data === 'calculator') return showPriceCalculator(chatId);
    if (data === 'noop')       return; // label-only buttons
    if (data === 'my_orders')  return showMyOrders(chatId);
    if (data === 'check_status') return showStatusInput(chatId);
    if (data === 'adm_stats')    { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return showAdminStats(chatId); }
    if (data === 'adm_organism')    { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return showOrganismStatus(chatId); }
    if (data === 'adm_run_organism') {
      if (!isAdmin(chatId)) return;
      await safeSend(chatId, '🌿 Запускаю проверку организма...\n\nРезультаты придут через 1-2 минуты.', {
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_organism' }]] }
      });
      const { spawn } = require('child_process');
      const proc = spawn('node', ['agents/run-organism.js'], {
        cwd: require('path').join(__dirname),
        detached: true,
        stdio: ['ignore', 'ignore', 'pipe'],
      });
      proc.stderr.on('data', d => console.error('[Organism]', d.toString().trim()));
      proc.unref();
      return;
    }

    if (data === 'adm_fix_organism') {
      if (!isAdmin(chatId)) return;
      await safeSend(chatId,
        '🔧 *Запускаю авто-исправление и перепроверку*\n\n' +
        'Агенты:\n' +
        '1\\. 🔧 Auto Fixer — исправляет базовые проблемы\n' +
        '2\\. 🐛 Bug Hunter — проверяет код\n' +
        '3\\. 🧠 Orchestrator — полная перепроверка всех 25 агентов\n\n' +
        '_Результаты придут в чат через 2-3 минуты_',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_organism' }]] }
        }
      );
      const { spawn } = require('child_process');
      const proc = spawn('node', ['agents/fix-and-recheck.js'], {
        cwd: require('path').join(__dirname),
        detached: true,
        stdio: ['ignore', 'ignore', 'pipe'],
      });
      proc.stderr.on('data', d => console.error('[FixRecheck]', d.toString().trim()));
      proc.unref();
      return;
    }

    // ── Catalog: cat_cat_{category}_{page}
    if (data.startsWith('cat_cat_')) {
      const parts = data.replace('cat_cat_', '').split('_');
      const page  = parseInt(parts.pop()) || 0;
      const cat   = parts.join('_');
      return showCatalog(chatId, cat, page);
    }

    // ── Model detail (client)
    if (data.startsWith('cat_model_')) {
      const id = parseInt(data.replace('cat_model_',''));
      return showModel(chatId, id);
    }

    // ── Client order detail
    if (data.startsWith('client_order_')) {
      const id = parseInt(data.replace('client_order_',''));
      return showClientOrder(chatId, id);
    }
    if (data.startsWith('my_order_')) {
      const id = parseInt(data.replace('my_order_', ''));
      await bot.answerCallbackQuery(callbackQuery.id).catch(() => {});
      return showClientOrder(chatId, id);
    }

    // ── Pay order
    if (data.startsWith('pay_order_')) {
      const orderId = parseInt(data.replace('pay_order_', ''));
      const ord = await get('SELECT * FROM orders WHERE id=?', [orderId]).catch(()=>null);
      if (!ord || ord.client_chat_id !== String(chatId)) {
        return safeSend(chatId, RU.ORDER_NOT_FOUND);
      }
      if (ord.payment_status === 'paid') {
        return safeSend(chatId, '✅ Эта заявка уже оплачена\\.', { parse_mode: 'MarkdownV2' });
      }
      // Request payment via API
      try {
        const https = require('https');
        const siteUrl = SITE_URL.replace(/\/$/, '');
        const url = new URL(`${siteUrl}/api/orders/${orderId}/pay`);
        const bodyStr = JSON.stringify({ phone: ord.client_phone });
        const resp = await new Promise((resolve, reject) => {
          const req = https.request(
            { hostname: url.hostname, port: url.port || 443, path: url.pathname, method: 'POST',
              headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(bodyStr) } },
            (res) => { const ch = []; res.on('data', d => ch.push(d)); res.on('end', () => resolve({ status: res.statusCode, data: JSON.parse(Buffer.concat(ch).toString()) })); }
          );
          req.on('error', reject);
          req.write(bodyStr); req.end();
        });
        if (resp.data.error) {
          return safeSend(chatId, `❌ ${esc(resp.data.error)}`, { parse_mode: 'MarkdownV2' });
        }
        if (resp.data.payment_url) {
          return safeSend(chatId,
            `💳 *Оплата заявки ${esc(ord.order_number)}*\n\nНажмите кнопку ниже для перехода к оплате:`,
            {
              parse_mode: 'MarkdownV2',
              reply_markup: { inline_keyboard: [
                [{ text: '💳 Перейти к оплате', url: resp.data.payment_url }],
                [{ text: '← Назад к заявке', callback_data: `client_order_${orderId}` }],
              ]},
            }
          );
        } else {
          // Stripe: no hosted URL, show client_secret info
          return safeSend(chatId,
            `💳 *Оплата инициирована*\n\nID платежа: \`${esc(resp.data.payment_id || '')}\`\n\nОбратитесь к менеджеру для завершения оплаты\\.`,
            { parse_mode: 'MarkdownV2' }
          );
        }
      } catch (e) {
        console.error('[Bot] pay_order:', e.message);
        return safeSend(chatId, '❌ Ошибка при создании платежа\\. Обратитесь к менеджеру\\.', { parse_mode: 'MarkdownV2' });
      }
    }

    // ── Booking: start
    if (data === 'bk_start')  return bkStep1(chatId, {});

    // ── Booking: book from model card
    if (data.startsWith('bk_model_')) {
      const id = parseInt(data.replace('bk_model_',''));
      const m  = await get('SELECT id,name FROM models WHERE id=?', [id]).catch(()=>null);
      return bkStep1(chatId, m ? { model_id: m.id, model_name: m.name } : {});
    }

    // ── Booking: model selection step 1
    if (data.startsWith('bk_pick_')) {
      const key = data.replace('bk_pick_','');
      const d   = {};
      if (key !== 'any') {
        const m = await get('SELECT id,name FROM models WHERE id=?', [parseInt(key)]).catch(()=>null);
        if (m) { d.model_id = m.id; d.model_name = m.name; }
      }
      return bkStep2EventType(chatId, d);
    }

    // ── Booking: event type
    if (data.startsWith('bk_etype_')) {
      const session = await getSession(chatId);
      const d = sessionData(session);
      const etype = data.replace('bk_etype_','');
      if (!Object.keys(EVENT_TYPES).includes(etype)) return;
      d.event_type = etype;
      return bkStep2Date(chatId, d);
    }

    // ── Booking: duration
    if (data.startsWith('bk_dur_')) {
      const session = await getSession(chatId);
      const d = sessionData(session);
      d.event_duration = data.replace('bk_dur_','');
      return bkStep2Location(chatId, d);
    }

    // ── Booking: менеджер подберёт модель
    if (data === 'bk_pick_any') {
      const d = { model_id: null, model_name: 'Менеджер подберёт' };
      return bkStep2EventType(chatId, d);
    }

    // ── Топ-модели
    if (data.startsWith('cat_top_')) {
      const page = parseInt(data.replace('cat_top_', '')) || 0;
      return showTopModels(chatId, page);
    }

    // ── Написать менеджеру
    if (data === 'contact_mgr') return showContactManager(chatId);

    // ── Получить контакт модели
    if (data.startsWith('model_contact_')) {
      const modelId = parseInt(data.replace('model_contact_', ''));
      return showModelContact(chatId, modelId);
    }

    // ── О нас
    if (data === 'about_us') return showAboutUs(chatId);

    // ── Прайс-лист
    if (data === 'pricing') return showPricing(chatId);

    // ── Калькулятор цен: calc_models_N_H_TYPE | calc_hours_N_H_TYPE | calc_type_N_H_TYPE
    if (data.startsWith('calc_')) {
      const cm = data.match(/^calc_(models|hours|type)_(\d+)_(\d+)_(.+)$/);
      if (cm) {
        const [, , modelsStr, hoursStr, type] = cm;
        const calcModels = parseInt(modelsStr);
        const calcHours  = parseInt(hoursStr);
        const VALID_CALC_EVENT_TYPES = ['wedding', 'corporate', 'fashion', 'commercial', 'other'];
        if (!isNaN(calcModels) && !isNaN(calcHours) && VALID_CALC_EVENT_TYPES.includes(type)) {
          return showPriceCalculator(chatId, { models: calcModels, hours: calcHours, eventType: type });
        }
      }
      return;
    }

    // ── Фильтр по городу
    if (data.startsWith('cat_city_')) {
      const parts = data.replace('cat_city_', '').split('_');
      const page = parseInt(parts.pop()) || 0;
      const city = parts.join('_');
      return showCatalogByCity(chatId, city, page);
    }

    // ── Booking: skip optional fields
    if (data === 'bk_skip_budget') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep2Comments(chatId, d);
    }
    if (data === 'bk_skip_comments') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep3Name(chatId, d);
    }
    if (data === 'bk_skip_email') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep3Telegram(chatId, d, q.from.username);
    }
    if (data === 'bk_skip_tg') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep4Confirm(chatId, d);
    }
    if (data.startsWith('bk_use_tg_')) {
      const username = data.replace('bk_use_tg_','');
      const session  = await getSession(chatId);
      const d = sessionData(session);
      d.client_telegram = username;
      return bkStep4Confirm(chatId, d);
    }

    // ── Booking: submit
    if (data === 'bk_submit') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      if (q.from.username && !d.client_telegram) d.client_telegram = q.from.username;
      if (!d.client_name || !d.client_phone || !d.event_type) {
        return safeSend(chatId, '❌ Данные неполные. Начните заново — /start');
      }
      return bkSubmit(chatId, d);
    }

    // ── Booking: cancel
    if (data === 'bk_cancel') {
      clearTimeout(sessionTimers.get(chatId));
      sessionTimers.delete(chatId);
      await clearSession(chatId);
      return isAdmin(chatId) ? showAdminMenu(chatId, q.from.first_name) : showMainMenu(chatId, q.from.first_name);
    }

    // ── Session: continue / restart
    if (data === 'session_continue') {
      return safeSend(chatId,
        '✅ Хорошо, продолжаем с того места где остановились\\.',
        { parse_mode: 'MarkdownV2' }
      );
    }
    if (data === 'session_restart') {
      clearTimeout(sessionTimers.get(chatId));
      sessionTimers.delete(chatId);
      await clearSession(chatId);
      return safeSend(chatId,
        '🔄 Начинаем заново\\. Используйте кнопки меню для навигации\\.',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '📝 Оформить заявку', callback_data: 'bk_start'   }],
            [{ text: '⚡ Быстрая заявка',   callback_data: 'bk_quick'   }],
            [{ text: '🏠 Главное меню',     callback_data: 'main_menu'  }],
          ]}
        }
      );
    }

    // ── Booking: back navigation
    if (data === 'bk_back_to_name') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep3Name(chatId, d);
    }
    if (data === 'bk_back_to_phone') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep3Phone(chatId, d);
    }
    if (data === 'bk_back_to_email') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep3Email(chatId, d);
    }

    // ── Admin orders list: adm_orders_{status}_{page}
    if (data.startsWith('adm_orders_')) {
      if (!isAdmin(chatId)) return;
      const parts  = data.replace('adm_orders_','').split('_');
      const page   = parseInt(parts.pop()) || 0;
      const status = parts.join('_');
      return showAdminOrders(chatId, status, page);
    }

    // ── Admin order status history
    if (data.startsWith('adm_order_history_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_order_history_',''));
      return showOrderStatusHistory(chatId, id);
    }

    // ── Admin order internal note — delete
    if (data.startsWith('adm_order_note_del_')) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_order_note_del_', ''));
      await run('UPDATE orders SET internal_note=NULL WHERE id=?', [orderId]);
      await bot.answerCallbackQuery(q.id, { text: '✅ Заметка удалена' });
      return showAdminOrder(chatId, orderId);
    }

    // ── Admin order internal note — edit prompt
    if (data.startsWith('adm_order_note_')) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_order_note_', ''));
      const order = await get('SELECT * FROM orders WHERE id=?', [orderId]);
      if (!order) return bot.answerCallbackQuery(q.id, { text: 'Заявка не найдена' });
      await setSession(chatId, 'adm_note_order_id', { orderId });
      return safeSend(chatId, `📝 *Внутренняя заметка для заявки \\#${esc(order.order_number || String(orderId))}*\n\nТекущая: ${order.internal_note ? esc(order.internal_note) : '_нет_'}\n\nВведите новую заметку \\(до 1000 символов\\) или /cancel:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🗑 Удалить заметку', callback_data: `adm_order_note_del_${orderId}` }]] }
      });
    }

    // ── Admin order detail
    if (data.startsWith('adm_order_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_order_',''));
      return showAdminOrder(chatId, id);
    }

    // ── Admin order actions
    if (data.startsWith('adm_confirm_'))  { if (!isAdmin(chatId)) return; return adminChangeStatus(chatId, parseInt(data.replace('adm_confirm_','')), 'confirmed'); }
    if (data.startsWith('adm_review_'))   { if (!isAdmin(chatId)) return; return adminChangeStatus(chatId, parseInt(data.replace('adm_review_','')), 'reviewing'); }
    if (data.startsWith('adm_reject_'))   { if (!isAdmin(chatId)) return; return adminChangeStatus(chatId, parseInt(data.replace('adm_reject_','')), 'cancelled'); }
    if (data.startsWith('adm_complete_')) { if (!isAdmin(chatId)) return; return adminChangeStatus(chatId, parseInt(data.replace('adm_complete_','')), 'completed'); }

    if (data.startsWith('adm_contact_')) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_contact_',''));
      const order   = await get('SELECT * FROM orders WHERE id=?', [orderId]).catch(()=>null);
      if (!order) return safeSend(chatId, RU.ORDER_NOT_FOUND);
      await setSession(chatId, 'replying', { order_id: orderId, order_number: order.order_number, client_name: order.client_name });
      return safeSend(chatId,
        `💬 Введите сообщение для клиента *${order.client_name}* \\(${esc(order.order_number)}\\):\n\n_/cancel — отменить_`,
        { parse_mode: 'MarkdownV2' }
      );
    }

    // ── Admin models
    // New paginated format: adm_models_p_{page}_{sort}_{archived}
    if (data.startsWith('adm_models_p_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_models_p_', '').split('_');
      const page     = parseInt(parts[0]) || 0;
      const sort     = parts[1] || 'name';
      const archived = parts[2] === '1';
      return showAdminModels(chatId, page, { sort, archived });
    }
    // Legacy format: adm_models_{page}
    if (data.startsWith('adm_models_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('adm_models_','')) || 0;
      return showAdminModels(chatId, page, {});
    }
    // adm_models (no suffix) — main menu
    if (data === 'adm_models') {
      if (!isAdmin(chatId)) return;
      return showAdminModels(chatId, 0, {});
    }
    if (data.startsWith('adm_model_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_model_',''));
      return showAdminModel(chatId, id);
    }
    if (data.startsWith('adm_toggle_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_toggle_',''));
      const m  = await get('SELECT available FROM models WHERE id=?', [id]).catch(()=>null);
      if (m) await run('UPDATE models SET available=? WHERE id=?', [m.available ? 0 : 1, id]);
      return showAdminModel(chatId, id);
    }
    if (data.startsWith('adm_featured_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_featured_',''));
      const m  = await get('SELECT featured FROM models WHERE id=?', [id]).catch(()=>null);
      if (m) await run('UPDATE models SET featured=? WHERE id=?', [m.featured ? 0 : 1, id]);
      await bot.answerCallbackQuery(q.id, { text: m?.featured ? '⭐ Убрано из топа' : '⭐ Добавлено в топ' }).catch(()=>{});
      return showAdminModel(chatId, id);
    }
    // ── Archive / Restore model
    if (data.startsWith('adm_archive_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_archive_', ''));
      await run('UPDATE models SET archived=1, available=0 WHERE id=?', [id]);
      await logAdminAction(chatId, 'archive_model', 'model', id);
      await bot.answerCallbackQuery(q.id, { text: '📦 Модель перемещена в архив' }).catch(()=>{});
      return showAdminModels(chatId, 0, {});
    }
    if (data.startsWith('adm_restore_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_restore_', ''));
      await run('UPDATE models SET archived=0 WHERE id=?', [id]);
      await logAdminAction(chatId, 'restore_model', 'model', id);
      await bot.answerCallbackQuery(q.id, { text: '✅ Модель восстановлена из архива' }).catch(()=>{});
      return showAdminModels(chatId, 0, { archived: true });
    }
    // ── Duplicate model
    if (data.startsWith('adm_duplicate_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_duplicate_', ''));
      const m  = await get('SELECT * FROM models WHERE id=?', [id]);
      if (!m) return;
      const { id: newId } = await run(
        `INSERT INTO models (name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color,
          bio, instagram, phone, category, city, featured, available, archived, photos)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?)`,
        [m.name + ' (копия)', m.age, m.height, m.weight, m.bust, m.waist, m.hips,
         m.shoe_size, m.hair_color, m.eye_color, m.bio, m.instagram, m.phone,
         m.category, m.city, m.photos]
      );
      await bot.answerCallbackQuery(q.id, { text: `✅ Создана копия: ID ${newId}` }).catch(()=>{});
      return safeSend(chatId, `✅ Модель *${esc(m.name)}* скопирована\\.\nНовый ID: *${newId}*\n\nОтредактируйте детали новой карточки\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '✏️ Редактировать копию', callback_data: `adm_model_${newId}` }]] }
      });
    }
    // ── Search model by name
    if (data === 'adm_search_model') {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, 'adm_search_model_input', {});
      return safeSend(chatId, '🔍 Введите имя или часть имени модели:', {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_models_p_0_name_0' }]] }
      });
    }

    // ── Settings
    if (data === 'adm_settings')  { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return showAdminSettings(chatId, 'main'); }
    // Подразделы настроек
    if (data === 'adm_settings_contacts') { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'contacts'); }
    if (data === 'adm_settings_notifs')   { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'notifs');   }
    if (data === 'adm_settings_catalog')  { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'catalog');  }
    if (data === 'adm_settings_booking')  { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'booking');  }
    if (data === 'adm_settings_reviews')  { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'reviews');  }
    if (data === 'adm_settings_cities')   { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'cities');   }
    if (data === 'adm_settings_bot')      { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'bot');      }
    if (data === 'adm_settings_limits')   { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'limits');   }
    // Toggle настройки каталога
    if (data === 'adm_catalog_sort_date')     { if (!isAdmin(chatId)) return; await setSetting('catalog_sort','date');     return showAdminSettings(chatId,'catalog'); }
    if (data === 'adm_catalog_sort_featured') { if (!isAdmin(chatId)) return; await setSetting('catalog_sort','featured'); return showAdminSettings(chatId,'catalog'); }
    if (data === 'adm_catalog_city_on')       { if (!isAdmin(chatId)) return; await setSetting('catalog_show_city','1');   return showAdminSettings(chatId,'catalog'); }
    if (data === 'adm_catalog_city_off')      { if (!isAdmin(chatId)) return; await setSetting('catalog_show_city','0');   return showAdminSettings(chatId,'catalog'); }
    if (data === 'adm_catalog_badge_on')      { if (!isAdmin(chatId)) return; await setSetting('catalog_show_featured_badge','1'); return showAdminSettings(chatId,'catalog'); }
    if (data === 'adm_catalog_badge_off')     { if (!isAdmin(chatId)) return; await setSetting('catalog_show_featured_badge','0'); return showAdminSettings(chatId,'catalog'); }
    // Toggle настройки бронирования
    if (data === 'adm_booking_quick_on')        { if (!isAdmin(chatId)) return; await setSetting('quick_booking_enabled','1');  return showAdminSettings(chatId,'booking'); }
    if (data === 'adm_booking_quick_off')       { if (!isAdmin(chatId)) return; await setSetting('quick_booking_enabled','0');  return showAdminSettings(chatId,'booking'); }
    if (data === 'adm_booking_autoconfirm_on')  { if (!isAdmin(chatId)) return; await setSetting('booking_auto_confirm','1');   return showAdminSettings(chatId,'booking'); }
    if (data === 'adm_booking_autoconfirm_off') { if (!isAdmin(chatId)) return; await setSetting('booking_auto_confirm','0');   return showAdminSettings(chatId,'booking'); }
    if (data === 'adm_booking_email_on')        { if (!isAdmin(chatId)) return; await setSetting('booking_require_email','1');  return showAdminSettings(chatId,'booking'); }
    if (data === 'adm_booking_email_off')       { if (!isAdmin(chatId)) return; await setSetting('booking_require_email','0');  return showAdminSettings(chatId,'booking'); }
    // Toggle настройки отзывов
    if (data === 'adm_reviews_on')       { if (!isAdmin(chatId)) return; await setSetting('reviews_enabled','1');        return showAdminSettings(chatId,'reviews'); }
    if (data === 'adm_reviews_off')      { if (!isAdmin(chatId)) return; await setSetting('reviews_enabled','0');        return showAdminSettings(chatId,'reviews'); }
    if (data === 'adm_reviews_auto_on')  { if (!isAdmin(chatId)) return; await setSetting('reviews_auto_approve','1');   return showAdminSettings(chatId,'reviews'); }
    if (data === 'adm_reviews_auto_off') { if (!isAdmin(chatId)) return; await setSetting('reviews_auto_approve','0');   return showAdminSettings(chatId,'reviews'); }
    // Toggle настройки бота
    if (data === 'adm_wishlist_on')  { if (!isAdmin(chatId)) return; await setSetting('wishlist_enabled','1'); return showAdminSettings(chatId,'bot'); }
    if (data === 'adm_wishlist_off') { if (!isAdmin(chatId)) return; await setSetting('wishlist_enabled','0'); return showAdminSettings(chatId,'bot'); }
    if (data === 'adm_search_on')    { if (!isAdmin(chatId)) return; await setSetting('search_enabled','1');   return showAdminSettings(chatId,'bot'); }
    if (data === 'adm_search_off')   { if (!isAdmin(chatId)) return; await setSetting('search_enabled','0');   return showAdminSettings(chatId,'bot'); }
    // adm_settings_main — alias for adm_settings (go to main settings menu)
    if (data === 'adm_settings_main') { if (!isAdmin(chatId)) return; return showAdminSettings(chatId, 'main'); }
    // Unified feature toggle handler for bot section (adm_toggle_{feature})
    if (data.startsWith('adm_toggle_') && isAdmin(chatId)) {
      const TOGGLE_FEATURES = {
        'quick_booking': 'quick_booking_enabled',
        'wishlist':      'wishlist_enabled',
        'search':        'search_enabled',
        'reviews':       'reviews_enabled',
        'loyalty':       'loyalty_enabled',
        'referral':      'referral_enabled',
        'model_stats':   'model_stats_enabled',
        'faq':           'faq_enabled',
        'calc':          'calc_enabled',
      };
      const featureKey = data.replace('adm_toggle_', '');
      const settingKey = TOGGLE_FEATURES[featureKey];
      if (settingKey) {
        const current = await getSetting(settingKey);
        const newVal = current === '0' ? '1' : '0';
        await setSetting(settingKey, newVal);
        await bot.answerCallbackQuery(q.id, { text: newVal === '1' ? '✅ Включено' : '🔕 Выключено' }).catch(()=>{});
        return showAdminSettings(chatId, 'bot');
      }
    }
    if (data === 'adm_broadcast') { if (!isAdmin(chatId)) return; return showBroadcast(chatId); }

    // ── Scheduled broadcasts
    if (data === 'adm_sched_bcast') { if (!isAdmin(chatId)) return; return showScheduledBroadcasts(chatId); }
    if (data === 'adm_new_sched_bcast') {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, 'adm_sched_bcast_text', {});
      return safeSend(chatId, `📅 *Новая запланированная рассылка*\n\nВведите текст рассылки:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_sched_bcast' }]] }
      });
    }
    if (data.startsWith('sched_bcast_cancel_')) {
      if (!isAdmin(chatId)) return;
      const sbId = parseInt(data.replace('sched_bcast_cancel_', ''));
      await run("UPDATE scheduled_broadcasts SET status='cancelled' WHERE id=? AND status='pending'", [sbId]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: '❌ Рассылка отменена' }).catch(() => {});
      return showScheduledBroadcasts(chatId);
    }
    // Segment selection for scheduled broadcast
    if (data.startsWith('adm_sched_bcast_seg_')) {
      if (!isAdmin(chatId)) return;
      const seg = data.replace('adm_sched_bcast_seg_', '');
      const sess = await getSession(chatId);
      const d2 = sessionData(sess);
      d2.sched_segment = seg;
      // Save to DB
      await run(
        `INSERT INTO scheduled_broadcasts (text, scheduled_at, segment, created_by) VALUES (?,?,?,?)`,
        [d2.sched_text, d2.sched_time, seg, String(chatId)]
      ).catch(() => {});
      await clearSession(chatId);
      const segLabel = seg === 'completed' ? 'Завершившие заявку' : seg === 'active' ? 'Активные клиенты' : 'Все клиенты';
      return safeSend(chatId,
        `✅ *Рассылка запланирована\\!*\n\nВремя: *${esc(d2.sched_time)}*\nСегмент: *${esc(segLabel)}*\n\nТекст: _${esc(String(d2.sched_text || '').slice(0, 100))}_`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '📅 Все рассылки', callback_data: 'adm_sched_bcast' }]] }
        }
      );
    }

    // ── Model stats
    if (data.startsWith('adm_model_stats_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_model_stats_', ''));
      return showModelStats(chatId, modelId);
    }

    // ── All order notes (paginated)
    if (data.startsWith('adm_notes_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_notes_', '').split('_');
      const pg = parseInt(parts.pop()) || 0;
      const oId = parseInt(parts.join('_')) || 0;
      return showAllOrderNotes(chatId, oId, pg);
    }

    // ── Quick replies: send template to client
    if (data.startsWith('qr_send_')) {
      if (!isAdmin(chatId)) return;
      const rest = data.replace('qr_send_', '');
      const underscoreIdx = rest.indexOf('_');
      const tplIdx = parseInt(rest.slice(0, underscoreIdx));
      const clientChatId = rest.slice(underscoreIdx + 1);
      const tpl = QUICK_REPLY_TEMPLATES[tplIdx];
      if (!tpl || !clientChatId) return;
      try {
        await safeSend(clientChatId, `💬 *Сообщение от менеджера:*\n\n${esc(tpl)}`, { parse_mode: 'MarkdownV2' });
        await bot.answerCallbackQuery(q.id, { text: '✅ Шаблон отправлен!' }).catch(() => {});
      } catch {
        await bot.answerCallbackQuery(q.id, { text: '❌ Не удалось отправить' }).catch(() => {});
      }
      return;
    }

    // ── Show quick replies for client
    if (data.startsWith('adm_qr_')) {
      if (!isAdmin(chatId)) return;
      const clientChatId = data.replace('adm_qr_', '');
      return showQuickReplies(chatId, clientChatId);
    }

    // ── Audit log
    if (data === 'adm_audit_log') { if (!isAdmin(chatId)) return; return showAuditLog(chatId, 0); }
    // ── Broadcast: new segment selection (adm_bc_seg_*)
    if (data === 'adm_bc_seg_all') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '👥 Все клиенты' }).catch(() => {});
      return _askBroadcastText(chatId, 'all');
    }
    if (data === 'adm_bc_seg_completed') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '✅ Завершённые заявки' }).catch(() => {});
      return _askBroadcastText(chatId, 'completed');
    }
    if (data === 'adm_bc_seg_city') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '🏙 По городу' }).catch(() => {});
      return showBroadcastCitySelection(chatId);
    }
    if (data === 'adm_bc_seg_new') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '🆕 Новые клиенты' }).catch(() => {});
      return _askBroadcastText(chatId, 'new');
    }
    // ── Broadcast: city chosen
    if (data.startsWith('adm_bc_city_')) {
      if (!isAdmin(chatId)) return;
      const city = data.slice('adm_bc_city_'.length);
      await bot.answerCallbackQuery(q.id, { text: `🏙 Город: ${city}` }).catch(() => {});
      return _askBroadcastText(chatId, `city:${city}`);
    }
    // ── Broadcast: ask about photo after text entered
    if (data === 'adm_bc_photo') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd   = sessionData(sess);
      await setSession(chatId, 'adm_broadcast_photo_wait', { ...sd });
      return safeSend(chatId,
        `🖼 *Рассылка — добавить фото*\n\nОтправьте фото:`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] }
        }
      );
    }
    if (data === 'adm_bc_send_now') {
      if (!isAdmin(chatId)) return;
      // Send without photo — go straight to preview
      const sess = await getSession(chatId);
      const sd   = sessionData(sess);
      if (!sd.broadcastText && !sd.broadcastRecipients) return showBroadcast(chatId);
      return previewBroadcast(chatId);
    }
    // ── Broadcast: confirm send
    if (data === 'adm_bc_confirm') {
      if (!isAdmin(chatId)) return;
      return doSendBroadcast(chatId);
    }
    // ── Broadcast: edit text
    if (data === 'adm_bc_edit') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd   = sessionData(sess);
      const segment = sd.broadcastSegment || 'all';
      await setSession(chatId, 'adm_broadcast_msg', { ...sd });
      return safeSend(chatId,
        `✏️ *Изменить текст рассылки*\n\nВведите новый текст:`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] }
        }
      );
    }
    // ── Broadcast: cancel from preview
    if (data === 'adm_bc_cancel_preview') {
      if (!isAdmin(chatId)) return;
      await clearSession(chatId);
      await bot.answerCallbackQuery(q.id, { text: '❌ Рассылка отменена' }).catch(() => {});
      return showBroadcast(chatId);
    }
    // ── Broadcast segment selection (legacy — kept for back-compat)
    if (data === 'adm_broadcast_all') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '👥 Выбрано: все клиенты' }).catch(() => {});
      return _askBroadcastText(chatId, 'all');
    }
    if (data === 'adm_broadcast_completed') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '✅ Завершившие заявку' }).catch(() => {});
      return _askBroadcastText(chatId, 'completed');
    }
    // ── Broadcast confirm (legacy)
    if (data === 'adm_broadcast_confirm') {
      if (!isAdmin(chatId)) return;
      return doSendBroadcast(chatId);
    }
    // ── Broadcast type selection (legacy)
    if (data === 'adm_broadcast_text') {
      if (!isAdmin(chatId)) return;
      const sess2 = await getSession(chatId);
      const sd2   = sessionData(sess2);
      await setSession(chatId, 'adm_broadcast_msg', { broadcastSegment: sd2.broadcastSegment || 'all' });
      return safeSend(chatId,
        `📝 *Рассылка — текст*\n\nВведите текст сообщения для рассылки:`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] }
        }
      );
    }
    if (data === 'adm_broadcast_photo') {
      if (!isAdmin(chatId)) return;
      const sess3 = await getSession(chatId);
      const sd3   = sessionData(sess3);
      await setSession(chatId, 'adm_broadcast_photo_wait', { broadcastSegment: sd3.broadcastSegment || 'all' });
      return safeSend(chatId,
        `🖼 *Рассылка — фото*\n\nОтправьте фото для рассылки:`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] }
        }
      );
    }
    // ── Quick toggle model availability
    if (data.startsWith('adm_toggle_avail_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_toggle_avail_', ''));
      const m  = await get('SELECT available FROM models WHERE id=?', [id]).catch(()=>null);
      if (!m) return;
      const newVal = m.available ? 0 : 1;
      await run('UPDATE models SET available=? WHERE id=?', [newVal, id]);
      await logAdminAction(chatId, 'toggle_availability', 'model', id, { available: newVal });
      await bot.answerCallbackQuery(q.id, { text: newVal ? '🟢 Модель доступна' : '🔴 Модель недоступна' }).catch(()=>{});
      return showAdminModels(chatId, 0, {});
    }
    // ── Admin search order
    if (data === 'adm_search_order') { if (!isAdmin(chatId)) return; return showAdminSearchOrder(chatId); }
    // ── My orders pagination
    if (data.startsWith('my_orders_page_')) {
      const pg = parseInt(data.replace('my_orders_page_', '')) || 0;
      return showMyOrders(chatId, pg);
    }
    // ── Broadcast with photo: skip caption
    if (data === 'adm_broadcast_photo_nosend') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd   = sessionData(sess);
      if (!sd.broadcast_photo_id) return safeSend(chatId, '❌ Фото не найдено. Попробуйте заново.');
      return sendBroadcastWithPhoto(chatId, sd.broadcast_photo_id, '');
    }
    if (data === 'adm_reviews')          { if (!isAdmin(chatId)) return; return showAdminReviews(chatId); }
    if (data === 'adm_reviews_pending')  { if (!isAdmin(chatId)) return; return showAdminReviewsList(chatId, 'pending'); }
    if (data === 'adm_reviews_approved') { if (!isAdmin(chatId)) return; return showAdminReviewsList(chatId, 'approved'); }
    if (data.startsWith('rev_approve_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_approve_', ''));
      await run('UPDATE reviews SET approved=1, status=NULL WHERE id=?', [id]).catch(()=>{});
      // Notify client if linked to an order
      try {
        const rev = await get('SELECT * FROM reviews WHERE id=?', [id]);
        if (rev && rev.order_id) {
          const ord = await get('SELECT client_chat_id FROM orders WHERE id=?', [rev.order_id]).catch(()=>null);
          if (ord?.client_chat_id) {
            await safeSend(ord.client_chat_id, `✅ Ваш отзыв одобрен и опубликован\\. Спасибо\\!`, { parse_mode: 'MarkdownV2' }).catch(()=>{});
          }
        }
      } catch {}
      return safeSend(chatId, `✅ Отзыв #${id} одобрен.`);
    }
    if (data.startsWith('rev_reject_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_reject_', ''));
      await run("UPDATE reviews SET approved=0, status='rejected' WHERE id=?", [id]).catch(()=>{});
      // Notify client if linked to an order
      try {
        const rev = await get('SELECT * FROM reviews WHERE id=?', [id]);
        if (rev && rev.order_id) {
          const ord = await get('SELECT client_chat_id FROM orders WHERE id=?', [rev.order_id]).catch(()=>null);
          if (ord?.client_chat_id) {
            await safeSend(ord.client_chat_id, `ℹ️ Ваш отзыв был отклонён модератором\\.`, { parse_mode: 'MarkdownV2' }).catch(()=>{});
          }
        }
      } catch {}
      return safeSend(chatId, `❌ Отзыв #${id} отклонён.`);
    }
    if (data.startsWith('rev_delete_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_delete_', ''));
      await run('DELETE FROM reviews WHERE id=?', [id]).catch(()=>{});
      return safeSend(chatId, `🗑 Отзыв #${id} удалён.`);
    }
    if (data === 'adm_admins')    { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return showAdminManagement(chatId); }
    if (data === 'adm_export')    { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return showExportMenu(chatId); }
    if (data === 'adm_addmodel')  { if (!isAdmin(chatId)) return; return showAddModelStep(chatId, { _step: 'name' }); }

    // ── Admin: client management
    if (data === 'adm_clients') { if (!isAdmin(chatId)) return; return showAdminClients(chatId, 0); }
    if (data === 'adm_panel')   { if (!isAdmin(chatId)) return; return showAdminMenu(chatId, q.from.first_name); }
    if (data.startsWith('adm_clients_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('adm_clients_', '')) || 0;
      return showAdminClients(chatId, page);
    }
    if (data.startsWith('adm_client_') && !data.startsWith('adm_clients_')) {
      if (!isAdmin(chatId)) return;
      const clientId = parseInt(data.replace('adm_client_', ''));
      if (!isNaN(clientId)) return showAdminClientCard(chatId, clientId);
    }

    // ── Admin: block/unblock client
    if (data.startsWith('adm_block_')) {
      if (!isAdmin(chatId)) return;
      const clientId = parseInt(data.replace('adm_block_', ''));
      await run(`INSERT OR REPLACE INTO blocked_clients (chat_id, blocked_by) VALUES (?,?)`, [clientId, chatId]);
      await bot.answerCallbackQuery(q.id, { text: '⛔ Клиент заблокирован' }).catch(()=>{});
      return showAdminClientCard(chatId, clientId);
    }
    if (data.startsWith('adm_unblock_')) {
      if (!isAdmin(chatId)) return;
      const clientId = parseInt(data.replace('adm_unblock_', ''));
      await run(`DELETE FROM blocked_clients WHERE chat_id=?`, [clientId]);
      await bot.answerCallbackQuery(q.id, { text: '✅ Клиент разблокирован' }).catch(()=>{});
      return showAdminClientCard(chatId, clientId);
    }

    // ── Admin: send personal message to client
    if (data.startsWith('adm_msg_client_')) {
      if (!isAdmin(chatId)) return;
      const clientId = parseInt(data.replace('adm_msg_client_', ''));
      await setSession(chatId, `adm_personal_msg_${clientId}`, {});
      return safeSend(chatId, `📝 Введите сообщение для клиента \\(ID: ${clientId}\\):\n\n_Сообщение будет отправлено от имени бота_`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: `adm_client_${clientId}` }]] }
      });
    }

    // ── Export period filters
    if (data === 'adm_export_today') { if (!isAdmin(chatId)) return; return doExportOrders(chatId, 'today'); }
    if (data === 'adm_export_week')  { if (!isAdmin(chatId)) return; return doExportOrders(chatId, 'week');  }
    if (data === 'adm_export_month') { if (!isAdmin(chatId)) return; return doExportOrders(chatId, 'month'); }
    if (data === 'adm_export_all')   { if (!isAdmin(chatId)) return; return doExportOrders(chatId, 'all');   }

    // ── Export: CSV documents
    if (data === 'adm_export_orders_csv') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '⏳ Формирую CSV...' }).catch(()=>{});
      return showExportOrdersMenu(chatId);
    }
    if (data === 'adm_export_models_csv') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '⏳ Формирую CSV...' }).catch(()=>{});
      return exportModelsCSV(chatId);
    }
    if (data === 'adm_export_clients_csv') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '⏳ Формирую CSV...' }).catch(()=>{});
      return exportClientsCSV(chatId);
    }

    // ── Quick stats
    if (data === 'adm_quick_stats') {
      if (!isAdmin(chatId)) return;
      try {
        const [todayR, activeR, monthBudget] = await Promise.all([
          get("SELECT COUNT(*) as n FROM orders WHERE date(created_at) = date('now')"),
          get("SELECT COUNT(*) as n FROM orders WHERE status IN ('new','reviewing','confirmed','in_progress')"),
          get(`SELECT SUM(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)) as total
               FROM orders WHERE status='completed' AND created_at >= datetime('now','-30 days')
               AND budget IS NOT NULL AND budget != '' AND budget GLOB '[0-9]*'`),
        ]);
        const revenue = monthBudget?.total ? Math.round(monthBudget.total).toLocaleString('ru') : '—';
        await bot.answerCallbackQuery(q.id, {
          text: `📊 Сегодня: ${todayR.n} | Активных: ${activeR.n} | Выручка/мес: ${revenue} руб.`,
          show_alert: true,
        }).catch(()=>{});
      } catch { await bot.answerCallbackQuery(q.id, { text: '❌ Ошибка загрузки статистики' }).catch(()=>{}); }
      return;
    }

    // ── Telegram channel
    if (data === 'tg_channel') {
      const ch = await getSetting('tg_channel').catch(()=>null);
      if (ch) {
        return safeSend(chatId, `📣 *Наш Telegram канал:*\n\n${esc(ch)}`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '📣 Перейти в канал', url: ch.startsWith('http') ? ch : `https://t.me/${ch.replace(/^@/,'')}` }],
            [{ text: '← Главное меню', callback_data: 'main_menu' }],
          ]}
        });
      }
      return;
    }

    // ── Bulk: новые → В работу
    if (data === 'adm_bulk_new_to_review') {
      if (!isAdmin(chatId)) return;
      const result = await run("UPDATE orders SET status='reviewing', updated_at=CURRENT_TIMESTAMP WHERE status='new'");
      return safeSend(chatId, `✅ Переведено ${result.changes} заявок в статус «На рассмотрении»`, {
        reply_markup: { inline_keyboard: [[{ text: '📋 К заявкам', callback_data: 'adm_orders__0' }]] }
      });
    }

    // ── Quick actions from orders list
    if (data.startsWith('adm_quick_confirm_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_quick_confirm_', ''));
      return adminChangeStatus(chatId, id, 'confirmed');
    }
    if (data.startsWith('adm_quick_complete_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_quick_complete_', ''));
      return adminChangeStatus(chatId, id, 'completed');
    }

    // ── Assign manager: show list
    if (data.startsWith('adm_assign_mgr_') && !data.match(/adm_assign_mgr_\d+_\d+/)) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_assign_mgr_', ''));
      const admins = await query("SELECT id, username, role FROM admins ORDER BY id").catch(()=>[]);
      if (!admins.length) return safeSend(chatId, '❌ Нет администраторов в базе.');
      const btns = admins.map(a => [{
        text: `${a.username} (${a.role})`,
        callback_data: `adm_assign_mgr_${orderId}_${a.id}`
      }]);
      btns.push([{ text: '← Назад', callback_data: `adm_order_${orderId}` }]);
      return safeSend(chatId, `👤 *Выберите менеджера для заявки*:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: btns }
      });
    }

    // ── Assign manager: set
    if (data.match(/^adm_assign_mgr_\d+_\d+$/)) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_assign_mgr_', '').split('_');
      const orderId = parseInt(parts[0]);
      const adminId = parseInt(parts[1]);
      await run('UPDATE orders SET manager_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', [adminId, orderId]);
      const [admin, order] = await Promise.all([
        get('SELECT username, telegram_id FROM admins WHERE id=?', [adminId]).catch(()=>null),
        get('SELECT order_number FROM orders WHERE id=?', [orderId]).catch(()=>null),
      ]);
      // Notify assigned manager if they have a telegram_id
      if (admin?.telegram_id && String(admin.telegram_id) !== String(chatId)) {
        safeSend(admin.telegram_id,
          `👤 Вам назначена заявка *${esc(order?.order_number||String(orderId))}*\n\nНажмите, чтобы открыть:`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '📋 Открыть заявку', callback_data: `adm_order_${orderId}` }]] }
          }
        ).catch(()=>{});
      }
      await safeSend(chatId, `✅ Менеджер *${esc(admin?.username||String(adminId))}* назначен на заявку\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] }
      });
      return;
    }

    // ── Order note: start input
    if (data.startsWith('adm_note_') && !data.startsWith('adm_note_input_')) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_note_', ''));
      await setSession(chatId, `adm_note_input_${orderId}`, {});
      return safeSend(chatId, `📝 *Введите заметку к заявке:*`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: `adm_order_${orderId}` }]] }
      });
    }

    // ── Settings inputs — set session and ask for text
    const settingPrompts = {
      'adm_set_greeting':           '📝 Введите новый текст *приветствия* (при /start):',
      'adm_set_about':              'ℹ️ Введите новый текст *«О нас»*:',
      'adm_set_phone':              '📞 Введите новый *номер телефона* агентства:',
      'adm_set_email':              '📧 Введите новый *email* агентства:',
      'adm_set_insta':              '📸 Введите новый *Instagram* (без @):',
      'adm_set_addr':               '📍 Введите новый *адрес* агентства:',
      'adm_set_pricing':            '💰 Введите новый *прайс-лист* (можно несколько строк):',
      'adm_set_whatsapp':           '📱 Введите *WhatsApp* номер (с кодом страны, например +79001234567):',
      'adm_set_site_url':           '🌐 Введите *URL сайта* (например https://nevesty-models.ru):',
      'adm_set_mgr_hours':          '🕐 Введите *часы работы менеджера* (например: Пн-Пт 9:00-20:00):',
      'adm_set_mgr_reply':          '💬 Введите *авто-ответ менеджера* при обращении:',
      'adm_set_catalog_per_page':   '📄 Введите *кол-во моделей на странице* (рекомендуется 5-10):',
      'adm_set_catalog_title':      '📌 Введите *заголовок каталога*:',
      'adm_set_booking_min_budget': '💰 Введите *минимальный бюджет* для заявки (оставьте пустым — без лимита):',
      'adm_set_booking_confirm_msg':'💬 Введите *сообщение после бронирования*:',
      'adm_set_booking_thanks':     '🎉 Введите *текст после успешного бронирования* (отображается клиенту):',
      'adm_set_tg_channel':         '📣 Введите *ссылку или @username* Telegram канала агентства:',
      'adm_set_reviews_min':        '🔢 Введите *минимум завершённых заявок* для написания отзыва:',
      'adm_set_reviews_prompt':     '📝 Введите *текст приглашения к отзыву*:',
      'adm_set_cities_list':        '🏙 Введите *список городов* через запятую (например: Москва, Санкт-Петербург, Казань):',
      'adm_set_welcome_photo':      '🖼 Введите *URL фото* для приветствия (или отправьте ссылку на изображение):',
      'adm_set_main_menu_text':     '📋 Введите *текст главного меню* бота:',
      'adm_set_model_max_photos':   '🖼 Введите *максимальное кол-во фото* у модели:',
      'adm_set_client_max_orders':  '📋 Введите *максимум активных заявок* у одного клиента:',
      'adm_set_client_msg_delay':   '⏱ Введите *минимальный интервал* между сообщениями клиента (секунды):',
      'adm_set_api_rate_limit':     '🔒 Введите *rate limit* API (запросов в минуту):',
    };
    if (settingPrompts[data]) {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, data, {});
      return safeSend(chatId, settingPrompts[data], {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_settings' }]] } });
    }

    // ── Notifications toggle
    if (data.startsWith('adm_notif_')) {
      if (!isAdmin(chatId)) return;
      const [, , key, onoff] = data.split('_');
      const settingKey = key === 'new' ? 'notif_new_order' : 'notif_status';
      await setSetting(settingKey, onoff === 'on' ? '1' : '0');
      return showAdminSettings(chatId);
    }

    // ── Add admin Telegram ID
    if (data === 'adm_add_admin_id') {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, 'adm_add_admin_id', {});
      return safeSend(chatId, '👑 Введите *Telegram ID* нового администратора:\n\n_Получить ID можно через @userinfobot_', {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_admins' }]] }
      });
    }

    // ── Add model wizard — skip buttons
    if (data.startsWith('adm_mdl_skip_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      const skipField = data.replace('adm_mdl_skip_','');
      const nextSteps = { name:'age', age:'height', height:'params', params:'shoe', shoe:'hair', hair:'eye', eye:'category', category:'instagram', instagram:'bio', bio:'photo', photo:'confirm' };
      d2._step = nextSteps[skipField] || 'confirm';
      return showAddModelStep(chatId, d2);
    }

    // ── Add model wizard — select buttons (hair, eye, category)
    if (data.startsWith('adm_mdl_hair_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      d2.hair_color = data.replace('adm_mdl_hair_',''); d2._step = 'eye';
      return showAddModelStep(chatId, d2);
    }
    if (data.startsWith('adm_mdl_eye_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      d2.eye_color = data.replace('adm_mdl_eye_',''); d2._step = 'category';
      return showAddModelStep(chatId, d2);
    }
    if (data.startsWith('adm_mdl_cat_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      const newCat = data.replace('adm_mdl_cat_','');
      if (!Object.keys(MODEL_CATEGORIES).includes(newCat)) return;
      d2.category = newCat; d2._step = 'instagram';
      return showAddModelStep(chatId, d2);
    }
    if (data === 'adm_mdl_save') {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      return saveNewModel(chatId, sessionData(session2));
    }

    // ── Edit model
    if (data.startsWith('adm_editmodel_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_editmodel_',''));
      return showModelEditMenu(chatId, id);
    }
    if (data.startsWith('adm_ef_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_ef_','').split('_');
      const modelId = parseInt(parts[0]);
      const field   = parts.slice(1).join('_');
      if (field === 'category') {
        // Show category selector
        const btns = Object.entries(MODEL_CATEGORIES).map(([k,v]) => [{ text: v, callback_data: `adm_efc_${modelId}_${k}` }]);
        btns.push([{ text: '← Назад', callback_data: `adm_editmodel_${modelId}` }]);
        return safeSend(chatId, '🏷 Выберите новую категорию:', { reply_markup: { inline_keyboard: btns } });
      }
      if (field === 'photo') {
        return showPhotoGalleryManager(chatId, modelId);
      }
      const fieldLabels = { name:'имя', age:'возраст', height:'рост (см)', weight:'вес (кг)',
                            shoe_size:'размер обуви', instagram:'Instagram', bio:'описание',
                            hair_color:'цвет волос', eye_color:'цвет глаз', params:'параметры (ОГ/ОТ/ОБ)',
                            phone:'телефон модели', city:'город', video_url:'ссылка на видео (URL)' };
      await setSession(chatId, `adm_ef_${modelId}_${field}`, {});
      return safeSend(chatId, `✏️ Введите новое *${fieldLabels[field]||field}*:`, {
        reply_markup: { inline_keyboard: [[{ text: '← Отмена', callback_data: `adm_editmodel_${modelId}` }]] } });
    }
    if (data.startsWith('adm_efc_')) {  // edit field category
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_efc_','').split('_');
      const modelId = parseInt(parts[0]);
      const cat = parts[1];
      if (!Object.keys(MODEL_CATEGORIES).includes(cat)) return;
      await run('UPDATE models SET category=? WHERE id=?', [cat, modelId]).catch(()=>{});
      return safeSend(chatId, '✅ Категория обновлена!', {
        reply_markup: { inline_keyboard: [[{ text: '✏️ Редактировать', callback_data: `adm_editmodel_${modelId}` }, { text: '← Карточка', callback_data: `adm_model_${modelId}` }]] }
      });
    }

    // ── Delete model
    if (data.startsWith('adm_del_model_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_del_model_',''));
      const m = await get('SELECT name FROM models WHERE id=?', [id]).catch(()=>null);
      return safeSend(chatId, `🗑 *Удалить модель «${m?.name||id}»?*\n\nЭто действие необратимо!`, {
        reply_markup: { inline_keyboard: [
          [{ text: '⚠️ Да, удалить', callback_data: `adm_del_confirm_${id}` }],
          [{ text: '← Отмена',       callback_data: `adm_model_${id}`        }],
        ]}
      });
    }
    if (data.startsWith('adm_del_confirm_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_del_confirm_',''));
      const m = await get('SELECT name FROM models WHERE id=?', [id]).catch(()=>null);
      await run('DELETE FROM models WHERE id=?', [id]).catch(()=>{});
      await logAdminAction(chatId, 'delete_model', 'model', id, { name: m?.name });
      return safeSend(chatId, `✅ Модель «${m?.name||id}» удалена.`, {
        reply_markup: { inline_keyboard: [[{ text: '← К моделям', callback_data: 'adm_models_0' }]] }
      });
    }
    if (data.startsWith('adm_gallery_clear_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_gallery_clear_',''));
      await run("UPDATE models SET photo_main=NULL, photos='[]' WHERE id=?", [modelId]).catch(()=>{});
      return showPhotoGalleryManager(chatId, modelId);
    }
    if (data.startsWith('adm_gallery_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_gallery_',''));
      return showPhotoGalleryManager(chatId, modelId);
    }

    // ── Прямой ответ клиенту (direct_reply_chatId — из вопроса менеджеру)
    if (data.startsWith('direct_reply_')) {
      if (!isAdmin(chatId)) return;
      const targetId = data.replace('direct_reply_', '');
      await setSession(chatId, 'direct_reply', { target_chat_id: targetId });
      return safeSend(chatId, `✍️ Введите ответ клиенту (ID: ${targetId}):`, {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_main' }]] }
      });
    }

    // ── Написать менеджеру
    if (data === 'msg_manager_start') {
      await setSession(chatId, 'msg_to_manager', {});
      const autoReply = await getSetting('manager_reply').catch(() => '');
      if (autoReply && autoReply.trim()) {
        await safeSend(chatId, esc(autoReply), { parse_mode: 'MarkdownV2' });
      }
      return safeSend(chatId,
        '✍️ *Напишите ваш вопрос*\n\nОтправьте сообщение — менеджер ответит в течение часа\\.',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] }
        }
      );
    }

    // ── AI Factory
    if (data === 'adm_factory') { if (!isAdmin(chatId)) return; return showFactoryPanel(chatId); }
    if (data === 'adm_factory_growth') { if (!isAdmin(chatId)) return; return showFactoryGrowth(chatId, 0); }
    if (data.startsWith('adm_factory_growth_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('adm_factory_growth_', '')) || 0;
      return showFactoryGrowth(chatId, page);
    }
    if (data === 'adm_factory_exp')       { if (!isAdmin(chatId)) return; return showFactoryExperiments(chatId); }
    if (data === 'adm_factory_decisions') { if (!isAdmin(chatId)) return; return showFactoryDecisions(chatId); }
    if (data === 'adm_factory_tasks')     { if (!isAdmin(chatId)) return; return showFactoryTasks(chatId, 0); }
    if (data === 'adm_experiments')       { if (!isAdmin(chatId)) return; return showAdminExperiments(chatId); }
    if (data.startsWith('adm_factory_tasks_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('adm_factory_tasks_', '')) || 0;
      return showFactoryTasks(chatId, page);
    }
    if (data.startsWith('factory_task_done_')) {
      if (!isAdmin(chatId)) return;
      const taskId = parseInt(data.replace('factory_task_done_', ''));
      await run("UPDATE factory_tasks SET status='done' WHERE id=?", [taskId]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: 'Задача выполнена!' }).catch(() => {});
      return safeSend(chatId, 'Задача отмечена как выполненная.', {
        reply_markup: { inline_keyboard: [[{ text: 'AI Задачи', callback_data: 'adm_factory_tasks' }]] }
      });
    }
    if (data.startsWith('factory_task_skip_')) {
      if (!isAdmin(chatId)) return;
      const taskId = parseInt(data.replace('factory_task_skip_', ''));
      await run("UPDATE factory_tasks SET status='skipped' WHERE id=?", [taskId]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: 'Задача пропущена' }).catch(() => {});
      return safeSend(chatId, 'Задача пропущена.', {
        reply_markup: { inline_keyboard: [[{ text: 'AI Задачи', callback_data: 'adm_factory_tasks' }]] }
      });
    }
    if (data.startsWith('adm_factory_done_')) {
      if (!isAdmin(chatId)) return;
      const actionId = parseInt(data.replace('adm_factory_done_', ''));
      await new Promise(resolve => {
        const sqlite3 = require('sqlite3').verbose();
        const fdb = new sqlite3.Database(FACTORY_DB_PATH, sqlite3.OPEN_READWRITE, err => {
          if (err) return resolve();
          fdb.run("UPDATE growth_actions SET status='done', updated_at=datetime('now') WHERE id=?", [actionId], () => { fdb.close(); resolve(); });
        });
      });
      return safeSend(chatId, '✅ Отмечено как выполнено.', {
        reply_markup: { inline_keyboard: [[{ text: '← Growth Actions', callback_data: 'adm_factory_growth' }]] }
      });
    }
    if (data === 'adm_factory_run') {
      if (!isAdmin(chatId)) return;
      await safeSend(chatId, '🔄 Запускаю цикл AI Factory...\n\nРезультат придёт через 1-2 минуты.', {
        reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] }
      });
      const { spawn } = require('child_process');
      const proc = spawn('python3', ['-c',
        'import sys; sys.path.insert(0,"/home/user/Pablo"); from factory.cycle import run_cycle; run_cycle()'
      ], { cwd: '/home/user/Pablo', detached: true, stdio: ['ignore','ignore','pipe'] });
      proc.stderr.on('data', d => console.error('[Factory]', d.toString().trim()));
      proc.unref();
      return;
    }

    // ── Agent feed
    if (data.startsWith('agent_feed_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('agent_feed_','')) || 0;
      return showAgentFeed(chatId, page);
    }

    // ── Agent discussions feed
    if (data === 'adm_discussions') {
      if (!isAdmin(chatId)) return;
      return showAgentDiscussions(chatId, '24h', 0);
    }
    if (data.startsWith('adm_disc_')) {
      if (!isAdmin(chatId)) return;
      const parts     = data.replace('adm_disc_', '').split('_');
      const rawPeriod = parts.slice(0, -1).join('_');
      const validPeriods = ['1h', '24h', '7d', '30d'];
      const period    = validPeriods.includes(rawPeriod) ? rawPeriod : '24h';
      const page      = parseInt(parts[parts.length - 1]) || 0;
      return showAgentDiscussions(chatId, period, page);
    }

    // ── Категории каталога (быстрые фильтры)
    if (data === 'cat_filter_fashion')     return showCatalog(chatId, 'fashion',    0, { category: 'fashion'    });
    if (data === 'cat_filter_commercial')  return showCatalog(chatId, 'commercial', 0, { category: 'commercial' });
    if (data === 'cat_filter_events')      return showCatalog(chatId, 'events',     0, { category: 'events'     });

    // ── Сортировка каталога
    if (data === 'cat_sort_featured') {
      catalogSortPrefs.set(String(chatId), 'featured');
      return showCatalog(chatId, '', 0);
    }
    if (data === 'cat_sort_alpha') {
      catalogSortPrefs.set(String(chatId), 'alpha');
      return showCatalog(chatId, '', 0);
    }

    // ── Поиск модели по параметрам (мульти-фильтр, БЛОК 2.4)
    if (data === 'cat_search') return showSearchMenu(chatId);

    // Height filter: srch_h_{min}_{max}
    if (data.startsWith('srch_h_')) {
      const parts = data.replace('srch_h_', '').split('_');
      const min = parseInt(parts[0]) || 0;
      const max = parseInt(parts[1]) || 999;
      const f = getSearchFilters(chatId);
      if (f.height_min === min && f.height_max === max) {
        // toggle off
        delete f.height_min; delete f.height_max;
      } else {
        f.height_min = min; f.height_max = max;
      }
      return showSearchMenu(chatId);
    }

    // Age filter: srch_a_{min}_{max}
    if (data.startsWith('srch_a_')) {
      const parts = data.replace('srch_a_', '').split('_');
      const min = parseInt(parts[0]) || 0;
      const max = parseInt(parts[1]) || 99;
      const f = getSearchFilters(chatId);
      if (f.age_min === min && f.age_max === max) {
        delete f.age_min; delete f.age_max;
      } else {
        f.age_min = min; f.age_max = max;
      }
      return showSearchMenu(chatId);
    }

    // Category filter: srch_c_{category}
    if (data.startsWith('srch_c_')) {
      const cat = data.replace('srch_c_', '');
      const f = getSearchFilters(chatId);
      f.category = (f.category === cat) ? null : cat;
      return showSearchMenu(chatId);
    }

    // City filter: srch_city_{city}
    if (data.startsWith('srch_city_')) {
      const city = data.replace('srch_city_', '');
      const f = getSearchFilters(chatId);
      f.city = (f.city === city) ? null : city;
      return showSearchMenu(chatId);
    }

    // Reset filters
    if (data === 'srch_reset') {
      searchFilters.set(String(chatId), {});
      return showSearchMenu(chatId);
    }

    // Run search
    if (data === 'srch_go') {
      const f = getSearchFilters(chatId);
      return showSearchResults(chatId, f, 0);
    }

    // Pagination: srch_page_{n}
    if (data.startsWith('srch_page_')) {
      const page = parseInt(data.replace('srch_page_', '')) || 0;
      const f = getSearchFilters(chatId);
      return showSearchResults(chatId, f, page);
    }

    // View model from search results
    if (data.startsWith('srch_view_')) {
      const modelId = parseInt(data.replace('srch_view_', ''));
      return showModel(chatId, modelId);
    }

    // Legacy cat_search_* callbacks (keep for backward compatibility)
    if (data.startsWith('cat_search_height_')) {
      const range = data.replace('cat_search_height_', '');
      const [min, max] = range.split('-').map(Number);
      const f = getSearchFilters(chatId);
      f.height_min = min || 0; f.height_max = max || 999;
      return showSearchResults(chatId, f, 0);
    }
    if (data.startsWith('cat_search_age_')) {
      const range = data.replace('cat_search_age_', '');
      const [min, max] = range.split('-').map(Number);
      const f = getSearchFilters(chatId);
      f.age_min = min || 0; f.age_max = max || 99;
      return showSearchResults(chatId, f, 0);
    }
    if (data.startsWith('cat_search_res_')) {
      // legacy pagination — just re-run current filters
      const rest  = data.replace('cat_search_res_', '');
      const parts = rest.split('_');
      const page2 = parseInt(parts.pop()) || 0;
      const f = getSearchFilters(chatId);
      return showSearchResults(chatId, f, page2);
    }

    // ── Отзывы (публичные)
    if (data === 'show_reviews' || data === 'cat_rev') return showPublicReviews(chatId, 0);
    if (data.startsWith('show_reviews_')) {
      const page = parseInt(data.replace('show_reviews_', '')) || 0;
      return showPublicReviews(chatId, page);
    }
    if (data.startsWith('cat_rev_')) {
      const page = parseInt(data.replace('cat_rev_', '')) || 0;
      return showPublicReviews(chatId, page);
    }

    // ── Оставить отзыв
    if (data === 'review_skip') {
      // Client dismissed the review follow-up
      return safeSend(chatId, '✅ Спасибо, что воспользовались нашими услугами\\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
      });
    }
    if (data.startsWith('leave_review_')) {
      const orderId = parseInt(data.replace('leave_review_', ''));
      return startLeaveReview(chatId, orderId);
    }
    if (data.startsWith('review_rating_')) {
      // review_rating_{orderId}_{rating}
      const parts = data.replace('review_rating_', '').split('_');
      const rating = parseInt(parts.pop());
      const orderId = parseInt(parts.join('_'));
      const session = await getSession(chatId);
      const d = sessionData(session);
      d.review_order_id = orderId;
      d.review_rating   = rating;
      await setSession(chatId, 'leave_review_text', d);
      return safeSend(chatId,
        `⭐ Оценка: ${'⭐'.repeat(rating)}\n\nТеперь напишите текст отзыва:`,
        { reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] } }
      );
    }

    // ── rev_rate_{rating}_{orderId} — альтернативний формат кнопок оцінки
    if (data.startsWith('rev_rate_')) {
      // rev_rate_5_123
      const parts = data.split('_'); // ['rev', 'rate', '5', '123']
      const rating  = parseInt(parts[2]);
      const orderId = parseInt(parts[3]);
      if (!rating || rating < 1 || rating > 5) return;
      // Verify order belongs to this user (if orderId provided)
      if (orderId) {
        const order = await get(
          'SELECT id, status FROM orders WHERE id=? AND client_chat_id=?',
          [orderId, String(chatId)]
        ).catch(() => null);
        if (!order) {
          return safeSend(chatId, '❌ Заявка не найдена\\.', { parse_mode: 'MarkdownV2' });
        }
        // Check for duplicate review
        const existing = await get(
          'SELECT id FROM reviews WHERE chat_id=? AND order_id=?',
          [String(chatId), orderId]
        ).catch(() => null);
        if (existing) {
          return safeSend(chatId, '✅ Ви вже залишали відгук на цю заявку\\.', {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
          });
        }
      }
      const session = await getSession(chatId);
      const d = sessionData(session);
      d.review_order_id = orderId || null;
      d.review_rating   = rating;
      await setSession(chatId, 'leave_review_text', d);
      const starLabel = rating === 5 ? '🌟' : '⭐'.repeat(rating);
      return safeSend(chatId,
        `${starLabel} *Оцінка: ${rating}/5*\n\nТепер напишіть короткий відгук \\(або надішліть «\\.» щоб пропустити\\):`,
        { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: [[{ text: '❌ Скасувати', callback_data: 'main_menu' }]] } }
      );
    }

    // ── Повторить заявку
    if (data.startsWith('repeat_order_')) {
      const orderId = parseInt(data.replace('repeat_order_', ''));
      return repeatOrder(chatId, orderId);
    }

    // ── Профиль: изменить контакты
    if (data === 'profile_edit_contacts') return startEditProfile(chatId);
    if (data === 'profile_edit_name') {
      await setSession(chatId, 'profile_edit_name', {});
      return safeSend(chatId, '👤 Введіть нове ім\'я:', {
        reply_markup: { inline_keyboard: [[{ text: '❌ Скасувати', callback_data: 'profile' }]] }
      });
    }
    if (data === 'profile_edit_phone') {
      await setSession(chatId, 'profile_edit_phone', {});
      return safeSend(chatId, '📞 Введите новый номер телефона:', {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'profile' }]] }
      });
    }

    // ── Настройки уведомлений клиента
    if (data === 'client_notif_settings') return showClientNotificationSettings(chatId);

    if (data === 'client_notif_status') {
      const prefs = await get('SELECT * FROM client_prefs WHERE chat_id=?', [chatId]).catch(() => null) || { notify_status: 1 };
      await run(
        `INSERT INTO client_prefs (chat_id, notify_status) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET notify_status=excluded.notify_status, updated_at=CURRENT_TIMESTAMP`,
        [chatId, prefs.notify_status ? 0 : 1]
      ).catch(() => {});
      return showClientNotificationSettings(chatId);
    }

    if (data === 'client_notif_promo') {
      const prefs = await get('SELECT * FROM client_prefs WHERE chat_id=?', [chatId]).catch(() => null) || { notify_promo: 1 };
      await run(
        `INSERT INTO client_prefs (chat_id, notify_promo) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET notify_promo=excluded.notify_promo, updated_at=CURRENT_TIMESTAMP`,
        [chatId, prefs.notify_promo ? 0 : 1]
      ).catch(() => {});
      return showClientNotificationSettings(chatId);
    }

    if (data === 'client_notif_review') {
      const prefs = await get('SELECT * FROM client_prefs WHERE chat_id=?', [chatId]).catch(() => null) || { notify_review: 1 };
      await run(
        `INSERT INTO client_prefs (chat_id, notify_review) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET notify_review=excluded.notify_review, updated_at=CURRENT_TIMESTAMP`,
        [chatId, prefs.notify_review ? 0 : 1]
      ).catch(() => {});
      return showClientNotificationSettings(chatId);
    }

    // ── Доступность модели
    if (data.startsWith('ask_availability_')) {
      const modelId = parseInt(data.replace('ask_availability_', ''));
      const m = await get('SELECT name, available FROM models WHERE id=?', [modelId]).catch(() => null);
      if (!m) return;
      const availText = m.available
        ? `✅ *${esc(m.name)}* доступна для заказа\\!\n\nЧтобы уточнить конкретные даты — напишите менеджеру или оформите заявку\\.`
        : `⏳ *${esc(m.name)}* временно занята\\.\n\nОставьте заявку и мы уточним ближайшие свободные даты\\.`;
      return safeSend(chatId, availText, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '📋 Оформить заявку', callback_data: `bk_model_${modelId}` }],
          [{ text: '📞 Менеджер', callback_data: 'msg_manager_start' }],
          [{ text: '← К модели', callback_data: `cat_model_${modelId}` }]
        ]}
      });
    }

    // ── FAQ: отдельный вопрос
    if (data.startsWith('faq_')) {
      const faqId = parseInt(data.replace('faq_', ''));
      const faq = await get('SELECT * FROM faq WHERE id=? AND active=1', [faqId]).catch(() => null);
      if (!faq) return;
      return safeSend(chatId, `*${esc(faq.question)}*\n\n${esc(faq.answer)}`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '← Все вопросы', callback_data: 'faq' }],
          [{ text: '📋 Оформить заявку', callback_data: 'bk_start' }]
        ]}
      });
    }

    // ── Отмена незавершённой заявки (из напоминания)
    if (data === 'cancel_booking') {
      await clearSession(chatId);
      return showMainMenu(chatId, q.from.first_name);
    }
  });

  // ── Photo handler (для загрузки фото модели через бот) ──────────────────
  bot.on('photo', async (msg) => {
    const chatId  = msg.chat.id;
    if (!isAdmin(chatId)) return;
    const session = await getSession(chatId);
    const state   = session?.state || 'idle';
    const d       = sessionData(session);
    const fileId  = msg.photo[msg.photo.length - 1].file_id;

    if (state === 'adm_mdl_photo') {
      d.photo_file_id = fileId; d._step = 'confirm';
      return showAddModelStep(chatId, d);
    }
    if (state.startsWith('adm_gallery_')) {
      const modelId = parseInt(state.replace('adm_gallery_',''));
      const m = await get('SELECT photo_main, photos FROM models WHERE id=?', [modelId]).catch(()=>null);
      if (!m) return safeSend(chatId, '❌ Модель не найдена.');
      let gallery = [];
      try { gallery = JSON.parse(m.photos || '[]'); } catch {}
      const all = m.photo_main ? [m.photo_main, ...gallery] : gallery;
      if (all.length >= 8) {
        return safeSend(chatId, '⚠️ Максимум 8 фото. Сначала нажмите «Очистить».');
      }
      if (!m.photo_main) {
        await run('UPDATE models SET photo_main=? WHERE id=?', [fileId, modelId]).catch(()=>{});
      } else {
        gallery.push(fileId);
        await run('UPDATE models SET photos=? WHERE id=?', [JSON.stringify(gallery), modelId]).catch(()=>{});
      }
      const newCount = all.length + 1;
      const remaining = 8 - newCount;
      const doneText = remaining > 0
        ? `✅ Фото ${newCount}/8 сохранено!\n\nМожно добавить ещё ${remaining} фото.`
        : `✅ Фото ${newCount}/8 — галерея заполнена!`;
      const buttons = [];
      if (remaining > 0) {
        buttons.push([{ text: `➕ Добавить ещё фото (${newCount}/8)`, callback_data: `adm_gallery_${modelId}` }]);
      }
      buttons.push([{ text: '✅ Готово — показать карточку', callback_data: `adm_model_${modelId}` }]);
      buttons.push([{ text: '🗑 Очистить все фото',          callback_data: `adm_gallery_clear_${modelId}` }]);
      return safeSend(chatId, doneText, { reply_markup: { inline_keyboard: buttons } });
    }
    if (state.startsWith('adm_ef_') && state.endsWith('_photo')) {
      const modelId = parseInt(state.replace('adm_ef_','').split('_')[0]);
      await clearSession(chatId);
      return showPhotoGalleryManager(chatId, modelId);
    }
    // ── Broadcast with photo: receive photo
    if (state === 'adm_broadcast_photo_wait') {
      // New flow: text already set, go straight to preview
      if (d.broadcastText !== undefined || d.broadcastRecipients) {
        d.broadcastPhotoId = fileId;
        await setSession(chatId, 'adm_broadcast_preview', d);
        return previewBroadcast(chatId);
      }
      // Legacy flow: ask for caption
      d.broadcast_photo_id = fileId;
      await setSession(chatId, 'adm_broadcast_caption', d);
      return safeSend(chatId,
        `✅ Фото получено\\!\n\nТеперь введите подпись к рассылке \\(или нажмите «Пропустить»\\):`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '⏭ Пропустить подпись', callback_data: 'adm_broadcast_photo_nosend' }],
            [{ text: '❌ Отмена',             callback_data: 'admin_menu'                  }],
          ]}
        }
      );
    }
  });

  // ── Message handler ────────────────────────────────────────────────────────
  const SESSION_TIMEOUT_MS = 30 * 60 * 1000;
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId  = msg.chat.id;
    const text    = msg.text.trim();

    // ── Block check: silently ignore messages from blocked users ─────────────
    const isBlockedUser = !isAdmin(chatId) && !!(await get(`SELECT chat_id FROM blocked_clients WHERE chat_id=?`, [chatId]).catch(()=>null));
    if (isBlockedUser) return;

    const session = await getSession(chatId);
    const state   = session?.state || 'idle';
    const d       = sessionData(session);

    // ── Session timeout: сброс если сессия не активна > 30 минут ────────────
    if (state !== 'idle' && session?.updated_at) {
      const updatedAt = new Date(session.updated_at).getTime();
      if (!isNaN(updatedAt) && Date.now() - updatedAt > SESSION_TIMEOUT_MS) {
        clearTimeout(sessionTimers.get(chatId));
        sessionTimers.delete(chatId);
        await clearSession(chatId);
        await safeSend(chatId,
          '⏰ Сессия истекла\\. Действие отменено\\.', {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
          }
        );
        return isAdmin(chatId)
          ? showAdminMenu(chatId, msg.from?.first_name)
          : showMainMenu(chatId, msg.from?.first_name);
      }
    }

    // ── Reset inactivity timer for active booking states ──────────────────────
    if (ACTIVE_BOOKING_STATES.has(state)) {
      resetSessionTimer(chatId);
    }

    // ── ReplyKeyboard кнопки клиента ─────────────────────────────────────────
    if (state === 'idle' || state === 'check_status') {
      // Клиентские кнопки
      if (!isAdmin(chatId)) {
        if (text === '⭐ Топ-модели')           return showTopModels(chatId, 0);
        if (text === '💃 Каталог')             return showCatalog(chatId, null, 0);
        if (text === '📝 Подать заявку')       return bkStep1(chatId);
        if (text === '⚡ Быстрая заявка')       return bkQuickStart(chatId);
        if (text === '❤️ Избранное')            return showFavorites(chatId, 0);
        if (text === '💬 Менеджер')            return showContactManager(chatId);
        if (text === '📋 Мои заявки')          return showMyOrders(chatId);
        if (text === '🔍 Статус заявки') {
          await setSession(chatId, 'check_status', {});
          return safeSend(chatId, '🔍 Введите номер заявки (например, НМ-001):');
        }
        if (text === '💰 Прайс')               return showPricing(chatId);
        if (text === '❓ FAQ')                 return showFaq(chatId);
        if (text === '👤 Профиль')             return showUserProfile(chatId, msg.from.first_name);
        if (text === '📞 Контакты')            return showContacts(chatId);
      }
      // Кнопки администратора
      if (isAdmin(chatId)) {
        if (text === '📋 Заявки')          return showAdminOrders(chatId, '', 0);
        if (text === '💃 Модели')          return showAdminModels(chatId, 0);
        if (text === '📊 Статистика')      return showAdminStats(chatId);
        if (text === '🤖 Организм')        return showOrganismStatus(chatId);
        if (text === '📡 Фид агентов')     return showAgentFeed(chatId, 0);
        if (text === '💬 Обсуждения')      return showAgentDiscussions(chatId);
        if (text === '⚙️ Настройки')      return showAdminSettings(chatId);
        if (text === '📢 Рассылка')        return showBroadcast(chatId);
        if (text === '📤 Экспорт')         return exportOrders(chatId);
        if (text === '👥 Клиенты')         return showAdminClients(chatId, 0);
      }
    }

    // ── Admin: settings text inputs
    if (isAdmin(chatId)) {
      const settingStates = {
        'adm_set_greeting':           ['greeting',                    '📝 Приветствие обновлено!'],
        'adm_set_about':              ['about',                       'ℹ️ Текст «О нас» обновлён!'],
        'adm_set_phone':              ['contacts_phone',               '📞 Телефон обновлён!'],
        'adm_set_email':              ['contacts_email',               '📧 Email обновлён!'],
        'adm_set_insta':              ['contacts_insta',               '📸 Instagram обновлён!'],
        'adm_set_addr':               ['contacts_addr',                '📍 Адрес обновлён!'],
        'adm_set_pricing':            ['pricing',                      '💰 Прайс-лист обновлён!'],
        'adm_set_whatsapp':           ['contacts_whatsapp',            '📱 WhatsApp обновлён!'],
        'adm_set_site_url':           ['site_url',                     '🌐 URL сайта обновлён!'],
        'adm_set_mgr_hours':          ['manager_hours',                '🕐 Часы работы обновлены!'],
        'adm_set_mgr_reply':          ['manager_reply',                '💬 Авто-ответ обновлён!'],
        'adm_set_catalog_per_page':   ['catalog_per_page',             '📄 Кол-во на странице обновлено!'],
        'adm_set_catalog_title':      ['catalog_title',                '📌 Заголовок каталога обновлён!'],
        'adm_set_booking_min_budget': ['booking_min_budget',           '💰 Мин. бюджет обновлён!'],
        'adm_set_booking_confirm_msg':['booking_confirm_msg',          '💬 Сообщение брони обновлено!'],
        'adm_set_booking_thanks':     ['booking_thanks_text',          '🎉 Текст после бронирования обновлён!'],
        'adm_set_tg_channel':         ['tg_channel',                   '📣 Telegram канал обновлён!'],
        'adm_set_reviews_min':        ['reviews_min_completed',        '🔢 Мин. заявок обновлено!'],
        'adm_set_reviews_prompt':     ['reviews_prompt_text',          '📝 Приглашение к отзыву обновлено!'],
        'adm_set_cities_list':        ['cities_list',                  '🏙 Список городов обновлён!'],
        'adm_set_welcome_photo':      ['welcome_photo_url',            '🖼 Фото приветствия обновлено!'],
        'adm_set_main_menu_text':     ['main_menu_text',               '📋 Текст меню обновлён!'],
        'adm_set_model_max_photos':   ['model_max_photos',             '🖼 Лимит фото обновлён!'],
        'adm_set_client_max_orders':  ['client_max_active_orders',     '📋 Лимит заявок обновлён!'],
        'adm_set_client_msg_delay':   ['client_msg_delay_sec',         '⏱ Интервал сообщений обновлён!'],
        'adm_set_api_rate_limit':     ['api_rate_limit',               '🔒 Rate limit обновлён!'],
      };
      if (settingStates[state]) {
        const [key, okMsg] = settingStates[state];
        await setSetting(key, text);
        await logAdminAction(chatId, 'update_setting', 'setting', null, { key });
        await clearSession(chatId);
        return safeSend(chatId, `✅ ${okMsg}`, {
          reply_markup: { inline_keyboard: [[{ text: '⚙️ К настройкам', callback_data: 'adm_settings' }]] }
        });
      }

      // ── Add admin Telegram ID
      if (state === 'adm_add_admin_id') {
        const newId = text.replace(/[^0-9]/g, '');
        if (!newId) return safeSend(chatId, '❌ Некорректный ID. Введите числовой Telegram ID:');
        await run('UPDATE admins SET telegram_id=? WHERE id=(SELECT MIN(id) FROM admins WHERE telegram_id IS NULL OR telegram_id="")', [newId]).catch(()=>{});
        await clearSession(chatId);
        return safeSend(chatId, `✅ Telegram ID \`${newId}\` добавлен!\n\n⚠️ Для постоянного добавления — также добавьте его в ADMIN_TELEGRAM_IDS в .env файле.`, {
          reply_markup: { inline_keyboard: [[{ text: '← Администраторы', callback_data: 'adm_admins' }]] }
        });
      }

      // ── Scheduled broadcast: step 1 — text input
      if (state === 'adm_sched_bcast_text') {
        if (!text || text.length < 2) return safeSend(chatId, '❌ Текст слишком короткий. Введите текст рассылки:');
        await setSession(chatId, 'adm_sched_bcast_time', { sched_text: text });
        return safeSend(chatId,
          `📅 Текст принят\\!\n\nВведите дату и время рассылки в формате:\n\`2026\\-05\\-20 14:00\``,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_sched_bcast' }]] }
          }
        );
      }

      // ── Scheduled broadcast: step 2 — time input
      if (state === 'adm_sched_bcast_time') {
        const timeMatch = text.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})$/);
        if (!timeMatch) {
          return safeSend(chatId, '❌ Неверный формат\\. Введите дату в формате `2026-05-20 14:00`:', { parse_mode: 'MarkdownV2' });
        }
        const scheduledAt = `${timeMatch[1]} ${timeMatch[2]}:00`;
        const sessData = { ...d, sched_time: scheduledAt };
        await setSession(chatId, 'adm_sched_bcast_segment', sessData);
        return safeSend(chatId,
          `⏰ Время: *${esc(scheduledAt)}*\n\nВыберите сегмент получателей:`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [
              [{ text: '👥 Все клиенты',         callback_data: 'adm_sched_bcast_seg_all'       }],
              [{ text: '✅ Завершившие заявку',   callback_data: 'adm_sched_bcast_seg_completed' }],
              [{ text: '▶️ Активные клиенты',     callback_data: 'adm_sched_bcast_seg_active'    }],
              [{ text: '❌ Отмена',               callback_data: 'adm_sched_bcast'               }],
            ]}
          }
        );
      }

      // ── Broadcast text
      if (state === 'adm_broadcast_msg') {
        return sendBroadcast(chatId, text);
      }

      // ── Broadcast photo caption
      if (state === 'adm_broadcast_caption') {
        if (!d.broadcast_photo_id) return safeSend(chatId, '❌ Фото не найдено. Начните рассылку заново.');
        return sendBroadcastWithPhoto(chatId, d.broadcast_photo_id, text);
      }

      // ── Admin search order input
      if (state === 'adm_search_order_input') {
        return searchAdminOrders(chatId, text);
      }

      // ── Admin search model input
      if (state === 'adm_search_model_input') {
        const q2 = text.trim();
        await clearSession(chatId);
        const results = await query(`SELECT * FROM models WHERE name LIKE ? AND archived=0 LIMIT 10`, [`%${q2}%`]);
        if (!results.length) return safeSend(chatId, '❌ Модели не найдены\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← Список моделей', callback_data: 'adm_models_p_0_name_0' }]] }
        });
        const keyboard2 = results.map(m => [{ text: `${m.featured?'⭐':''}${m.name} (${m.city||'город не указан'})`, callback_data: `adm_model_${m.id}` }]);
        keyboard2.push([{ text: '← Список моделей', callback_data: 'adm_models_p_0_name_0' }]);
        return safeSend(chatId, `🔍 Найдено ${results.length} моделей по запросу "*${esc(q2)}*":`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: keyboard2 }
        });
      }

      // ── Order note input
      if (state.startsWith('adm_note_input_')) {
        const orderId = parseInt(state.replace('adm_note_input_', ''));
        if (!orderId) { await clearSession(chatId); return; }
        await run('INSERT INTO order_notes (order_id, admin_note) VALUES (?,?)', [orderId, text]);
        await clearSession(chatId);
        return safeSend(chatId, `✅ Заметка добавлена\\.`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] }
        });
      }

      // ── Internal note for order (adm_order_note_ flow)
      if (state === 'adm_note_order_id') {
        const orderId = d?.orderId;
        if (!orderId) { await clearSession(chatId); return; }
        const trimmed = text.slice(0, 1000);
        await run('UPDATE orders SET internal_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', [trimmed, orderId]);
        await clearSession(chatId);
        return safeSend(chatId, `✅ *Заметка сохранена\\!*\n\n${esc(trimmed)}`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] }
        });
      }

      // ── Personal message to client
      if (state.startsWith('adm_personal_msg_')) {
        const clientId = parseInt(state.replace('adm_personal_msg_', ''));
        await clearSession(chatId);
        try {
          await bot.sendMessage(clientId, `📨 *Сообщение от агентства:*\n\n${esc(text)}`, { parse_mode: 'MarkdownV2' });
          await safeSend(chatId, `✅ Сообщение отправлено клиенту ${clientId}\\.`, { parse_mode: 'MarkdownV2' });
        } catch {
          await safeSend(chatId, `❌ Не удалось отправить \\(клиент мог заблокировать бота\\)\\.`, { parse_mode: 'MarkdownV2' });
        }
        return;
      }
    }

    // ── Admin: add model text inputs
    if (isAdmin(chatId) && state.startsWith('adm_mdl_')) {
      const step = state.replace('adm_mdl_', '');
      if (step === 'name') {
        if (text.length < 2) return safeSend(chatId, '❌ Имя слишком короткое. Введите имя модели:');
        d.name = text; d._step = 'age';
        return showAddModelStep(chatId, d);
      }
      if (step === 'age') {
        d.age = parseInt(text) || null; d._step = 'height';
        return showAddModelStep(chatId, d);
      }
      if (step === 'height') {
        d.height = parseInt(text) || null; d._step = 'params';
        return showAddModelStep(chatId, d);
      }
      if (step === 'params') {
        const parts = text.split('/').map(x => parseInt(x.trim()));
        if (parts.length === 3 && parts.every(Boolean)) {
          [d.bust, d.waist, d.hips] = parts;
        }
        d._step = 'shoe'; return showAddModelStep(chatId, d);
      }
      if (step === 'shoe') {
        d.shoe_size = text; d._step = 'hair';
        return showAddModelStep(chatId, d);
      }
      if (step === 'instagram') {
        d.instagram = text.replace('@',''); d._step = 'bio';
        return showAddModelStep(chatId, d);
      }
      if (step === 'bio') {
        d.bio = text; d._step = 'photo';
        return showAddModelStep(chatId, d);
      }
    }

    // ── Admin: edit model field input
    if (isAdmin(chatId) && state.startsWith('adm_ef_')) {
      // state: adm_ef_{id}_{field}
      const parts = state.replace('adm_ef_','').split('_');
      const modelId = parseInt(parts[0]);
      const field   = parts.slice(1).join('_');
      const fieldMap = { name:'name', age:'age', height:'height', weight:'weight',
                         shoe_size:'shoe_size', instagram:'instagram', bio:'bio', eye_color:'eye_color',
                         hair_color:'hair_color', phone:'phone', city:'city', video_url:'video_url' };
      if (field === 'params') {
        const ps = text.split('/').map(x => parseInt(x.trim()));
        if (ps.length === 3 && ps.every(Boolean)) {
          await run('UPDATE models SET bust=?,waist=?,hips=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
            [ps[0],ps[1],ps[2],modelId]).catch(()=>{});
        }
      } else if (fieldMap[field] && /^[a-z_]+$/.test(fieldMap[field])) {
        const col = fieldMap[field];
        const val = ['age','height','weight'].includes(field) ? (parseInt(text)||null) : text;
        await run(`UPDATE models SET ${col}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, [val, modelId]).catch(()=>{});
      }
      await clearSession(chatId);
      return safeSend(chatId, '✅ Поле обновлено!', {
        reply_markup: { inline_keyboard: [[{ text: '✏️ Редактировать ещё', callback_data: `adm_editmodel_${modelId}` }, { text: '← Карточка', callback_data: `adm_model_${modelId}` }]] }
      });
    }

    // ── Admin reply to client
    if (isAdmin(chatId) && state === 'replying' && d.order_id) {
      const order = await get('SELECT * FROM orders WHERE id=?', [d.order_id]).catch(()=>null);
      if (!order) { await clearSession(chatId); return safeSend(chatId, RU.ORDER_NOT_FOUND); }
      const adm = await get('SELECT username FROM admins WHERE telegram_id=?', [String(chatId)]).catch(()=>null);
      await run('INSERT INTO messages (order_id,sender_type,sender_name,content) VALUES (?,?,?,?)',
        [d.order_id, 'admin', adm?.username||'Менеджер', text]);
      if (order.client_chat_id) await sendMessageToClient(order.client_chat_id, order.order_number, text);
      await clearSession(chatId);
      return safeSend(chatId, `✅ Сообщение отправлено клиенту ${order.client_name}.`, {
        reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${d.order_id}` }]] }
      });
    }

    // ── Leave review: text input
    if (state === 'leave_review_text') {
      if (!text) {
        return safeSend(chatId, '❌ Введите текст отзыва или отправьте «.» чтобы пропустить:');
      }
      // Allow "." as a shortcut to skip writing text
      const reviewText = text.trim() === '.' ? '' : text.trim();
      if (reviewText && reviewText.length < 3) {
        return safeSend(chatId, '❌ Отзыв слишком короткий. Напишите хотя бы несколько слов или отправьте «.» чтобы пропустить:');
      }
      const orderId = d.review_order_id;
      const rating  = d.review_rating || 5;
      let clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      let modelId = null;
      try {
        const ord = await get('SELECT client_name, model_id FROM orders WHERE id=?', [orderId]);
        if (ord?.client_name) clientName = ord.client_name;
        if (ord?.model_id) modelId = ord.model_id;
      } catch {}
      await run(
        'INSERT OR IGNORE INTO reviews (chat_id, order_id, client_name, rating, text, model_id, approved) VALUES (?,?,?,?,?,?,0)',
        [String(chatId), orderId || null, clientName, rating, reviewText, modelId]
      ).catch(e => console.error('[Bot] insert review:', e.message));
      await clearSession(chatId);

      // Bonus points for good review (rating 4-5)
      let reviewBonusMsg = '';
      if (rating >= 4) {
        await addLoyaltyPoints(chatId, 100, 'review', 'Бонус за отзыв').catch(()=>{});
        reviewBonusMsg = '\n\n🎁 *\\+100 баллов* начислено за отзыв\\!';
      }
      // Grant "first_review" achievement
      await grantAchievement(chatId, 'first_review').catch(()=>{});

      const adminIds2 = await getAdminChatIds();
      const reviewPreview = reviewText
        ? `\n\n${esc(reviewText.substring(0, 200))}`
        : ' _(текст не указан)_';
      await Promise.allSettled(adminIds2.map(id => safeSend(id,
        `⭐ Новый отзыв от *${esc(clientName)}*\nОценка: ${'⭐'.repeat(rating)}${reviewPreview}`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '✅ Модерация отзывов', callback_data: 'adm_reviews' }]] }
        }
      )));
      return safeSend(chatId,
        `✅ Спасибо за отзыв\\!\n\nОн появится после модерации\\.${reviewBonusMsg}`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
        }
      );
    }

    // ── Edit profile name
    if (state === 'profile_edit_name') {
      if (!text || text.trim().length < 2) {
        return safeSend(chatId, '❌ Введіть ім\'я (мінімум 2 символи):');
      }
      if (text.trim().length > 100) {
        return safeSend(chatId, '❌ Ім\'я занадто довге \\(максимум 100 символів\\):', { parse_mode: 'MarkdownV2' });
      }
      const newName = text.trim().slice(0, 100);
      await run(
        `UPDATE orders SET client_name=? WHERE client_chat_id=? AND id=(SELECT MAX(id) FROM orders WHERE client_chat_id=?)`,
        [newName, String(chatId), String(chatId)]
      ).catch(() => {});
      await clearSession(chatId);
      return safeSend(chatId, `✅ Ім'я оновлено: *${esc(newName)}*`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '👤 Мій профіль', callback_data: 'profile' }]] }
      });
    }

    // ── Edit profile phone
    if (state === 'profile_edit_phone') {
      if (!/^[\d\s+\-()]{7,20}$/.test(text)) {
        return safeSend(chatId, '❌ Введите корректный номер телефона:');
      }
      await run('UPDATE orders SET client_phone=? WHERE client_chat_id=?', [text, String(chatId)]).catch(()=>{});
      await clearSession(chatId);
      return safeSend(chatId, `✅ Телефон обновлён: *${esc(text)}*`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '👤 Мой профиль', callback_data: 'profile' }]] }
      });
    }

    // ── Status check
    if (state === 'check_status') {
      return showOrderStatus(chatId, text);
    }

    // ── Booking text inputs
    switch (state) {
      case 'bk_s2_date':
        if (!text || text.length < 3) return safeSend(chatId, '❌ Введите дату мероприятия:');
        d.event_date = text;
        return bkStep2Duration(chatId, d);

      case 'bk_s2_loc':
        if (!text) return safeSend(chatId, '❌ Введите место проведения:');
        d.location = text;
        return bkStep2Budget(chatId, d);

      case 'bk_s2_budget':
        d.budget = text;
        return bkStep2Comments(chatId, d);

      case 'bk_s2_comments':
        d.comments = text;
        return bkStep3Name(chatId, d);

      case 'bk_s3_name':
        if (text.length < 2) return safeSend(chatId, '❌ Введите имя и фамилию:');
        d.client_name = text;
        return bkStep3Phone(chatId, d);

      case 'bk_s3_phone':
        if (!/^[\d\s+\-()]{7,20}$/.test(text)) return safeSend(chatId, '❌ Введите корректный номер телефона:');
        d.client_phone = text;
        return bkStep3Email(chatId, d);

      case 'bk_s3_email':
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text)) return safeSend(chatId, '❌ Некорректный email. Введите email или нажмите «Пропустить»:');
        d.client_email = text;
        return bkStep3Telegram(chatId, d, msg.from.username);

      case 'bk_s3_tg':
        d.client_telegram = text.replace('@','');
        return bkStep4Confirm(chatId, d);

      default:
        // unknown booking state — handled by fallthrough logic below
        break;
    }

    // ── Вопрос менеджеру (через кнопку "Написать менеджеру")
    if (state === 'msg_to_manager') {
      const clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      const username   = msg.from.username ? `@${msg.from.username}` : '';
      const adminIds   = await getAdminChatIds();
      await Promise.allSettled(adminIds.map(id => safeSend(id,
        `💬 *Вопрос менеджеру*\nОт: ${esc(clientName)} ${esc(username)}\nTelegram ID: ${chatId}\n\n${esc(text)}`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[
            { text: '💬 Ответить', callback_data: `direct_reply_${chatId}` }
          ]]}
        }
      )));
      await clearSession(chatId);
      // Check "talkative" achievement after sending
      await checkAndGrantAchievements(chatId).catch(()=>{});
      return safeSend(chatId,
        '✅ Вопрос отправлен менеджеру\\. Мы ответим в ближайшее время\\!',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
        }
      );
    }

    // ── Ответ администратора напрямую клиенту (direct_reply)
    if (isAdmin(chatId) && state === 'direct_reply' && d.target_chat_id) {
      await safeSend(d.target_chat_id,
        `💬 *Сообщение от менеджера:*\n\n${esc(text)}`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '✍️ Ответить', callback_data: 'msg_manager_start' }]] }
        }
      );
      await clearSession(chatId);
      return safeSend(chatId, '✅ Ответ отправлен клиенту.');
    }

    // ── Client free message → forward to admin
    if (!isAdmin(chatId)) {
      const clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      const username   = msg.from.username ? `@${msg.from.username}` : '';
      const order      = await get('SELECT * FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 1', [String(chatId)]).catch(()=>null);
      if (order) {
        await run('INSERT INTO messages (order_id,sender_type,sender_name,content) VALUES (?,?,?,?)',
          [order.id, 'client', clientName, text]).catch(()=>{});
      }
      const adminIds = await getAdminChatIds();
      const header   = order
        ? `📩 *Сообщение от клиента*\nЗаявка: *${esc(order.order_number)}*\nКлиент: ${esc(clientName)} ${esc(username)}\n\n`
        : `📩 *Новое сообщение*\n${esc(clientName)} ${esc(username)}\n\n`;
      await Promise.allSettled(adminIds.map(id => safeSend(id, header + esc(text), {
        parse_mode: 'MarkdownV2',
        reply_markup: order ? { inline_keyboard: [[
          { text: '💬 Ответить',   callback_data: `adm_contact_${order.id}` },
          { text: '📋 Заявка',     callback_data: `adm_order_${order.id}`   },
        ]]} : undefined
      })));
      return safeSend(chatId, '✅ Сообщение передано менеджеру\\. Ответим в ближайшее время\\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
      });
    }
  });

  // Register new features (wishlist, quick booking, height search, dashboard)
  _registerNewFeatures();

  return { notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient, instance: bot };
}

// ─── Notifications ────────────────────────────────────────────────────────────

async function notifyAdmin(text, opts = {}) {
  if (!bot) return;
  const ids = await getAdminChatIds();
  await Promise.allSettled(ids.map(id => safeSend(id, text, { ...opts })));
}

async function notifyNewOrder(order) {
  if (!bot) return;
  let modelName = null;
  if (order.model_id) {
    const m = await get('SELECT name FROM models WHERE id=?', [order.model_id]).catch(()=>null);
    if (m) modelName = m.name;
  }
  const text =
    `🆕 *Новая заявка\\!*\n\n` +
    `📋 *${esc(order.order_number)}*\n` +
    `👤 ${esc(order.client_name)}\n📞 ${esc(order.client_phone)}\n` +
    (order.client_email    ? `📧 ${esc(order.client_email)}\n`                                : '') +
    (order.client_telegram ? `💬 @${esc(String(order.client_telegram).replace('@',''))}\n`   : '') +
    `\n🎭 ${esc(EVENT_TYPES[order.event_type]||order.event_type)}\n` +
    (order.event_date  ? `📅 ${esc(order.event_date)}\n`  : '') +
    (order.location    ? `📍 ${esc(order.location)}\n`    : '') +
    (order.budget      ? `💰 ${esc(order.budget)}\n`      : '') +
    (modelName         ? `💃 ${esc(modelName)}\n`         : '') +
    (order.comments    ? `\n💬 ${esc(order.comments)}`    : '');

  const ids = await getAdminChatIds();
  await Promise.allSettled(ids.map(id => safeSend(id, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [
        { text: '✅ Подтвердить', callback_data: `adm_confirm_${order.id}` },
        { text: '🔍 В работу',   callback_data: `adm_review_${order.id}`  },
        { text: '❌ Отклонить',  callback_data: `adm_reject_${order.id}`  },
      ],
      [{ text: '💬 Написать клиенту', callback_data: `adm_contact_${order.id}` }],
      [{ text: '📋 Открыть заявку',   callback_data: `adm_order_${order.id}`   }],
    ]}
  })));
}

async function notifyStatusChange(clientChatId, orderNumber, newStatus) {
  if (!bot || !clientChatId) return;

  // Check client notification preferences
  const clientPrefs = await get('SELECT notify_status FROM client_prefs WHERE chat_id=?', [clientChatId]).catch(() => null);
  if (clientPrefs && clientPrefs.notify_status === 0) {
    // Client opted out of status notifications
    return;
  }
  const msgs = {
    confirmed:   `✅ *Заявка ${esc(orderNumber)} подтверждена\\!*\n\nМенеджер свяжется с вами для уточнения деталей\\.`,
    reviewing:   `🔍 *Заявка ${esc(orderNumber)} принята в работу\\.*\n\nМы изучаем ваш запрос\\.`,
    in_progress: `▶️ *Заявка ${esc(orderNumber)} выполняется\\.*`,
    completed:   `🏁 *Заявка ${esc(orderNumber)} завершена\\!*\n\nСпасибо, что выбрали Nevesty Models\\! 💎`,
    cancelled:   `❌ *Заявка ${esc(orderNumber)} отклонена\\.*\n\nЕсли есть вопросы — свяжитесь с нами\\.`,
  };
  const text = msgs[newStatus];
  if (!text) return;

  // Кнопки действий для клиента при смене статуса
  const keyboard = { inline_keyboard: [
    [{ text: '💬 Написать менеджеру', callback_data: 'contact_mgr'  },
     { text: '📋 Мои заявки',        callback_data: 'my_orders'     }],
    [{ text: '📝 Повторить заявку',  callback_data: 'bk_start'      }],
  ]};

  // После завершения — предлагаем оставить отзыв (если отзывы включены)
  let reviewsEnabledForCompleted = false;
  let reviewOrderId = null;
  if (newStatus === 'completed') {
    try {
      const [reviewsEnabled, order] = await Promise.all([
        getSetting('reviews_enabled').catch(()=>null),
        get('SELECT id FROM orders WHERE order_number=?', [orderNumber]).catch(()=>null),
      ]);
      reviewOrderId = order?.id || null;
      if (reviewsEnabled === '1' && order) {
        reviewsEnabledForCompleted = true;
        keyboard.inline_keyboard.unshift([
          { text: '⭐ Оставить отзыв', callback_data: `leave_review_${order.id}` }
        ]);
      }
    } catch {}
  }

  // WhatsApp кнопка — если есть телефон и настроен WhatsApp контакт агентства
  try {
    const [orderRow, waContact] = await Promise.all([
      get('SELECT client_phone FROM orders WHERE order_number=?', [orderNumber]).catch(()=>null),
      getSetting('contacts_whatsapp').catch(()=>null),
    ]);
    if (orderRow?.client_phone && waContact) {
      const statusLabels = {
        confirmed: 'подтверждена', reviewing: 'принята в работу',
        in_progress: 'выполняется', completed: 'завершена', cancelled: 'отклонена',
      };
      const waMsg = `Здравствуйте! Статус вашей заявки №${orderNumber} изменён: ${statusLabels[newStatus] || newStatus}. Агентство Nevesty Models.`;
      const phone = orderRow.client_phone.replace(/[^0-9+]/g, '');
      const waUrl = `https://wa.me/${phone.replace(/^\+/, '')}?text=${encodeURIComponent(waMsg)}`;
      keyboard.inline_keyboard.push([{ text: '💬 Написать в WhatsApp', url: waUrl }]);
    }
  } catch {}

  await safeSend(clientChatId, text, { parse_mode: 'MarkdownV2', reply_markup: keyboard });

  // Отправляем отдельное приглашение к отзыву с задержкой (если отзывы включены и клиент прошёл порог)
  if (reviewsEnabledForCompleted && reviewOrderId) {
    try {
      const [reviewsMinCompleted, reviewsPromptText] = await Promise.all([
        getSetting('reviews_min_completed').catch(()=>null),
        getSetting('reviews_prompt_text').catch(()=>null),
      ]);
      const minCompleted = parseInt(reviewsMinCompleted) || 1;
      const completedCount = await get(
        "SELECT COUNT(*) as n FROM orders WHERE client_chat_id=? AND status='completed'",
        [String(clientChatId)]
      ).catch(() => ({ n: 0 }));
      if ((completedCount?.n || 0) >= minCompleted) {
        const promptText = reviewsPromptText ||
          'Понравилось сотрудничество? Оставьте отзыв — это займёт 1 минуту 😊';
        setTimeout(async () => {
          await safeSend(clientChatId, promptText, {
            reply_markup: {
              inline_keyboard: [
                [{ text: '⭐ Оставить отзыв', callback_data: `leave_review_${reviewOrderId}` }],
                [{ text: '⏩ Позже',          callback_data: 'review_skip'                   }],
              ],
            },
          }).catch(() => {});
        }, 3000);
      }
    } catch {}
  }
}

async function sendMessageToClient(clientChatId, orderNumber, text) {
  if (!bot || !clientChatId) return;
  await safeSend(clientChatId, `💬 *Сообщение от менеджера* \\(${esc(orderNumber)}\\):\n\n${esc(text)}`, { parse_mode: 'MarkdownV2' });
}

async function notifyPaymentSuccess(clientChatId, orderNumber) {
  if (!bot || !clientChatId) return;
  await safeSend(
    clientChatId,
    `✅ *Оплата получена\\!* Ваша заявка *${esc(orderNumber)}* подтверждена\\.\n\nСпасибо\\! Менеджер свяжется с вами для уточнения деталей\\.`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ]},
    }
  );
}

// ─── FAQ ──────────────────────────────────────────────────────────────────────

async function showFaq(chatId) {
  const faqItems = await query('SELECT * FROM faq WHERE active=1 ORDER BY sort_order ASC, id ASC').catch(() => []);
  const keyboard = faqItems.map(faq => [{ text: `❓ ${faq.question}`, callback_data: `faq_${faq.id}` }]);
  keyboard.push([{ text: '🏠 Главное меню', callback_data: 'main_menu' }]);

  return safeSend(chatId, '❓ *Часто задаваемые вопросы*\n\nВыберите вопрос:', {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard }
  });
}

// ─── User Profile ──────────────────────────────────────────────────────────────

async function showUserProfile(chatId, firstName) {
  try {
    const [orders, lastOrderFull] = await Promise.all([
      query(
        `SELECT o.id, o.status, o.created_at, o.order_number FROM orders o
         WHERE o.client_chat_id = ?
         ORDER BY o.created_at DESC LIMIT 50`,
        [String(chatId)]
      ),
      get(
        `SELECT client_name, client_phone, client_email FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 1`,
        [String(chatId)]
      ).catch(()=>null),
    ]);

    if (!orders.length) {
      return safeSend(chatId,
        `👤 *Мой профиль*\n\nИмя: *${esc(firstName || 'Гость')}*\n\nУ вас пока нет заявок\\. Оформите первую прямо сейчас\\!`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '📝 Оформить заявку', callback_data: 'bk_start'  }],
            [{ text: '🏠 Главное меню',    callback_data: 'main_menu' }],
          ]}
        }
      );
    }

    // Count by status
    const counts = {};
    for (const o of orders) {
      counts[o.status] = (counts[o.status] || 0) + 1;
    }

    const firstDate = orders[orders.length - 1]?.created_at
      ? new Date(orders[orders.length - 1].created_at).toLocaleDateString('ru')
      : 'неизвестно';
    const lastDate = orders[0]?.created_at
      ? new Date(orders[0].created_at).toLocaleDateString('ru')
      : 'неизвестно';

    const loyalty = await get(`SELECT * FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(() => null);
    const level = !loyalty ? '🥉 Бронзовый'
      : loyalty.total_earned >= 5000 ? '💎 Платиновый'
      : loyalty.total_earned >= 2000 ? '🥇 Золотой'
      : loyalty.total_earned >= 500 ? '🥈 Серебряный'
      : '🥉 Бронзовый';

    let text = `👤 *Мой профиль*\n\n`;
    text += `Имя: *${esc(firstName || lastOrderFull?.client_name || 'Гость')}*\n`;
    text += `💫 Уровень: *${esc(level)}*\n`;
    if (loyalty) text += `🎁 Баллов: *${loyalty.points}*\n`;
    if (lastOrderFull?.client_phone) text += `📞 Телефон: ${esc(lastOrderFull.client_phone)}\n`;
    if (lastOrderFull?.client_email) text += `📧 Email: ${esc(lastOrderFull.client_email)}\n`;
    text += `\n📋 *История заявок:*\n`;
    text += `Всего: ${orders.length}\n`;
    text += `Первая: ${esc(firstDate)}\n`;
    text += `Последняя: ${esc(lastDate)}\n\n`;

    const statusOrder = ['new','reviewing','confirmed','in_progress','completed','cancelled'];
    for (const st of statusOrder) {
      if (counts[st]) {
        const label = STATUS_LABELS[st] || st;
        text += `  ${label}: ${counts[st]}\n`;
      }
    }

    // Last 3 orders for quick access
    const recentBtns = orders.slice(0, 3).map(o => [{
      text: `${o.order_number}  ${STATUS_LABELS[o.status]||o.status}`,
      callback_data: `client_order_${o.id}`
    }]);

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...recentBtns,
        [{ text: '📋 Все заявки',        callback_data: 'my_orders'             }],
        [{ text: '🏆 Достижения',        callback_data: 'my_achievements'       },
         { text: '💫 Баллы',             callback_data: 'loyalty'               }],
        [{ text: '✏️ Изменить контакты', callback_data: 'profile_edit_contacts' }],
        [{ text: '🔔 Уведомления',        callback_data: 'client_notif_settings' }],
        [{ text: '📝 Новая заявка',       callback_data: 'bk_start'             }],
        [{ text: '🏠 Главное меню',       callback_data: 'main_menu'            }],
      ]}
    });
  } catch (e) { console.error('[Bot] showUserProfile:', e.message); }
}

// ─── Client notification preferences ─────────────────────────────────────────

async function showClientNotificationSettings(chatId) {
  const prefs = await get('SELECT * FROM client_prefs WHERE chat_id=?', [chatId])
    .catch(() => null) || { notify_status: 1, notify_promo: 1, notify_review: 1 };

  const onOff = v => v ? '🔔 Вкл' : '🔕 Выкл';

  return safeSend(chatId, '🔔 *Настройки уведомлений*\n\nВыберите что вы хотите получать:', {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: `${onOff(prefs.notify_status)} Статус заявки`, callback_data: 'client_notif_status' }],
      [{ text: `${onOff(prefs.notify_promo)} Акции и предложения`, callback_data: 'client_notif_promo' }],
      [{ text: `${onOff(prefs.notify_review)} Просьба оставить отзыв`, callback_data: 'client_notif_review' }],
      [{ text: '← Назад', callback_data: 'profile' }]
    ]}
  });
}

// ─── AI Factory Panel ─────────────────────────────────────────────────────────

const FACTORY_DB_PATH = require('path').join(__dirname, '..', 'factory', 'factory.db');

function factoryDbGet(sql, params = []) {
  return new Promise((resolve, reject) => {
    const sqlite3 = require('sqlite3').verbose();
    const fdb = new sqlite3.Database(FACTORY_DB_PATH, sqlite3.OPEN_READONLY, err => {
      if (err) return resolve(null);
      fdb.get(sql, params, (e, row) => { fdb.close(); e ? resolve(null) : resolve(row || null); });
    });
  });
}

function factoryDbAll(sql, params = []) {
  return new Promise((resolve, reject) => {
    const sqlite3 = require('sqlite3').verbose();
    const fdb = new sqlite3.Database(FACTORY_DB_PATH, sqlite3.OPEN_READONLY, err => {
      if (err) return resolve([]);
      fdb.all(sql, params, (e, rows) => { fdb.close(); e ? resolve([]) : resolve(rows || []); });
    });
  });
}

async function showFactoryPanel(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [lastCycle, lastDecision, pendingCount, runningExp] = await Promise.all([
      factoryDbGet('SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1'),
      factoryDbGet("SELECT * FROM decisions ORDER BY created_at DESC LIMIT 1"),
      factoryDbGet("SELECT COUNT(*) as n FROM growth_actions WHERE status='pending'"),
      factoryDbGet("SELECT COUNT(*) as n FROM experiments WHERE status='running'"),
    ]);

    const score = lastCycle?.health_score ?? '—';
    const icon = score >= 70 ? '💚' : score >= 50 ? '🟡' : '🔴';
    const elapsed = lastCycle ? `${lastCycle.duration_s || '?'}с` : 'нет данных';
    const cycleTime = lastCycle?.finished_at
      ? new Date(lastCycle.finished_at).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow', hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' })
      : '—';

    const decisionLine = lastDecision
      ? `\n🧠 Решение CEO: ${lastDecision.decision_type} — ${(lastDecision.rationale || '').slice(0, 80)}`
      : '';

    const text =
      `🏭 AI Startup Factory\n\n` +
      `${icon} Health Score: ${score}%\n` +
      `🕐 Последний цикл: ${cycleTime} (${elapsed})\n` +
      `💡 Действий в очереди: ${pendingCount?.n ?? 0}\n` +
      `🧪 Экспериментов активных: ${runningExp?.n ?? 0}` +
      decisionLine;

    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '🔄 Запустить цикл', callback_data: 'adm_factory_run' },
         { text: '💡 Growth Actions', callback_data: 'adm_factory_growth' }],
        [{ text: '🧪 Эксперименты',  callback_data: 'adm_factory_exp' },
         { text: '📋 Решения CEO',   callback_data: 'adm_factory_decisions' }],
        [{ text: '🎯 AI Задачи',     callback_data: 'adm_factory_tasks' },
         { text: '🧪 A/B Тесты',    callback_data: 'adm_experiments' }],
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ]}
    });
  } catch (e) {
    console.error('[Factory] showFactoryPanel:', e.message);
    return safeSend(chatId, '🏭 AI Factory ещё не запущен.\n\nЗапустите: `pm2 start nevesty-factory`', {
      reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
    });
  }
}

async function showFactoryGrowth(chatId, page = 0) {
  if (!isAdmin(chatId)) return;
  const LIMIT = 8;
  const offset = page * LIMIT;
  const [actions, totalRow] = await Promise.all([
    factoryDbAll(
      "SELECT * FROM growth_actions WHERE status='pending' ORDER BY priority DESC, created_at DESC LIMIT ? OFFSET ?",
      [LIMIT, offset]
    ),
    factoryDbGet("SELECT COUNT(*) as n FROM growth_actions WHERE status='pending'"),
  ]);

  const total = totalRow?.n ?? 0;
  if (!actions.length) {
    return safeSend(chatId, '💡 Нет pending growth actions.\n\nЗапустите цикл Factory чтобы сгенерировать новые.', {
      reply_markup: { inline_keyboard: [
        [{ text: '🔄 Запустить цикл', callback_data: 'adm_factory_run' }],
        [{ text: '← Factory', callback_data: 'adm_factory' }],
      ]}
    });
  }

  for (const a of actions) {
    const channelIcon = { telegram: '📱', instagram: '📸', tiktok: '🎵', seo: '🔍', email: '📧', direct: '📞' }[a.channel] || '💡';
    const text = `${channelIcon} [${a.channel}/${a.action_type}] приоритет ${a.priority}\n\n${(a.content || '').slice(0, 600)}`;
    await safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [[
        { text: '✅ Выполнено', callback_data: `adm_factory_done_${a.id}` },
      ]]}
    });
  }

  const nav = [];
  if (page > 0) nav.push({ text: '◀ Назад', callback_data: `adm_factory_growth_${page - 1}` });
  if (offset + LIMIT < total) nav.push({ text: 'Ещё ▶', callback_data: `adm_factory_growth_${page + 1}` });

  return safeSend(chatId, `Показано ${offset + 1}–${Math.min(offset + LIMIT, total)} из ${total}`, {
    reply_markup: { inline_keyboard: [
      nav.length ? nav : [],
      [{ text: '← Factory', callback_data: 'adm_factory' }],
    ].filter(r => r.length) }
  });
}

async function showFactoryDecisions(chatId) {
  if (!isAdmin(chatId)) return;
  const decisions = await factoryDbAll(
    'SELECT * FROM decisions ORDER BY created_at DESC LIMIT 10'
  );
  if (!decisions.length) {
    return safeSend(chatId, 'Нет решений CEO.', {
      reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] }
    });
  }
  const icons = { create_mvp:'📦', scale:'🚀', kill:'💀', iterate:'🔧', grow:'📣', experiment:'🧪', optimize:'⚙️', monitor:'👁' };
  const lines = decisions.map(d =>
    `${icons[d.decision_type] || '•'} ${d.decision_type} — ${(d.rationale || '').slice(0, 80)}`
  );
  return safeSend(chatId, `📋 Решения CEO (последние 10)\n\n${lines.join('\n')}`, {
    reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] }
  });
}

async function showFactoryExperiments(chatId) {
  if (!isAdmin(chatId)) return;
  const exps = await factoryDbAll(
    "SELECT * FROM experiments ORDER BY started_at DESC LIMIT 8"
  );
  if (!exps.length) {
    return safeSend(chatId, 'Нет экспериментов.', {
      reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] }
    });
  }
  const statusIcon = { running:'🔵', concluded:'✅' };
  const resultIcon = { scale:'🚀', kill:'💀', iterate:'🔧' };
  const lines = exps.map(e =>
    `${statusIcon[e.status] || '•'} ${e.name}\n` +
    `   A=${e.conversion_a ?? '—'}% / B=${e.conversion_b ?? '—'}%` +
    (e.result ? ` → ${resultIcon[e.result] || ''} ${e.result}` : '')
  );
  return safeSend(chatId, `🧪 Эксперименты\n\n${lines.join('\n\n')}`, {
    reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] }
  });
}

// ─── Factory Tasks (CEO growth_actions synced from factory) ──────────────────

async function showFactoryTasks(chatId, page) {
  if (!isAdmin(chatId)) return;
  if (!page) page = 0;
  const LIMIT = 6;
  const offset = page * LIMIT;
  try {
    const [tasks, totalRow] = await Promise.all([
      query("SELECT * FROM factory_tasks WHERE status='pending' ORDER BY priority DESC, created_at DESC LIMIT ? OFFSET ?", [LIMIT, offset]),
      get("SELECT COUNT(*) as n FROM factory_tasks WHERE status='pending'"),
    ]);
    const total = totalRow ? totalRow.n : 0;
    if (!tasks || !tasks.length) {
      return safeSend(chatId, '🎯 Нет активных AI-задач.\n\nЗапустите цикл Factory чтобы сгенерировать новые задачи.', {
        reply_markup: { inline_keyboard: [[{ text: '🔄 Запустить цикл', callback_data: 'adm_factory_run' }], [{ text: '← Factory', callback_data: 'adm_factory' }]] }
      });
    }
    const priIcon = function (p) { return p >= 8 ? '🔴' : p >= 5 ? '🟡' : '🟢'; };
    const dIcons = { marketing: '📣', sales: '💼', product: '📦', tech: '🛠', hr: '👥', operations: '⚙', creative: '🎨', finance: '💰', research: '🔬', analytics: '📊' };
    for (const t of tasks) {
      const dept = t.department || '';
      const dicon = dIcons[dept] || '🎯';
      const parts = [dicon + ' AI-задача #' + t.id, '', priIcon(t.priority || 5) + ' Приоритет: ' + (t.priority || 5) + '/10'];
      if (dept) parts.push('🏢 Отдел: ' + dept);
      if (t.expected_impact) parts.push('📈 Эффект: ' + t.expected_impact);
      parts.push('', (t.action || '').slice(0, 400));
      await safeSend(chatId, parts.join('\n'), {
        reply_markup: { inline_keyboard: [[{ text: '✅ Выполнено', callback_data: 'factory_task_done_' + t.id }, { text: '🗑 Пропустить', callback_data: 'factory_task_skip_' + t.id }]] }
      });
    }
    const nav = [];
    if (page > 0) nav.push({ text: '◀ Назад', callback_data: 'adm_factory_tasks_' + (page - 1) });
    if (offset + LIMIT < total) nav.push({ text: 'Ещё ▶', callback_data: 'adm_factory_tasks_' + (page + 1) });
    return safeSend(chatId, 'Показано ' + (offset + 1) + '–' + Math.min(offset + LIMIT, total) + ' из ' + total, {
      reply_markup: { inline_keyboard: [...(nav.length ? [nav] : []), [{ text: '← Factory', callback_data: 'adm_factory' }]] }
    });
  } catch (e) {
    console.error('[Bot] showFactoryTasks:', e.message);
    return safeSend(chatId, 'Ошибка загрузки AI-задач.', {
      reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] }
    });
  }
}

// ─── A/B Experiments (synced from AI Factory) ────────────────────────────────

async function showAdminExperiments(chatId) {
  if (!isAdmin(chatId)) return;
  const experiments = await query(`SELECT * FROM ab_experiments ORDER BY created_at DESC LIMIT 10`).catch(() => []);

  if (!experiments.length) {
    return safeSend(chatId, `🧪 *A/B Эксперименты*\n\nЭкспериментов пока нет\\. Factory сгенерирует их при следующем цикле\\.`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Factory панель', callback_data: 'adm_factory' }]] }
    });
  }

  const statusIcon = { proposed: '💡', running: '▶️', applied: '✅', skipped: '❌' };
  const lines = experiments.map((e, i) =>
    `${i + 1}\\. ${statusIcon[e.status] || '💡'} ${esc(e.hypothesis?.slice(0, 80) || '')}\\.\\.\\.\n   _Усилие: ${esc(e.effort || '?')} | Ожидание: ${esc(e.expected_lift || '?')}_`
  ).join('\n\n');

  return safeSend(chatId, `🧪 *A/B Эксперименты* \\(${experiments.length}\\)\n\n${lines}`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '← Factory панель', callback_data: 'adm_factory' }]
    ]}
  });
}

// ─── Admin Reviews ────────────────────────────────────────────────────────────

async function showAdminReviews(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [pendingCount, approvedCount] = await Promise.all([
      get("SELECT COUNT(*) as n FROM reviews WHERE approved=0 AND (status IS NULL OR status != 'rejected')"),
      get("SELECT COUNT(*) as n FROM reviews WHERE approved=1"),
    ]);
    const text = `*⭐ Управление отзывами*\n\nОжидают одобрения: *${esc(String(pendingCount.n))}*\nОдобрено: *${esc(String(approvedCount.n))}*`;
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [
          { text: `⏳ Ожидают (${pendingCount.n})`,  callback_data: 'adm_reviews_pending'  },
          { text: `✅ Одобрены (${approvedCount.n})`, callback_data: 'adm_reviews_approved' },
        ],
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminReviews:', e.message); }
}

async function showAdminReviewsList(chatId, filter) {
  if (!isAdmin(chatId)) return;
  try {
    let reviews;
    if (filter === 'pending') {
      reviews = await query("SELECT * FROM reviews WHERE approved=0 AND (status IS NULL OR status != 'rejected') ORDER BY created_at DESC").catch(()=>[]);
    } else {
      reviews = await query("SELECT * FROM reviews WHERE approved=1 ORDER BY created_at DESC").catch(()=>[]);
    }

    if (!reviews.length) {
      const label = filter === 'pending' ? 'ожидающих одобрения' : 'одобренных';
      return safeSend(chatId, `Нет ${label} отзывов.`, {
        reply_markup: { inline_keyboard: [[{ text: '← К отзывам', callback_data: 'adm_reviews' }]] }
      });
    }

    for (const r of reviews) {
      const stars = '⭐'.repeat(Math.max(1, Math.min(5, r.rating || 1)));
      const preview = r.text ? r.text.slice(0, 100) + (r.text.length > 100 ? '…' : '') : '';
      const msgText = `*Отзыв \\#${esc(String(r.id))}*\n👤 ${esc(r.client_name)}\n${stars}\n\n${esc(preview)}`;
      const btns = [];
      if (filter === 'pending') {
        btns.push({ text: '✅ Одобрить', callback_data: `rev_approve_${r.id}` });
        btns.push({ text: '❌ Отклонить', callback_data: `rev_reject_${r.id}` });
      } else {
        btns.push({ text: '🗑 Удалить',  callback_data: `rev_delete_${r.id}` });
        btns.push({ text: '❌ Отклонить', callback_data: `rev_reject_${r.id}` });
      }
      await safeSend(chatId, msgText, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [btns] }
      });
    }

    const label = filter === 'pending' ? 'ожидают одобрения' : 'одобрено';
    return safeSend(chatId, `Всего ${label}: ${reviews.length}`, {
      reply_markup: { inline_keyboard: [[{ text: '← К отзывам', callback_data: 'adm_reviews' }]] }
    });
  } catch (e) { console.error('[Bot] showAdminReviewsList:', e.message); }
}

// ─── Топ-модели ───────────────────────────────────────────────────────────────

async function showTopModels(chatId, page = 0) {
  try {
    const perPage = 5;
    const models = await query(
      `SELECT m.*,
        (SELECT COUNT(*) FROM orders o WHERE o.model_id=m.id AND o.status NOT IN ('cancelled','new')) as book_count
       FROM models m WHERE m.available=1
       ORDER BY m.featured DESC, book_count DESC, m.id ASC`
    ).catch(() => []);

    if (!models.length) {
      return safeSend(chatId, '📭 Нет доступных моделей\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'main_menu' }]] }
      });
    }

    const total = models.length;
    const slice = models.slice(page * perPage, page * perPage + perPage);
    const modelBtns = slice.map((m, i) => {
      const star = m.featured ? '⭐ ' : '';
      const rank = page * perPage + i + 1;
      return [{ text: `${rank}. ${star}${m.name}  ·  ${m.height}см`, callback_data: `cat_model_${m.id}` }];
    });
    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `cat_top_${page-1}` });
    if ((page+1)*perPage < total) nav.push({ text: '▶️', callback_data: `cat_top_${page+1}` });

    return safeSend(chatId,
      `⭐ *Топ\\-модели Nevesty Models*\n\n_Рейтинг по популярности и востребованности_\n\nВсего: ${total} ${ru_plural(total,'модель','модели','моделей')}`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          ...modelBtns,
          ...(nav.length ? [nav] : []),
          [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }],
          [{ text: '🏠 Меню', callback_data: 'main_menu' }],
        ]}
      }
    );
  } catch (e) { console.error('[Bot] showTopModels:', e.message); }
}

// ─── Написать менеджеру ───────────────────────────────────────────────────────

async function showContactManager(chatId) {
  const [phone, insta, waPhone, mgrHours] = await Promise.all([
    getSetting('contacts_phone').catch(() => '+7 (900) 000-00-00'),
    getSetting('contacts_insta').catch(() => '@nevesty_models'),
    getSetting('contacts_whatsapp').catch(() => null).then(v => v || getSetting('agency_phone').catch(() => '')),
    getSetting('manager_hours').catch(() => ''),
  ]);
  await setSession(chatId, 'msg_to_manager', {});
  const waDigits = (waPhone || '').replace(/\D/g, '');
  let msgText =
    `💬 *Связаться с менеджером*\n\n` +
    `Напишите ваш вопрос прямо здесь — менеджер ответит в течение часа\\.\n\n` +
    `Или свяжитесь напрямую:\n` +
    `📞 ${esc(phone)}\n` +
    `📸 Instagram: ${esc(insta)}`;
  if (mgrHours && mgrHours.trim()) {
    msgText += `\n🕐 Часы работы: ${esc(mgrHours)}`;
  }
  const inlineRows = [
    [{ text: '✍️ Написать вопрос сейчас', callback_data: 'msg_manager_start' }],
  ];
  if (waDigits) {
    inlineRows.push([{ text: '📱 WhatsApp', url: `https://wa.me/${waDigits}` }]);
  }
  inlineRows.push([{ text: '🏠 Главное меню', callback_data: 'main_menu' }]);
  return safeSend(chatId, msgText, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: inlineRows }
  });
}

// ─── Получить контакт модели ──────────────────────────────────────────────────

async function showModelContact(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена.');
    const parts = [];
    if (m.phone)     parts.push(`📞 Телефон: ${esc(m.phone)}`);
    if (m.instagram) parts.push(`📸 Instagram: @${esc(m.instagram)}`);
    if (!parts.length) {
      return safeSend(chatId,
        `📱 *Контакт модели ${esc(m.name)}*\n\nДля получения контакта обратитесь к менеджеру\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '💬 Написать менеджеру', callback_data: 'contact_mgr' }],
            [{ text: '← Назад', callback_data: `cat_model_${modelId}` }],
          ]}
        }
      );
    }
    return safeSend(chatId,
      `📱 *Контакт: ${esc(m.name)}*\n\n${parts.join('\n')}`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '📝 Заказать модель', callback_data: `bk_model_${m.id}` }],
          [{ text: '← Назад', callback_data: `cat_model_${modelId}` }],
        ]}
      }
    );
  } catch (e) { console.error('[Bot] showModelContact:', e.message); }
}

// ─── О нас ────────────────────────────────────────────────────────────────────

async function showAboutUs(chatId) {
  const about   = await getSetting('about').catch(() => 'Мы работаем с 2018 года. Более 200 моделей в базе.');
  const phone   = await getSetting('contacts_phone').catch(() => '');
  const pricing = await getSetting('pricing').catch(() => '');
  return safeSend(chatId,
    `ℹ️ *О нас — Nevesty Models*\n\n${esc(about)}\n\n` +
    `💎 *Почему мы:*\n` +
    `• Более 200 профессиональных моделей\n` +
    `• Работаем по всей России\n` +
    `• Договор и полная юридическая чистота\n` +
    `• Fashion, Commercial, Events, Runway\n\n` +
    (phone ? `📞 ${esc(phone)}` : ''),
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '💃 Смотреть каталог', callback_data: 'cat_cat__0' }],
        [{ text: '📞 Контакты',         callback_data: 'contacts'   }],
        [{ text: '🏠 Меню',             callback_data: 'main_menu'  }],
      ]}
    }
  );
}

// ─── Прайс-лист ───────────────────────────────────────────────────────────────

async function showPricing(chatId) {
  const pricing = await getSetting('pricing').catch(() => '');
  const pricingText = pricing || `💰 *Наши пакеты услуг*

🥉 *Базовый пакет*
• 1 модель на 4 часа
• Базовый образ
• Подходит для небольших мероприятий
💵 от 15 000 ₽

🥈 *Стандартный пакет*
• 1\\-2 модели на 8 часов
• Профессиональный образ
• Фото\\-съёмка включена
💵 от 30 000 ₽

🥇 *Премиум пакет*
• 3\\+ модели, любое время
• Полный стилинг и визаж
• Личный менеджер
💵 от 60 000 ₽

_Цены ориентировочные\\. Точная стоимость согласовывается индивидуально\\._`;

  return safeSend(chatId, pricingText, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      [{ text: '📋 Оформить заявку', callback_data: 'bk_start' }],
      [{ text: '📞 Связаться с менеджером', callback_data: 'msg_manager_start' }],
      [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
    ]}
  });
}

// ─── Каталог по городу ────────────────────────────────────────────────────────

async function showCatalogByCity(chatId, city, page = 0) {
  try {
    const perPage = parseInt(await getSetting('catalog_per_page') || '5');
    const models = city
      ? await query('SELECT * FROM models WHERE available=1 AND city=? ORDER BY id', [city])
      : await query('SELECT * FROM models WHERE available=1 ORDER BY id');

    if (!models.length) {
      return safeSend(chatId, `📭 Моделей в городе «${city}» нет\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '💃 Все модели', callback_data: 'cat_cat__0' }]] }
      });
    }

    const total = models.length;
    const slice = models.slice(page * perPage, page * perPage + perPage);
    const modelBtns = slice.map(m => [{ text: `${m.name} · ${m.height}см`, callback_data: `cat_model_${m.id}` }]);
    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `cat_city_${city}_${page-1}` });
    if ((page+1)*perPage < total) nav.push({ text: '▶️', callback_data: `cat_city_${city}_${page+1}` });

    return safeSend(chatId,
      `🏙 *Модели — ${esc(city)}*\n\nНайдено: ${total}`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          ...modelBtns,
          ...(nav.length ? [nav] : []),
          [{ text: '🏠 Меню', callback_data: 'main_menu' }],
        ]}
      }
    );
  } catch (e) { console.error('[Bot] showCatalogByCity:', e.message); }
}

// ─── Поиск модели по параметрам (БЛОК 2.4) ───────────────────────────────────
// In-memory фильтры для каждого пользователя
const searchFilters = new Map(); // chatId → { height_min, height_max, age_min, age_max, category, city }

function getSearchFilters(chatId) {
  if (!searchFilters.has(String(chatId))) searchFilters.set(String(chatId), {});
  return searchFilters.get(String(chatId));
}

async function showSearchMenu(chatId) {
  const f = getSearchFilters(chatId);

  // Height range label
  const heightRanges = [
    { key: '155_160', label: '155–160 см', min: 155, max: 160 },
    { key: '161_165', label: '161–165 см', min: 161, max: 165 },
    { key: '166_170', label: '166–170 см', min: 166, max: 170 },
    { key: '171_175', label: '171–175 см', min: 171, max: 175 },
    { key: '176_180', label: '176–180 см', min: 176, max: 180 },
    { key: '181_999', label: '181+ см',    min: 181, max: 999 },
  ];
  const ageRanges = [
    { key: '18_22', label: '18–22 года', min: 18, max: 22 },
    { key: '23_27', label: '23–27 лет',  min: 23, max: 27 },
    { key: '28_32', label: '28–32 лет',  min: 28, max: 32 },
    { key: '33_99', label: '33+ лет',    min: 33, max: 99 },
  ];

  // Height buttons (2 per row)
  const heightBtns = [];
  for (let i = 0; i < heightRanges.length; i += 2) {
    const row = heightRanges.slice(i, i + 2).map(r => {
      const active = f.height_min === r.min && f.height_max === r.max;
      return { text: (active ? '✅ ' : '📏 ') + r.label, callback_data: `srch_h_${r.key}` };
    });
    heightBtns.push(row);
  }

  // Age buttons (2 per row)
  const ageBtns = [];
  for (let i = 0; i < ageRanges.length; i += 2) {
    const row = ageRanges.slice(i, i + 2).map(r => {
      const active = f.age_min === r.min && f.age_max === r.max;
      return { text: (active ? '✅ ' : '🎂 ') + r.label, callback_data: `srch_a_${r.key}` };
    });
    ageBtns.push(row);
  }

  // Category buttons
  const catDefs = [
    { key: 'fashion',    label: '👗 Fashion'    },
    { key: 'commercial', label: '📷 Commercial' },
    { key: 'events',     label: '🎉 Events'     },
  ];
  const catBtns = catDefs.map(c => {
    const active = f.category === c.key;
    return { text: (active ? '✅ ' : '') + c.label, callback_data: `srch_c_${c.key}` };
  });

  // City buttons from settings (up to 8)
  const citiesSetting = await getSetting('cities_list').catch(() => '');
  const cities = citiesSetting ? citiesSetting.split(',').map(c => c.trim()).filter(Boolean).slice(0, 8) : [];
  const cityBtns = cities.map(city => {
    const active = f.city === city;
    return { text: (active ? '✅ ' : '🏙 ') + city, callback_data: `srch_city_${city}` };
  });

  // Build active filter summary
  const activeParts = [];
  if (f.height_min != null) {
    const r = heightRanges.find(r => r.min === f.height_min);
    if (r) activeParts.push(`📏 ${r.label}`);
  }
  if (f.age_min != null) {
    const r = ageRanges.find(r => r.min === f.age_min);
    if (r) activeParts.push(`🎂 ${r.label}`);
  }
  if (f.category) activeParts.push(`🏷 ${f.category}`);
  if (f.city)     activeParts.push(`🏙 ${f.city}`);

  const summaryLine = activeParts.length
    ? `\n\n_Выбрано: ${esc(activeParts.join(', '))}_`
    : `\n\n_Фильтры не выбраны — показать всех_`;

  const keyboard = [
    ...heightBtns,
    ...ageBtns,
    [catBtns[0], catBtns[1], catBtns[2]],
    ...(cityBtns.length ? [cityBtns.slice(0, 4)] : []),
    ...(cityBtns.length > 4 ? [cityBtns.slice(4)] : []),
    [{ text: '🔄 Сбросить', callback_data: 'srch_reset' },
     { text: '🔍 Найти',    callback_data: 'srch_go'    }],
    [{ text: '← Назад',   callback_data: 'main_menu'   }],
  ];

  return safeSend(chatId,
    `🔍 *Поиск моделей*${summaryLine}`,
    { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } }
  );
}

async function showSearchResults(chatId, filters, page = 0) {
  try {
    page = parseInt(page) || 0;
    const perPage = 5;

    const conditions = ['available=1'];
    const params = [];
    if (filters.height_min != null) { conditions.push('height >= ?'); params.push(filters.height_min); }
    if (filters.height_max != null && filters.height_max < 999) { conditions.push('height <= ?'); params.push(filters.height_max); }
    if (filters.age_min != null)    { conditions.push('age >= ?');    params.push(filters.age_min); }
    if (filters.age_max != null && filters.age_max < 99) { conditions.push('age <= ?');    params.push(filters.age_max); }
    if (filters.category)           { conditions.push('category = ?'); params.push(filters.category); }
    if (filters.city)               { conditions.push('city = ?');     params.push(filters.city); }

    const where = conditions.join(' AND ');
    const models = await query(`SELECT * FROM models WHERE ${where} ORDER BY name`, params);
    const total  = models.length;

    if (!total) {
      return safeSend(chatId,
        `🔍 *Поиск моделей*\n\nПо выбранным фильтрам ничего не найдено\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '🔄 Изменить фильтры', callback_data: 'cat_search'  }],
            [{ text: '💃 Все модели',        callback_data: 'cat_cat__0' }],
          ]}
        }
      );
    }

    const slice = models.slice(page * perPage, page * perPage + perPage);
    let text = `🔍 *Результаты поиска*\n\nНайдено: *${total}* ${ru_plural(total,'модель','модели','моделей')}\n\n`;
    slice.forEach((m, i) => {
      text += `${page * perPage + i + 1}\\. *${esc(m.name)}*`;
      if (m.city)   text += ` · ${esc(m.city)}`;
      if (m.height) text += ` · ${m.height} см`;
      if (m.age)    text += ` · ${m.age} лет`;
      text += '\n';
    });

    const modelBtns = slice.map(m => [{
      text: `👁 ${m.name}`,
      callback_data: `srch_view_${m.id}`
    }]);

    const nav = [];
    if (page > 0)                     nav.push({ text: '◀️', callback_data: `srch_page_${page-1}` });
    if ((page+1)*perPage < total)     nav.push({ text: '▶️', callback_data: `srch_page_${page+1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...modelBtns,
        ...(nav.length ? [nav] : []),
        [{ text: '🔍 Изменить поиск', callback_data: 'cat_search'  }],
        [{ text: '🏠 Главное меню',   callback_data: 'main_menu'   }],
      ]}
    });
  } catch (e) { console.error('[Bot] showSearchResults:', e.message); }
}

// ─── Публичные отзывы ─────────────────────────────────────────────────────────

async function showPublicReviews(chatId, page) {
  page = parseInt(page) || 0;
  try {
    const perPage = 5;
    const totalRow = await get('SELECT COUNT(*) as n FROM reviews WHERE approved=1').catch(()=>({n:0}));
    const total = totalRow.n;

    if (!total) {
      return safeSend(chatId,
        '📭 Пока нет опубликованных отзывов\\.\n\nБудьте первым\\!', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '⭐ Оставить отзыв', callback_data: 'leave_review_0' }],
            [{ text: '🏠 Главное меню',    callback_data: 'main_menu'      }],
          ]}
        }
      );
    }

    const reviews = await query(
      'SELECT * FROM reviews WHERE approved=1 ORDER BY created_at DESC LIMIT ? OFFSET ?',
      [perPage, page * perPage]
    ).catch(()=>[]);

    let text = `⭐ *Отзывы клиентов Nevesty Models*\n\n`;
    reviews.forEach((r, i) => {
      const stars = '⭐'.repeat(Math.max(1, Math.min(5, r.rating || 5)));
      const date  = r.created_at ? new Date(r.created_at).toLocaleDateString('ru') : '';
      text += `${page * perPage + i + 1}\\. *${esc(r.client_name)}* ${stars}`;
      if (date) text += ` \\(${esc(date)}\\)`;
      text += `\n_${esc(r.text)}_`;
      if (r.admin_reply) text += `\n💬 _${esc(r.admin_reply)}_`;
      text += '\n\n';
    });

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `show_reviews_${page-1}` });
    if ((page+1)*perPage < total) nav.push({ text: '▶️', callback_data: `show_reviews_${page+1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...(nav.length ? [nav] : []),
        [{ text: '⭐ Оставить отзыв', callback_data: 'leave_review_0' }],
        [{ text: '🏠 Главное меню',    callback_data: 'main_menu'      }],
      ]}
    });
  } catch (e) { console.error('[Bot] showPublicReviews:', e.message); }
}

// ─── Оставить отзыв ───────────────────────────────────────────────────────────

async function startLeaveReview(chatId, orderId) {
  orderId = parseInt(orderId) || 0;

  // Validate order if orderId provided
  if (orderId) {
    const order = await get(
      'SELECT id, order_number, status FROM orders WHERE id=? AND client_chat_id=?',
      [orderId, String(chatId)]
    ).catch(() => null);
    if (!order) {
      return safeSend(chatId, RU.ORDER_NOT_FOUND, {
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
      });
    }
    if (order.status !== 'completed') {
      return safeSend(chatId, '⚠️ Отзыв можно оставить только после завершения заявки\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] },
      });
    }
    // Check duplicate review for this order
    const existing = await get(
      'SELECT id FROM reviews WHERE chat_id=? AND order_id=?',
      [String(chatId), orderId]
    ).catch(() => null);
    if (existing) {
      return safeSend(chatId, '✅ Вы уже оставили отзыв для этой заявки\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
      });
    }
  }

  const text = orderId
    ? `⭐ *Оставить отзыв о заявке*\n\nОцените работу агентства по 5\\-балльной шкале:`
    : `⭐ *Оставить отзыв*\n\nОцените работу агентства Nevesty Models\\!`;

  const ratingRow = [1,2,3,4,5].map(n => ({
    text: '⭐'.repeat(n),
    callback_data: `review_rating_${orderId}_${n}`
  }));

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [
      ratingRow,
      [{ text: '❌ Отмена', callback_data: 'main_menu' }],
    ]}
  });
}

// ─── Повторить заявку ─────────────────────────────────────────────────────────

async function repeatOrder(chatId, orderId) {
  try {
    const o = await get(
      'SELECT * FROM orders WHERE id=? AND client_chat_id=?',
      [orderId, String(chatId)]
    );
    if (!o) {
      return safeSend(chatId, RU.ORDER_NOT_FOUND, {
        reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] }
      });
    }

    const prefill = {
      client_name:     o.client_name,
      client_phone:    o.client_phone,
      client_email:    o.client_email || null,
      client_telegram: o.client_telegram || null,
    };
    if (o.model_id) {
      const m = await get('SELECT id,name,available FROM models WHERE id=?', [o.model_id]).catch(()=>null);
      if (m?.available) {
        prefill.model_id   = m.id;
        prefill.model_name = m.name;
      }
    }

    await setSession(chatId, 'bk_s1', prefill);
    await safeSend(chatId,
      `🔁 *Повторная заявка*\n\nКонтактные данные предзаполнены из предыдущей заявки\\.`,
      { parse_mode: 'MarkdownV2' }
    );
    return bkStep2EventType(chatId, prefill);
  } catch (e) { console.error('[Bot] repeatOrder:', e.message); }
}

// ─── Редактировать профиль ────────────────────────────────────────────────────

async function startEditProfile(chatId) {
  try {
    const lastOrder = await get(
      'SELECT client_name, client_phone, client_email FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 1',
      [String(chatId)]
    ).catch(()=>null);

    let text = `✏️ *Редактировать контакты*\n\n`;
    if (lastOrder) {
      text += `Текущие данные:\n`;
      text += `👤 ${esc(lastOrder.client_name)}\n`;
      text += `📞 ${esc(lastOrder.client_phone)}\n`;
      if (lastOrder.client_email) text += `📧 ${esc(lastOrder.client_email)}\n`;
      text += `\n_Изменение телефона обновит данные во всех ваших заявках_`;
    } else {
      text += `_У вас пока нет заявок\\. Данные сохранятся автоматически при первой заявке\\._`;
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '👤 Змінити ім\'я',   callback_data: 'profile_edit_name'  }],
        [{ text: '📞 Изменить телефон', callback_data: 'profile_edit_phone' }],
        [{ text: '← Профиль',           callback_data: 'profile'            }],
      ]}
    });
  } catch (e) { console.error('[Bot] startEditProfile:', e.message); }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE A: Избранные модели (Wishlist) ───────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function getFavoriteIds(chatId) {
  try {
    const rows = await query('SELECT model_id FROM favorites WHERE chat_id=?', [String(chatId)]);
    return rows.map(r => r.model_id);
  } catch { return []; }
}

async function showFavorites(chatId, page = 0) {
  try {
    const favs = await query(
      `SELECT f.model_id, m.name, m.height, m.category, m.available
       FROM favorites f
       JOIN models m ON m.id = f.model_id
       WHERE f.chat_id = ?
       ORDER BY f.created_at DESC`,
      [String(chatId)]
    );

    if (!favs.length) {
      return safeSend(chatId,
        '❤️ *Избранные модели*\n\nУ вас пока нет избранных моделей\\.\n\nОткройте карточку модели и нажмите ❤️ чтобы добавить\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '💃 Каталог моделей', callback_data: 'cat_cat__0' }],
            [{ text: '🏠 Главное меню',    callback_data: 'main_menu'  }],
          ]}
        }
      );
    }

    const perPage = 5;
    const total   = favs.length;
    const slice   = favs.slice(page * perPage, page * perPage + perPage);

    let text = `❤️ *Избранные модели* \\(${total}\\)\n\n`;
    const modelBtns = slice.map(m => {
      const avail = m.available ? '🟢' : '🔴';
      text += `${avail} *${esc(m.name)}* — ${m.height || '?'}см, ${esc(m.category || '')}\n`;
      return [
        { text: `${avail} ${m.name}`, callback_data: `cat_model_${m.model_id}` },
        { text: '❌ Убрать',          callback_data: `fav_remove_${m.model_id}` }
      ];
    });

    const nav = [];
    if (page > 0)                      nav.push({ text: '◀️', callback_data: `fav_list_${page - 1}` });
    if ((page + 1) * perPage < total)  nav.push({ text: '▶️', callback_data: `fav_list_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...modelBtns,
        ...(nav.length ? [nav] : []),
        [{ text: '📝 Оформить заявку', callback_data: 'bk_start'  }],
        [{ text: '🏠 Главное меню',    callback_data: 'main_menu' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showFavorites:', e.message); }
}

async function addFavorite(chatId, modelId) {
  try {
    const m = await get('SELECT id, name FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена\\.');
    await run('INSERT OR IGNORE INTO favorites (chat_id, model_id) VALUES (?,?)', [String(chatId), modelId]);
    return safeSend(chatId,
      `❤️ *${esc(m.name)}* добавлена в избранное\\!`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '❤️ Мои избранные', callback_data: 'fav_list_0'           }],
          [{ text: '← К модели',       callback_data: `cat_model_${modelId}` }],
        ]}
      }
    );
  } catch (e) { console.error('[Bot] addFavorite:', e.message); }
}

async function removeFavorite(chatId, modelId) {
  try {
    const m = await get('SELECT name FROM models WHERE id=?', [modelId]).catch(() => null);
    await run('DELETE FROM favorites WHERE chat_id=? AND model_id=?', [String(chatId), modelId]);
    return safeSend(chatId,
      `💔 *${esc(m?.name || 'Модель')}* убрана из избранного\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '❤️ Мои избранные', callback_data: 'fav_list_0' }],
          [{ text: '🏠 Главное меню',   callback_data: 'main_menu'  }],
        ]}
      }
    );
  } catch (e) { console.error('[Bot] removeFavorite:', e.message); }
}

// ─── Wishlist (wishlists table — mirrors favorites) ───────────────────────────

async function showWishlist(chatId, page = 0) {
  try {
    const PAGE_SIZE = 5;
    const rows = await query(
      `SELECT m.id, m.name, m.category, m.city, m.featured FROM wishlists w
       JOIN models m ON m.id = w.model_id AND (m.archived IS NULL OR m.archived = 0)
       WHERE w.chat_id = ?
       ORDER BY w.created_at DESC
       LIMIT ? OFFSET ?`,
      [String(chatId), PAGE_SIZE + 1, page * PAGE_SIZE]
    ).catch(() => []);

    const hasMore = rows.length > PAGE_SIZE;
    const items = rows.slice(0, PAGE_SIZE);

    if (items.length === 0 && page === 0) {
      return safeSend(chatId,
        '❤️ *Список избранного пуст*\n\nДобавляйте понравившихся моделей кнопкой ❤️ в их профиле\\.',
        { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] } }
      );
    }

    const totalRow = await get('SELECT COUNT(*) as c FROM wishlists WHERE chat_id=?', [String(chatId)]).catch(() => ({ c: items.length }));

    let text = `❤️ *Обрані моделі* \\(${totalRow.c}\\)\n\n`;
    const keyboard = [];
    for (const m of items) {
      const star = m.featured ? '⭐ ' : '';
      const cat  = MODEL_CATEGORIES[m.category] || m.category || '';
      const city = m.city ? ` · ${esc(m.city)}` : '';
      text += `${star}*${esc(m.name)}* · ${esc(cat)}${city}\n`;
      keyboard.push([
        { text: '👁 Переглянути', callback_data: `fav_view_${m.id}` },
        { text: '❌ Видалити',    callback_data: `fav_remove_${m.id}` },
      ]);
    }

    const navRow = [];
    if (page > 0) navRow.push({ text: '◀️ Назад',  callback_data: `fav_list_${page - 1}` });
    if (hasMore)  navRow.push({ text: 'Далі ▶️',   callback_data: `fav_list_${page + 1}` });
    if (navRow.length) keyboard.push(navRow);
    keyboard.push([{ text: '💃 Каталог', callback_data: 'cat_cat__0' }, { text: '🏠 Головна', callback_data: 'main_menu' }]);

    return safeSend(chatId, text,
      { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } }
    );
  } catch (e) { console.error('[Bot] showWishlist:', e.message); }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE B: Быстрая заявка (Quick Booking) ────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function bkQuickStart(chatId) {
  await setSession(chatId, 'bk_quick_name', {});
  resetSessionTimer(chatId);
  return safeSend(chatId,
    `⚡ *Быстрая заявка*\n\nМенеджер свяжется с вами и уточнит все детали\\.\n\n📝 Шаг 1/2 — Введите ваше имя:`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📋 Полная форма', callback_data: 'bk_start'  }],
        [{ text: '❌ Отменить',     callback_data: 'main_menu' }],
      ]}
    }
  );
}

async function bkQuickPhone(chatId, data) {
  await setSession(chatId, 'bk_quick_phone', data);
  resetSessionTimer(chatId);
  return safeSend(chatId,
    `⚡ *Быстрая заявка*\n\n✅ Имя: *${esc(data.quick_name)}*\n\n📝 Шаг 2/2 — Введите номер телефона:\n_Пример: \\+7\\(999\\)123\\-45\\-67_`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'main_menu' }]] }
    }
  );
}

async function bkQuickSubmit(chatId, data) {
  try {
    const orderNum = generateOrderNumber();
    await run(
      `INSERT INTO orders (order_number,client_name,client_phone,event_type,comments,client_chat_id,status)
       VALUES (?,?,?,'other',?,?,'new')`,
      [orderNum, data.quick_name, data.quick_phone, 'Быстрая заявка — менеджер уточнит детали', String(chatId)]
    );
    const order = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]);
    await clearSession(chatId);
    await safeSend(chatId,
      `⚡ *Заявка принята\\!*\n\nНомер: *${esc(orderNum)}*\n\nМенеджер позвонит на *${esc(data.quick_phone)}* в ближайшее время\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '📋 Мои заявки',  callback_data: 'my_orders'  }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ]}
      }
    );
    if (order) notifyNewOrder(order);
  } catch (e) {
    console.error('[Bot] bkQuickSubmit:', e.message);
    await clearSession(chatId);
    return safeSend(chatId, '❌ Ошибка при отправке\\.  Попробуйте позже\\.', { parse_mode: 'MarkdownV2' });
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE D: Поиск по росту — ввод диапазона вручную ──────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function showHeightSearchInput(chatId) {
  await setSession(chatId, 'search_height', {});
  return safeSend(chatId,
    `📏 *Поиск моделей по росту*\n\nВведите диапазон роста в формате:\n*170\\-180* или одно значение *175*\n\n_Или выберите быстрый диапазон:_`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '160–165 см', callback_data: 'cat_search_height_160-165' },
         { text: '165–170 см', callback_data: 'cat_search_height_165-170' }],
        [{ text: '170–175 см', callback_data: 'cat_search_height_170-175' },
         { text: '175–185 см', callback_data: 'cat_search_height_175-185' }],
        [{ text: '← Поиск',       callback_data: 'cat_search' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ]}
    }
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE E: Расширенный дашборд администратора ───────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function showAdminDashboard(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const now      = new Date();
    const todayStr = now.toISOString().slice(0, 10);
    const weekAgo  = new Date(now - 7  * 86400000).toISOString().slice(0, 10);
    const monthAgo = new Date(now - 30 * 86400000).toISOString().slice(0, 10);

    const [today, week, month, topModels, newClients, totalDone] = await Promise.all([
      get(`SELECT COUNT(*) as n FROM orders WHERE date(created_at)=?`, [todayStr]),
      get(`SELECT COUNT(*) as n FROM orders WHERE date(created_at)>=?`, [weekAgo]),
      get(`SELECT COUNT(*) as n FROM orders WHERE date(created_at)>=?`, [monthAgo]),
      query(
        `SELECT m.name, COUNT(o.id) as cnt
         FROM models m JOIN orders o ON o.model_id=m.id
         WHERE o.status NOT IN ('cancelled')
         GROUP BY m.id ORDER BY cnt DESC LIMIT 3`
      ),
      get(
        `SELECT COUNT(DISTINCT client_chat_id) as n FROM orders
         WHERE date(created_at)>=? AND client_chat_id IS NOT NULL AND client_chat_id!=''`,
        [monthAgo]
      ),
      get(`SELECT COUNT(*) as n FROM orders WHERE status='completed'`),
    ]);

    let text = `📊 *Дашборд Nevesty Models*\n\n`;
    text += `📅 Заявок сегодня: *${today.n}*\n`;
    text += `📅 За неделю: *${week.n}*\n`;
    text += `📅 За месяц: *${month.n}*\n`;
    text += `🏁 Завершено всего: *${totalDone.n}*\n`;
    text += `👥 Новых клиентов за месяц: *${newClients?.n || 0}*\n\n`;

    if (topModels.length) {
      text += `🏆 *Топ\\-3 модели по заказам:*\n`;
      topModels.forEach((m, i) => {
        text += `  ${i + 1}\\. *${esc(m.name)}* — ${m.cnt} заказов\n`;
      });
    } else {
      text += `_Заказов с привязанными моделями пока нет_\n`;
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📋 Заявки',     callback_data: 'adm_orders__0' },
         { text: '📊 Статистика', callback_data: 'adm_stats'     }],
        [{ text: '← Меню',       callback_data: 'admin_menu'    }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminDashboard:', e.message); }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── Hook new features into the bot after initBot() ──────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

function _registerNewFeatures() {
  if (!bot) return;

  // ── Additional callback_query handlers ─────────────────────────────────────
  bot.on('callback_query', async (q) => {
    const chatId = q.message.chat.id;
    const data   = q.data;
    try { await bot.answerCallbackQuery(q.id); } catch {}

    // Favorites / Wishlist list
    if (data === 'fav_list') {
      return showWishlist(chatId, 0);
    }
    if (data.startsWith('fav_list_')) {
      const page = parseInt(data.replace('fav_list_', '')) || 0;
      return showWishlist(chatId, page);
    }

    // View model from wishlist
    if (data.startsWith('fav_view_')) {
      const modelId = parseInt(data.replace('fav_view_', ''));
      return showModel(chatId, modelId);
    }

    // Favorites add/remove
    if (data.startsWith('fav_add_')) {
      return addFavorite(chatId, parseInt(data.replace('fav_add_', '')));
    }
    if (data.startsWith('fav_remove_')) {
      return removeFavorite(chatId, parseInt(data.replace('fav_remove_', '')));
    }

    // Category search
    if (data.startsWith('cat_search_cat_')) {
      const cat = data.replace('cat_search_cat_', '');
      if (!['fashion', 'commercial', 'events'].includes(cat)) return;
      return showCatalog(chatId, cat, 0);
    }

    // Quick booking
    if (data === 'bk_quick') return bkQuickStart(chatId);

    // Height search manual input
    if (data === 'search_height_input') return showHeightSearchInput(chatId);

    // srch_height / srch_age — text-input prompts (alias callbacks)
    if (data === 'srch_height') {
      await setSession(chatId, 'search_height', {});
      return safeSend(chatId,
        '📏 Введите диапазон роста, например: *165\\-175*\nИли просто одно число: *170*',
        { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: [[{ text: '🔙 Назад', callback_data: 'cat_search' }]] } }
      );
    }
    if (data === 'srch_age') {
      await setSession(chatId, 'search_age', {});
      return safeSend(chatId,
        '🎂 Введите диапазон возраста, например: *22\\-28*\nИли одно число: *25*',
        { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: [[{ text: '🔙 Назад', callback_data: 'cat_search' }]] } }
      );
    }

    // Admin dashboard
    if (data === 'adm_dashboard') {
      if (!isAdmin(chatId)) return;
      return showAdminDashboard(chatId);
    }

    // ── Model comparison
    if (data.startsWith('compare_add_')) {
      const modelId = parseInt(data.replace('compare_add_', ''));
      return addToCompare(chatId, modelId);
    }
    if (data === 'compare_show') {
      return showComparison(chatId);
    }
    if (data === 'compare_clear') {
      _compareLists.delete(String(chatId));
      return safeSend(chatId, '🗑 Список сравнения очищен\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] }
      });
    }

    // ── AI bio generator
    if (data.startsWith('adm_ai_bio_apply_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_ai_bio_apply_', ''));
      const session = await getSession(chatId);
      const d = sessionData(session);
      const bio = d?.ai_bio;
      if (!bio) {
        return safeSend(chatId, '❌ Описание не найдено\. Сгенерируйте заново\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '🤖 Сгенерировать', callback_data: `adm_ai_bio_${modelId}` }]] }
        });
      }
      await run('UPDATE models SET bio=? WHERE id=?', [bio, modelId]).catch(() => {});
      await clearSession(chatId);
      return safeSend(chatId, '✅ Описание сохранено\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          [{ text: '✏️ Редактировать', callback_data: `adm_editmodel_${modelId}` }],
          [{ text: '← Карточка',       callback_data: `adm_model_${modelId}`      }],
        ]}
      });
    }
    if (data.startsWith('adm_ai_bio_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_ai_bio_', ''));
      return generateAiBio(chatId, modelId);
    }
  });

  // ── Additional message state handlers ─────────────────────────────────────
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId  = msg.chat.id;
    const text    = msg.text.trim();
    const session = await getSession(chatId);
    const state   = session?.state || 'idle';
    const d       = sessionData(session);

    // Quick booking: collect name
    if (state === 'bk_quick_name') {
      if (text.length < 2) return safeSend(chatId, '❌ Введите имя (минимум 2 символа):');
      if (text.length > 100) return safeSend(chatId, '❌ Имя слишком длинное (максимум 100 символов):');
      d.quick_name = text.slice(0, 100);
      return bkQuickPhone(chatId, d);
    }

    // Quick booking: collect phone
    if (state === 'bk_quick_phone') {
      if (!/^[\d\s+\-()]{7,20}$/.test(text)) {
        return safeSend(chatId, '❌ Введите корректный номер телефона:');
      }
      d.quick_phone = text;
      return bkQuickSubmit(chatId, d);
    }

    // Height search: manual range input
    if (state === 'search_height') {
      const clean = text.replace(/\s/g, '');
      const rangeMatch  = clean.match(/^(\d{3})-(\d{3})$/);
      const singleMatch = clean.match(/^(\d{3})$/);
      if (rangeMatch) {
        await clearSession(chatId);
        const f = getSearchFilters(chatId);
        f.height_min = parseInt(rangeMatch[1]); f.height_max = parseInt(rangeMatch[2]);
        return showSearchResults(chatId, f, 0);
      } else if (singleMatch) {
        await clearSession(chatId);
        const h = parseInt(singleMatch[1]);
        const f = getSearchFilters(chatId);
        f.height_min = h; f.height_max = h;
        return showSearchResults(chatId, f, 0);
      } else {
        return safeSend(chatId,
          '❌ Неверный формат\\. Введите диапазон, например: *170\\-180* или одно значение *175*',
          { parse_mode: 'MarkdownV2' }
        );
      }
    }

    // Age search: manual range input (via srch_age callback)
    if (state === 'search_age') {
      const clean = text.replace(/\s/g, '');
      const rangeMatch  = clean.match(/^(\d{1,2})-(\d{1,2})$/);
      const singleMatch = clean.match(/^(\d{1,2})$/);
      if (rangeMatch) {
        await clearSession(chatId);
        const f = getSearchFilters(chatId);
        f.age_min = parseInt(rangeMatch[1]); f.age_max = parseInt(rangeMatch[2]);
        return showSearchResults(chatId, f, 0);
      } else if (singleMatch) {
        await clearSession(chatId);
        const a = parseInt(singleMatch[1]);
        const f = getSearchFilters(chatId);
        f.age_min = a; f.age_max = a + 5;
        return showSearchResults(chatId, f, 0);
      } else {
        return safeSend(chatId,
          '❌ Неверный формат\\. Введите диапазон, например: *22\\-28* или одно значение *25*',
          { parse_mode: 'MarkdownV2' }
        );
      }
    }
  });
}

module.exports = { initBot, notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient, notifyPaymentSuccess, _registerNewFeatures };
