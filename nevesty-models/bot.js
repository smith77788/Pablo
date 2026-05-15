require('dotenv').config();
const crypto = require('crypto');
const TelegramBot = require('node-telegram-bot-api');
const { query, run, get, generateOrderNumber } = require('./database');
const { RU } = require('./utils/strings');
const STRINGS = require('./strings');
const {
  STATUS_LABELS,
  VALID_STATUSES: _VALID_STATUSES,
  EVENT_TYPES,
  CATEGORIES,
  MODEL_CATEGORIES,
  MODEL_HAIR_COLORS,
  MODEL_EYE_COLORS,
  DURATIONS,
  MAX_MESSAGE_LENGTH,
  MAX_CAPTION_LENGTH,
  SESSION_TIMEOUT_MS,
} = require('./utils/constants');
let mailer;
try {
  mailer = require('./services/mailer');
} catch {
  mailer = null;
}
let smsService;
try {
  smsService = require('./services/sms');
} catch {
  smsService = null;
}

const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '')
  .split(',')
  .map(s => s.trim())
  .filter(Boolean);
const SITE_URL = process.env.SITE_URL || 'http://localhost:3000';
const WEBHOOK_URL = process.env.WEBHOOK_URL || '';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || crypto.randomBytes(32).toString('hex');

let bot = null;

// ─── Session timers (in-memory, cleared on restart) ───────────────────────────
const sessionTimers = new Map();

// ─── Session soft-reminder timers (booking-only, 15 min proactive nudge) ─────
const SESSION_REMINDER_MS = 15 * 60 * 1000; // 15 minutes — fires before hard timeout
const sessionReminderTimers = {};

// ─── Session warning timers (fires 2 min before hard timeout) ─────────────────
const SESSION_WARNING_BEFORE_MS = 2 * 60 * 1000; // warn 2 minutes before expiry
const sessionWarningTimers = new Map();

const ACTIVE_BOOKING_STATES = new Set([
  'bk_s1',
  'bk_s1_add',
  'bk_s2_event',
  'bk_s2_date',
  'bk_s2_dur',
  'bk_s2_loc',
  'bk_s2_budget',
  'bk_s2_comments',
  'bk_s3_name',
  'bk_s3_phone',
  'bk_s3_email',
  'bk_s3_tg',
  'bk_s4',
  'bk_repeat_confirm',
  'leave_review_text',
  'bk_quick_name',
  'bk_quick_phone',
  'profile_edit_name',
  'profile_edit_phone',
  'profile_edit_email',
  'ai_match_desc',
  'techspec_input',
]);

// States that have active user input in progress (booking + admin flows)
function isActiveInputState(state) {
  if (!state || state === 'idle') return false;
  if (ACTIVE_BOOKING_STATES.has(state)) return true;
  // Admin input states
  if (state.startsWith('adm_mdl_')) return true;
  if (state === 'model_reg_phone') return true;
  if (state.startsWith('adm_set_')) return true;
  if (state.startsWith('adm_gallery_')) return true;
  if (state.startsWith('adm_ai_bio_preview_')) return true;
  if (state.startsWith('adm_add_busy_')) return true;
  if (state.startsWith('adm_ef_')) return true;
  if (state.startsWith('adm_note_input_')) return true;
  if (state.startsWith('adm_personal_msg_')) return true;
  if (
    [
      'adm_broadcast_msg',
      'adm_broadcast_preview',
      'adm_broadcast_photo_wait',
      'adm_broadcast_edit_text',
      'adm_broadcast_caption',
      'adm_sched_bcast_text',
      'adm_sched_bcast_time',
      'adm_sched_bcast_segment',
      'adm_search_order_input',
      'adm_order_search_input',
      'adm_search_notes_input',
      'adm_search_model_input',
      'adm_note_order_id',
      'adm_add_admin_id',
      'replying',
      'direct_reply',
      'msg_to_manager',
      'check_status',
      'search_height',
      'search_age',
      'broadcast_schedule_time',
    ].includes(state)
  )
    return true;
  return false;
}

function clearSessionWarning(chatId) {
  clearTimeout(sessionWarningTimers.get(chatId));
  sessionWarningTimers.delete(chatId);
}

function setSessionWarning(chatId) {
  clearSessionWarning(chatId);
  const warningDelay = SESSION_TIMEOUT_MS - SESSION_WARNING_BEFORE_MS;
  if (warningDelay <= 0) return; // timeout too short to warn
  const t = setTimeout(async () => {
    sessionWarningTimers.delete(chatId);
    try {
      const sess = await getSession(chatId);
      if (sess?.state && isActiveInputState(sess.state)) {
        await bot.sendMessage(
          chatId,
          '⏰ Бронирование будет отменено через 2 минуты\\. Продолжите ввод или нажмите кнопку ниже\\.',
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [[{ text: '▶️ Продолжить', callback_data: 'session_keepalive' }]],
            },
          }
        );
      }
    } catch {
      /* session may already be gone */
    }
  }, warningDelay);
  if (t?.unref) t.unref();
  sessionWarningTimers.set(chatId, t);
}

function resetSessionTimer(chatId) {
  clearTimeout(sessionTimers.get(chatId));
  clearSessionWarning(chatId);
  setSessionWarning(chatId);
  setSessionReminder(chatId);
  const timer = setTimeout(async () => {
    clearSessionReminder(chatId);
    clearSessionWarning(chatId);
    try {
      const sess = await getSession(chatId);
      const state = sess?.state;
      if (state && isActiveInputState(state)) {
        await clearSession(chatId);
        const adminUser = isAdmin(chatId);
        if (ACTIVE_BOOKING_STATES.has(state)) {
          // Booking flow timeout — offer restart
          await safeSend(
            chatId,
            '⏰ *Время сессии истекло\\. Бронирование отменено\\.*\n\nВаши данные не сохранены\\. Хотите начать заново?',
            {
              parse_mode: 'MarkdownV2',
              reply_markup: {
                inline_keyboard: [
                  [{ text: '🔄 Начать заново', callback_data: 'bk_start' }],
                  [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
                ],
              },
            }
          );
        } else if (adminUser) {
          // Admin flow timeout
          await safeSend(
            chatId,
            '⏰ *Время ввода истекло\\.*\n\nДействие отменено из\\-за длительного бездействия\\.',
            {
              parse_mode: 'MarkdownV2',
              reply_markup: {
                inline_keyboard: [
                  [{ text: '🔄 Продолжить', callback_data: 'admin_menu' }],
                  [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
                ],
              },
            }
          );
        } else {
          // Client non-booking flow timeout
          await safeSend(
            chatId,
            '⏰ *Время ожидания истекло\\.*\n\nДействие сброшено\\. Напишите /start чтобы вернуться в меню\\.',
            {
              parse_mode: 'MarkdownV2',
              reply_markup: {
                inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]],
              },
            }
          );
        }
      }
    } catch {}
    sessionTimers.delete(chatId);
  }, SESSION_TIMEOUT_MS);
  sessionTimers.set(chatId, timer);
}

// ─── Booking soft-reminder helpers ────────────────────────────────────────────
function clearSessionReminder(chatId) {
  if (sessionReminderTimers[chatId]) {
    clearTimeout(sessionReminderTimers[chatId]);
    delete sessionReminderTimers[chatId];
  }
}

function setSessionReminder(chatId) {
  clearSessionReminder(chatId);
  const t = setTimeout(async () => {
    try {
      const sess = await getSession(chatId);
      if (sess?.state && sess.state.startsWith('bk_')) {
        await bot.sendMessage(
          chatId,
          esc('⏰ Вы начали бронирование, но не завершили.\n\nПродолжить? Ваши данные сохранены.'),
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [{ text: '✅ Продолжить', callback_data: 'bk_resume' }],
                [{ text: '❌ Отмена', callback_data: 'bk_cancel_session' }],
              ],
            },
          }
        );
      } else if (sess?.state && isActiveInputState(sess.state)) {
        // Non-booking active state — generic reminder
        await bot.sendMessage(chatId, 'Вы ещё здесь\\? Хотите продолжить?', {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '▶️ Продолжить', callback_data: 'resume_session' }],
              [{ text: '❌ Отменить', callback_data: 'cancel_session' }],
            ],
          },
        });
      }
    } catch {
      /* bot may be offline or session already cleared */
    }
    delete sessionReminderTimers[chatId];
  }, SESSION_REMINDER_MS);
  if (t?.unref) t.unref();
  sessionReminderTimers[chatId] = t;
}

// ─── Booking progress helper ──────────────────────────────────────────────────
function bookingProgress(step, total = 4) {
  const filled = '▓'.repeat(step);
  const empty = '░'.repeat(total - step);
  return `${filled}${empty} Шаг ${step}/${total}`;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// ─── UTM link helper ──────────────────────────────────────────────────────────
function siteUrl(path, utmParams = {}) {
  const base = SITE_URL.replace(/\/$/, '') + path;
  const params = new URLSearchParams({
    utm_source: 'telegram',
    utm_medium: 'bot',
    ...utmParams,
  });
  return `${base}?${params.toString()}`;
}

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, '\\$&');
}

function isAdmin(chatId) {
  return ADMIN_IDS.includes(String(chatId));
}

async function getAdminChatIds() {
  try {
    const rows = await query("SELECT telegram_id FROM admins WHERE telegram_id IS NOT NULL AND telegram_id != ''");
    return [...new Set([...ADMIN_IDS, ...rows.map(r => r.telegram_id)])];
  } catch {
    return [...ADMIN_IDS];
  }
}

async function safeSend(chatId, text, opts = {}) {
  // Telegram hard limit — truncate gracefully
  if (text && text.length > MAX_MESSAGE_LENGTH) text = text.slice(0, MAX_MESSAGE_LENGTH - 3) + '…';
  try {
    return await bot.sendMessage(chatId, text, opts);
  } catch (e) {
    if (opts.parse_mode && /parse entities|can't parse/i.test(e.message)) {
      try {
        return await bot.sendMessage(chatId, text, { ...opts, parse_mode: undefined });
      } catch {}
    }
    console.warn(`[Bot] send→${chatId}: ${e.message}`);
    return null;
  }
}

async function safePhoto(chatId, photo, opts = {}) {
  // Telegram caption limit
  if (opts.caption && opts.caption.length > MAX_CAPTION_LENGTH) {
    opts = { ...opts, caption: opts.caption.slice(0, MAX_CAPTION_LENGTH - 3) + '…' };
  }
  try {
    return await bot.sendPhoto(chatId, photo, opts);
  } catch {
    return safeSend(chatId, opts.caption || '📷', { parse_mode: opts.parse_mode });
  }
}

// ─── Audit log ────────────────────────────────────────────────────────────────

async function logAdminAction(adminChatId, action, entityType = null, entityId = null, details = null) {
  await run(`INSERT INTO audit_log (admin_chat_id, action, entity_type, entity_id, details) VALUES (?,?,?,?,?)`, [
    adminChatId,
    action,
    entityType,
    entityId,
    details ? JSON.stringify(details) : null,
  ]).catch(() => {});
}

// ─── Session ──────────────────────────────────────────────────────────────────

// ─── In-memory session cache (write-through to SQLite) ───────────────────────
// Устраняет зависания когда SQLite занята агентами: чтение всегда из памяти,
// запись сначала в память, затем асинхронно в SQLite.
const _sessionCache = new Map(); // chatId → { state, data, updated_at }
// Evict idle sessions from memory cache every hour to prevent unbounded growth
// Sessions in 'idle' state with no recent activity are safe to evict (will be re-read from SQLite)
setInterval(
  () => {
    const cutoff = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(); // 2 hours ago
    for (const [key, sess] of _sessionCache) {
      if ((!sess.state || sess.state === 'idle') && sess.updated_at < cutoff) {
        _sessionCache.delete(key);
      }
    }
  },
  60 * 60 * 1000
).unref();

async function getSession(chatId) {
  const key = String(chatId);
  if (_sessionCache.has(key)) return _sessionCache.get(key);
  try {
    const row = await get('SELECT * FROM telegram_sessions WHERE chat_id=?', [key]);
    if (row) _sessionCache.set(key, row);
    return row || null;
  } catch {
    return null;
  }
}

async function setSession(chatId, state, data = {}) {
  const key = String(chatId);
  const rec = { chat_id: key, state, data: JSON.stringify(data), updated_at: new Date().toISOString() };
  _sessionCache.set(key, rec);
  // Persist to SQLite in background — bot doesn't wait for it
  run(`INSERT OR REPLACE INTO telegram_sessions (chat_id,state,data,updated_at) VALUES (?,?,?,CURRENT_TIMESTAMP)`, [
    key,
    state,
    JSON.stringify(data),
  ]).catch(e => console.error('[Bot] setSession persist:', e.message));
}

async function clearSession(chatId) {
  await setSession(chatId, 'idle', {});
}

function sessionData(session) {
  try {
    const d = session?.data;
    return typeof d === 'string' ? JSON.parse(d || '{}') : d || {};
  } catch {
    return {};
  }
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
  } catch {
    return cached?.value ?? null;
  }
}

async function setSetting(key, value) {
  _settingsCache.set(key, { value, expiresAt: Date.now() + SETTINGS_TTL });
  await run('INSERT OR REPLACE INTO bot_settings (key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)', [key, value]);
}

// ─── Admin Handlers module ────────────────────────────────────────────────────
const _adminHandlers = require('./handlers/admin');
_adminHandlers.init({ safeSend, isAdmin, esc });
const showAdminStats = _adminHandlers.showAdminStats;
const showAdminModels = _adminHandlers.showAdminModels;
const showAdminOrders = _adminHandlers.showAdminOrders;
const showAdminOrdersToday = _adminHandlers.showAdminOrdersToday;
const showAdminReviewsPanel = _adminHandlers.showAdminReviews;

// ─── Keyboards ────────────────────────────────────────────────────────────────

// Persistent ReplyKeyboard — всегда показывается внизу чата вместо клавиатуры
const REPLY_KB_CLIENT = {
  keyboard: [
    [{ text: '⭐ Топ-модели' }, { text: '💃 Каталог' }],
    [{ text: '📝 Подать заявку' }, { text: '⚡ Быстрая заявка' }],
    [{ text: '❤️ Избранное' }, { text: '💬 Менеджер' }],
    [{ text: '📋 Мои заявки' }, { text: '🔍 Статус заявки' }, { text: '👤 Профиль' }],
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
  const [
    tgChannel,
    calcEnabled,
    wishlistEnabled,
    quickBookingEnabled,
    searchEnabled,
    reviewsEnabled,
    loyaltyEnabled,
    referralEnabled,
    faqEnabled,
  ] = await Promise.all([
    getSetting('tg_channel').catch(() => null),
    getSetting('calc_enabled').catch(() => null),
    getSetting('wishlist_enabled', '1').catch(() => '1'),
    getSetting('quick_booking_enabled', '1').catch(() => '1'),
    getSetting('search_enabled', '1').catch(() => '1'),
    getSetting('reviews_enabled', '1').catch(() => '1'),
    getSetting('loyalty_enabled').catch(() => '1'),
    getSetting('referral_enabled').catch(() => '1'),
    getSetting('faq_enabled').catch(() => '1'),
  ]);

  // Row 1: always
  const rows = [
    [
      { text: '💃 Каталог', callback_data: 'cat_cat__0' },
      { text: '⭐ Топ-модели', callback_data: 'cat_top_0' },
    ],
  ];

  // Row 2: booking (quick booking is gated)
  const bookingRow = [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }];
  if (quickBookingEnabled !== '0') bookingRow.push({ text: '⚡ Быстрая заявка', callback_data: 'bk_quick' });
  rows.push(bookingRow);

  // Tech spec generator
  rows.push([{ text: '📋 Тех. задание', callback_data: 'techspec_start' }]);

  // Row 3: orders + profile
  rows.push([
    { text: '📋 Мои заявки', callback_data: 'my_orders' },
    { text: '👤 Мой профиль', callback_data: 'profile' },
  ]);

  // Row 4: wishlist + calculator (wishlist gated)
  const favRow = [];
  if (wishlistEnabled !== '0') favRow.push({ text: '❤️ Избранное', callback_data: 'fav_list_0' });
  if (calcEnabled === '1') favRow.push({ text: '🧮 Калькулятор', callback_data: 'calculator' });
  if (favRow.length) rows.push(favRow);

  // Row 5: reviews + FAQ (both gated)
  const reviewRow = [];
  if (reviewsEnabled !== '0') reviewRow.push({ text: '⭐ Отзывы', callback_data: 'show_reviews' });
  if (faqEnabled !== '0') reviewRow.push({ text: '❓ FAQ', callback_data: 'faq' });
  if (reviewRow.length) rows.push(reviewRow);

  // Row 6: loyalty + referral (both gated)
  const loyaltyRow = [];
  if (referralEnabled !== '0') loyaltyRow.push({ text: '🎁 Реферальная программа', callback_data: 'referral' });
  if (loyaltyEnabled !== '0') loyaltyRow.push({ text: '💫 Баллы лояльности', callback_data: 'loyalty' });
  if (loyaltyRow.length) rows.push(loyaltyRow);

  // Row 7: category filters
  rows.push([
    { text: '👗 Fashion', callback_data: 'cat_filter_fashion' },
    { text: '📷 Commercial', callback_data: 'cat_filter_commercial' },
  ]);

  // Row 8: search (gated)
  if (searchEnabled !== '0') {
    rows.push([
      { text: '🔍 Поиск по параметрам', callback_data: 'cat_search' },
      { text: '📏 Поиск по росту', callback_data: 'search_height_input' },
    ]);
    rows.push([{ text: '🤖 AI подбор', callback_data: 'ai_match' }]);
  }

  // Row 9: pricing + manager
  rows.push([
    { text: '💰 Прайс-лист', callback_data: 'pricing' },
    { text: '💬 Написать менеджеру', callback_data: 'contact_mgr' },
  ]);

  // Row 10: about + contacts
  rows.push([
    { text: 'ℹ️ О нас', callback_data: 'about_us' },
    { text: '📞 Контакты', callback_data: 'contacts' },
  ]);

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
      [
        { text: `📋 Заявки${badge}`, callback_data: 'adm_orders__0' },
        { text: '💃 Модели', callback_data: 'adm_models_0' },
      ],
      [
        { text: '📊 Статистика', callback_data: 'adm_stats' },
        { text: '📈 Дашборд', callback_data: 'adm_dashboard' },
        { text: '⚡ Кратко', callback_data: 'adm_quick_stats' },
      ],
      [
        { text: `🤖 Организм${health}`, callback_data: 'adm_organism' },
        { text: '⚙️ Настройки', callback_data: 'adm_settings' },
      ],
      [
        { text: '📢 Рассылка', callback_data: 'adm_broadcast' },
        { text: '📅 Рассылки', callback_data: 'adm_sched_bcast' },
        { text: '📤 Экспорт заявок', callback_data: 'adm_export' },
      ],
      [
        { text: '➕ Добавить модель', callback_data: 'adm_addmodel' },
        { text: '👑 Администраторы', callback_data: 'adm_admins' },
      ],
      [
        { text: '📡 Фид агентов', callback_data: 'agent_feed_0' },
        { text: '⭐ Отзывы', callback_data: 'adm_reviews' },
        { text: '💬 Обсуждения', callback_data: 'adm_discussions' },
      ],
      [
        { text: '🔍 Найти заявку', callback_data: 'adm_search_order' },
        { text: '🔍 Заметки', callback_data: 'adm_search_notes' },
        { text: '🏭 AI Factory', callback_data: 'adm_factory' },
      ],
      [
        { text: '💡 Growth Actions', callback_data: 'adm_factory_growth' },
        { text: '🎯 AI Задачи', callback_data: 'adm_factory_tasks' },
      ],
      [
        { text: '👥 Клиенты', callback_data: 'adm_clients' },
        { text: '📋 Журнал', callback_data: 'adm_audit_log' },
        { text: '👥 Менеджеры', callback_data: 'adm_managers' },
      ],
      ...(SITE_URL.startsWith('https://')
        ? [
            [
              { text: '📱 Mini App', web_app: { url: SITE_URL.replace(/\/$/, '') + '/webapp.html' } },
              { text: '🌐 Сайт', url: siteUrl('/', { utm_campaign: 'admin_menu' }) },
            ],
          ]
        : []),
    ],
  };
};

// ─── Client screens ───────────────────────────────────────────────────────────

async function showMainMenu(chatId, name) {
  await clearSession(chatId);
  const [greeting, menuText, welcomePhoto, clientKb] = await Promise.all([
    getSetting('greeting').catch(() => null),
    getSetting('main_menu_text').catch(() => null),
    getSetting('welcome_photo_url').catch(() => null),
    buildClientKeyboard(),
  ]);
  // Сначала показываем persistent ReplyKeyboard
  await safeSend(chatId, `💎 Nevesty Models — меню активировано`, { reply_markup: REPLY_KB_CLIENT });
  let greetingText;
  if (greeting) {
    greetingText = esc(greeting.replace('{name}', name || 'гость'));
  } else {
    const menuLabel = menuText || 'Выберите действие:';
    greetingText = `💎 *Nevesty Models*\n\nДобро пожаловать${name ? ', ' + esc(name) : ''}\\!\n\n_Агентство профессиональных моделей — Fashion, Commercial, Events_\n\n${esc(menuLabel)}`;
  }
  // If welcome photo is configured and looks like a URL, send as photo with caption
  if (welcomePhoto && typeof welcomePhoto === 'string' && welcomePhoto.startsWith('http')) {
    try {
      return await bot.sendPhoto(chatId, welcomePhoto, {
        caption: greetingText,
        parse_mode: 'MarkdownV2',
        reply_markup: clientKb,
      });
    } catch (e) {
      console.warn('[Bot] welcome_photo_url failed, falling back to text:', e.message);
    }
  }
  return safeSend(chatId, greetingText, { parse_mode: 'MarkdownV2', reply_markup: clientKb });
}

// ─── Admin: Client Management ─────────────────────────────────────────────────

async function showAdminClients(chatId, page = 0) {
  if (!isAdmin(chatId)) return;
  const LIMIT = 8;
  const clients = await query(
    `
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
    LIMIT ? OFFSET ?`,
    [LIMIT, page * LIMIT]
  );

  const total =
    (
      await get(
        `SELECT COUNT(DISTINCT client_chat_id) as cnt FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND CAST(client_chat_id AS INTEGER) > 0`
      )
    )?.cnt || 0;

  if (!clients.length) return safeSend(chatId, '👥 Клиентов пока нет\\.', { parse_mode: 'MarkdownV2' });

  const keyboard = clients.map(c => [
    {
      text: `${c.name || 'Без имени'} (${c.total_orders} зак.)`,
      callback_data: `adm_client_${c.chat_id}`,
    },
  ]);

  // Pagination
  const nav = [];
  if (page > 0) nav.push({ text: '← Назад', callback_data: `adm_clients_${page - 1}` });
  if ((page + 1) * LIMIT < total) nav.push({ text: 'Вперёд →', callback_data: `adm_clients_${page + 1}` });
  if (nav.length) keyboard.push(nav);
  keyboard.push([{ text: '🔙 Admin панель', callback_data: 'adm_panel' }]);

  await safeSend(chatId, `👥 *Клиенты* \\(${total} всего\\)\nСтраница ${page + 1}`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard },
  });
}

async function showAdminClientCard(chatId, clientId) {
  if (!isAdmin(chatId)) return;

  const orders = await query(`SELECT * FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 10`, [
    String(clientId),
  ]);
  if (!orders.length) return safeSend(chatId, '❌ Клиент не найден или нет заявок\\.', { parse_mode: 'MarkdownV2' });

  const client = orders[0];
  const stats = await get(
    `SELECT
    COUNT(*) as total,
    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
    SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled
  FROM orders WHERE client_chat_id=?`,
    [String(clientId)]
  );

  const isBlocked = !!(await get(`SELECT chat_id FROM blocked_clients WHERE chat_id=?`, [clientId]).catch(() => null));
  const loyalty = await get(`SELECT points, total_earned, level FROM loyalty_points WHERE chat_id=?`, [clientId]).catch(
    () => null
  );

  const recentOrders = orders
    .slice(0, 5)
    .map(o => `• #${esc(o.order_number || String(o.id))} — ${esc(o.status)}`)
    .join('\n');

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
  ]
    .filter(Boolean)
    .join('\n');

  await safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '✉️ Написать клиенту', callback_data: `adm_msg_client_${clientId}` }],
        [
          {
            text: isBlocked ? '✅ Разблокировать' : '⛔ Заблокировать',
            callback_data: isBlocked ? `adm_unblock_${clientId}` : `adm_block_${clientId}`,
          },
        ],
        [{ text: '← Список клиентов', callback_data: 'adm_clients' }],
      ],
    },
  });
}

async function showAdminMenu(chatId, name) {
  if (!isAdmin(chatId)) return;
  await clearSession(chatId);
  try {
    const [ordersRow, scoreRow] = await Promise.all([
      get("SELECT COUNT(*) as n FROM orders WHERE status='new'").catch(() => ({ n: 0 })),
      get("SELECT message FROM agent_logs WHERE from_name='Orchestrator' ORDER BY created_at DESC LIMIT 1").catch(
        () => null
      ),
    ]);
    const badge = ordersRow.n > 0 ? ` 🔴${ordersRow.n}` : '';
    const scoreMatch = scoreRow?.message?.match(/Health Score:\s*(\d+)%/);
    const score = scoreMatch ? parseInt(scoreMatch[1]) : null;
    // Сначала показываем persistent ReplyKeyboard для быстрого доступа
    await safeSend(chatId, `👑 Панель администратора — меню активировано`, { reply_markup: REPLY_KB_ADMIN });
    return safeSend(
      chatId,
      `👑 *Панель администратора*${name ? `\n_${esc(name)}_` : ''}\n\nЗаявок в очереди: *${ordersRow.n}*`,
      { parse_mode: 'MarkdownV2', reply_markup: KB_MAIN_ADMIN(badge, score) }
    );
  } catch (e) {
    console.error('[Bot] showAdminMenu:', e.message);
  }
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
      page = cat;
      cat = filter.category || '';
    }
    if (!filter) filter = {};
    if (!cat) cat = filter.category || '';
    page = page || 0;

    // Per-user sort preference + global catalog_sort setting
    const sortPref = catalogSortPrefs.get(String(chatId)) || 'featured';
    const globalSort = await getSetting('catalog_sort').catch(() => 'featured');
    const effectiveSort = sortPref && sortPref !== 'featured' ? sortPref : globalSort || 'featured';
    // Support both old ('alpha'/'date') and new ('name'/'newest') sort value names
    const orderClause =
      effectiveSort === 'alpha' || effectiveSort === 'name'
        ? 'ORDER BY name ASC'
        : effectiveSort === 'date' || effectiveSort === 'newest'
          ? 'ORDER BY id DESC'
          : 'ORDER BY featured DESC, name ASC';

    // Build WHERE clause
    const conditions = ['available=1', 'COALESCE(archived,0)=0'];
    const params = [];
    if (cat) {
      conditions.push('category=?');
      params.push(cat);
    }
    if (filter.city) {
      conditions.push('city=?');
      params.push(filter.city);
    }
    const where = conditions.join(' AND ');
    const models = await query(`SELECT * FROM models WHERE ${where} ${orderClause}`, params);

    if (!models.length) {
      return safeSend(chatId, '📭 Моделей по выбранному фильтру нет\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'main_menu' }]] },
      });
    }

    const _rawPerPage = parseInt(await getSetting('catalog_per_page').catch(() => '5')) || 5;
    const perPage = Math.min(20, Math.max(1, _rawPerPage));
    const total = models.length;
    const slice = models.slice(page * perPage, page * perPage + perPage);

    // Category filter buttons (fashion / commercial / events)
    const catFilterRow = [
      { text: (cat === 'fashion' ? '✅ ' : '') + '💄 Фэшн', callback_data: 'cat_filter_fashion' },
      { text: (cat === 'commercial' ? '✅ ' : '') + '📸 Коммерческая', callback_data: 'cat_filter_commercial' },
      { text: (cat === 'events' ? '✅ ' : '') + '🎉 Мероприятия', callback_data: 'cat_filter_events' },
    ];

    // Sort row
    const sortRow = [
      { text: (sortPref === 'featured' ? '✅ ' : '') + '⭐ Топ', callback_data: 'cat_sort_featured' },
      {
        text: (sortPref === 'newest' || sortPref === 'date' ? '✅ ' : '') + '🆕 Нові',
        callback_data: 'cat_sort_newest',
      },
      { text: (sortPref === 'alpha' || sortPref === 'name' ? '✅ ' : '') + '🔤 А-Я', callback_data: 'cat_sort_alpha' },
    ];

    // Dynamic city buttons from settings, fallback to DB distinct cities
    const citiesSetting = await getSetting('cities_list').catch(() => '');
    let cityList = citiesSetting
      ? citiesSetting
          .split(',')
          .map(c => c.trim())
          .filter(Boolean)
          .slice(0, 8)
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
      if (cityList[i + 1])
        row.push({
          text: '🏙 ' + cityList[i + 1],
          callback_data: 'cat_city_' + encodeURIComponent(cityList[i + 1]) + '_0',
        });
      cityRows.push(row);
    }

    // Load city and badge display settings in parallel
    const [showCitySettingRaw, showBadgeSettingRaw] = await Promise.all([
      getSetting('catalog_show_city').catch(() => null),
      getSetting('catalog_show_featured_badge').catch(() => null),
    ]);
    // Show city: setting-driven (default on if multiple cities in slice)
    const citySet = new Set(slice.map(m => m.city).filter(Boolean));
    const showCityInCard =
      showCitySettingRaw === '1' || showCitySettingRaw === 'true'
        ? true
        : showCitySettingRaw === '0' || showCitySettingRaw === 'false'
          ? false
          : citySet.size > 1;
    // Show featured badge: setting-driven (default on)
    const showFeaturedBadge = showBadgeSettingRaw !== '0' && showBadgeSettingRaw !== 'false';

    // Category short labels for inline display
    const catShortLabels = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };

    // Model buttons: numbered, featured-first indicator, key stats
    const modelBtns = slice.map((m, i) => {
      const num = page * perPage + i + 1;
      const featStar = showFeaturedBadge && m.featured ? '⭐' : '·';
      const catShort = catShortLabels[m.category] || m.category || '';
      const cityPart = showCityInCard && m.city ? ` | ${m.city}` : '';
      const agePart = m.age ? ` | ${m.age} л` : '';
      const heightPart = m.height ? ` | ${m.height} см` : '';
      return [
        {
          text: `[${num}] ${featStar} ${m.name}${heightPart}${agePart}${catShort ? ` | ${catShort}` : ''}${cityPart}`,
          callback_data: `cat_model_${m.id}`,
        },
      ];
    });

    // Pagination
    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `cat_cat_${cat}_${page - 1}` });
    if ((page + 1) * perPage < total) nav.push({ text: '▶️', callback_data: `cat_cat_${cat}_${page + 1}` });

    const keyboard = [
      catFilterRow,
      sortRow,
      ...cityRows,
      ...modelBtns,
      ...(nav.length ? [nav] : []),
      [
        { text: '🔍 Поиск', callback_data: 'cat_search' },
        { text: '📝 Оформить заявку', callback_data: 'bk_start' },
      ],
      [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
    ];

    const label = CATEGORIES[cat] || 'Все';
    const cityLabel = filter.city ? ` — 🏙 ${esc(filter.city)}` : '';
    const pageInfo = Math.ceil(total / perPage) > 1 ? ` \\(стр\\. ${page + 1}/${Math.ceil(total / perPage)}\\)` : '';
    const featuredCount = models.filter(mo => mo.featured).length;
    const featuredNote = featuredCount > 0 ? `\n⭐ — топ\\-модели` : '';
    const catalogBreadcrumb = `_🏠 Главная › 💃 Каталог_\n\n`;
    return safeSend(
      chatId,
      `${catalogBreadcrumb}💃 *Каталог моделей — ${esc(label)}${cityLabel}*${pageInfo}\n\nНайдено: *${total}* ${ru_plural(total, 'модель', 'модели', 'моделей')}${featuredNote}\n\nВыберите модель:`,
      { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } }
    );
  } catch (e) {
    console.error('[Bot] showCatalog:', e.message);
  }
}

// Alias: showCatalogFiltered — filter by category (БЛОК 2.7)
async function _showCatalogFiltered(chatId, page, category) {
  return showCatalog(chatId, category || '', page || 0, { category: category || '' });
}

async function showModel(chatId, modelId, backBtn = null) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m)
      return safeSend(chatId, '❌ Модель не найдена\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] },
      });

    // Increment view counter (fire-and-forget)
    run('UPDATE models SET view_count = COALESCE(view_count,0) + 1 WHERE id=?', [modelId]).catch(() => {});

    // Reviews: count + average rating
    const reviewRow = await get(
      'SELECT AVG(rating) as avg, COUNT(*) as cnt FROM reviews WHERE model_id=? AND approved=1',
      [modelId]
    ).catch(() => null);
    const reviewCount = reviewRow ? reviewRow.cnt || 0 : 0;
    const reviewAvg = reviewRow ? reviewRow.avg || 0 : 0;

    // Completed orders count
    const orderCountRow = await get('SELECT COUNT(*) as n FROM orders WHERE model_id=? AND status="completed"', [
      m.id,
    ]).catch(() => ({ n: 0 }));
    const completedOrders = orderCountRow ? orderCountRow.n || 0 : 0;

    // Upcoming busy dates (next 3)
    const upcomingBusy = await query(
      `SELECT busy_date FROM model_busy_dates WHERE model_id=? AND busy_date >= date('now') ORDER BY busy_date LIMIT 10`,
      [m.id]
    ).catch(() => []);
    const busyRanges = groupBusyDatesIntoRanges(upcomingBusy);

    // Category banner
    const categoryBanners = {
      fashion: '👗 Fashion Model',
      commercial: '📸 Commercial Model',
      events: '🎭 Event Model',
    };
    const catBanner = categoryBanners[m.category] || '💃 Model';

    // Rating stars (filled up to rounded avg, out of 5)
    function ratingStars(avg) {
      const filled = Math.round(avg);
      return '⭐'.repeat(Math.min(5, Math.max(0, filled))) + '☆'.repeat(Math.max(0, 5 - filled));
    }

    const lines = [];
    if (m.featured) lines.push(`⭐ Топ\\-модель`);
    if (m.age) lines.push(`📅 Возраст: *${m.age}* лет`);
    if (m.height) lines.push(`📏 Рост: *${m.height}* см`);
    if (m.weight) lines.push(`⚖️ Вес: *${m.weight}* кг`);
    if (m.bust && m.waist && m.hips) lines.push(`📐 Параметры: *${m.bust}/${m.waist}/${m.hips}*`);
    if (m.shoe_size) lines.push(`👟 Обувь: *${esc(m.shoe_size)}*`);
    if (m.hair_color) lines.push(`💇 Волосы: *${esc(m.hair_color)}*`);
    if (m.eye_color) lines.push(`👁 Глаза: *${esc(m.eye_color)}*`);
    if (m.city) lines.push(`🏙 Город: *${esc(m.city)}*`);
    if (m.instagram) lines.push(`📸 @${esc(m.instagram)}`);
    // Enhanced stats
    if (reviewCount > 0)
      lines.push(`${ratingStars(reviewAvg)} *${reviewCount}* ${ru_plural(reviewCount, 'отзыв', 'отзыва', 'отзывов')}`);
    if (completedOrders > 0) lines.push(`📋 *${esc(String(completedOrders))}* заявок`);
    const viewCount = (m.view_count || 0) + 1; // +1 for the just-incremented count
    if (viewCount > 50) lines.push(`🔥 Популярная`);
    if (viewCount > 0) lines.push(`👁 *${viewCount}* просмотров`);

    let avail;
    if (!m.available) {
      avail = '🔴 Временно недоступна';
    } else if (busyRanges.length > 0) {
      const rangeStrs = busyRanges
        .slice(0, 3)
        .map(r =>
          r.start === r.end ? formatDateShort(r.start) : `${formatDateShort(r.start)}–${formatDateShort(r.end)}`
        );
      avail = `⚠️ Занята: ${rangeStrs.join(', ')}`;
    } else {
      avail = '✅ Доступна для бронирования';
    }
    const star = m.featured ? '⭐ ' : '';
    // Caption must fit Telegram's media caption limit
    const bioEsc = m.bio ? esc(m.bio) : '';
    const bioFits = bioEsc.slice(0, 180) + (bioEsc.length > 180 ? '…' : '');
    const breadcrumb = `_🏠 Главная › 💃 Каталог › ${esc(m.name)}_`;
    const captionParts = [breadcrumb, `*${esc(catBanner)}*`, `${star}*${esc(m.name)}*`, '', ...lines, '', avail];
    if (bioFits) captionParts.push('', `_${bioFits}_`);
    const caption = captionParts.join('\n').slice(0, MAX_CAPTION_LENGTH - 4);

    const contactBtn =
      m.phone || m.instagram ? [{ text: '📞 Написать менеджеру', callback_data: `model_contact_${m.id}` }] : [];
    const profileUrl = siteUrl(`/model/${m.id}`, { utm_campaign: 'model_card', utm_content: String(m.id) });
    const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(siteUrl('/model/' + m.id, { utm_campaign: 'share' }))}&text=${encodeURIComponent('Посмотри эту модель: ' + m.name)}`;

    // Check wishlist status and wishlist_enabled setting in parallel
    const [wishlistEnabled, inWishlist] = await Promise.all([
      getSetting('wishlist_enabled').catch(() => '1'),
      isInWishlist(chatId, m.id).catch(() => false),
    ]);
    const favText = inWishlist ? '💔 Убрать из избранного' : '❤️ В избранное';
    const favCb = inWishlist ? `fav_remove_${m.id}` : `fav_add_${m.id}`;

    // Improved keyboard layout
    const keyboardRows = [];
    // Row 1: Book (large, prominent)
    if (m.available) keyboardRows.push([{ text: '📋 Забронировать', callback_data: `bk_model_${m.id}` }]);
    // Row 2: Fav + Reviews
    const row2 = [];
    if (wishlistEnabled !== '0') row2.push({ text: favText, callback_data: favCb });
    row2.push({ text: '⭐ Отзывы', callback_data: `reviews_model_${m.id}` });
    if (row2.length) keyboardRows.push(row2);
    // Row 3: All photos + Back (catalog or search results)
    keyboardRows.push([
      { text: '🌐 Профиль на сайте', url: profileUrl },
      backBtn || { text: '← Назад в каталог', callback_data: 'cat_cat__0' },
    ]);
    // Row 4: Contact manager (if available)
    if (contactBtn.length) keyboardRows.push(contactBtn);
    // Row 5: Compare + Share
    keyboardRows.push([
      { text: '⚖️ Сравнить', callback_data: `compare_add_${m.id}` },
      { text: '📤 Поделиться', url: shareUrl },
    ]);
    // Row 6: Availability + Main menu
    keyboardRows.push([
      {
        text: m.available ? '📅 Уточнить доступность' : '📞 Узнать о доступности',
        callback_data: `ask_availability_${m.id}`,
      },
      { text: '🏠 Меню', callback_data: 'main_menu' },
    ]);

    const keyboard = { inline_keyboard: keyboardRows };

    // Собираем все фото: photo_main + галерея
    let galleryUrls = [];
    try {
      galleryUrls = JSON.parse(m.photos || '[]');
    } catch {}
    if (m.photo_main && !galleryUrls.includes(m.photo_main)) {
      galleryUrls.unshift(m.photo_main);
    }

    if (galleryUrls.length >= 2) {
      // Медиагруппа — caption только на первом фото
      const totalPhotos = galleryUrls.length;
      const media = galleryUrls.slice(0, 8).map((url, i) => {
        const item = { type: 'photo', media: url };
        if (i === 0) {
          // Add photo count to caption header
          const galCaption = caption.slice(0, MAX_CAPTION_LENGTH - 74) + `\n\n📸 Фото: 1 из ${totalPhotos}`;
          item.caption = galCaption.slice(0, MAX_CAPTION_LENGTH - 4);
          item.parse_mode = 'MarkdownV2';
        }
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
      return safeSend(chatId, `📸 *${esc(m.name)}* — фото: ${totalPhotos} шт\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: keyboard,
      });
    }

    if (m.photo_main) {
      await safePhoto(chatId, m.photo_main, { caption, parse_mode: 'MarkdownV2', reply_markup: keyboard });
      if (bioEsc.length > 180) {
        await safeSend(chatId, `📝 *Описание:*\n\n_${bioEsc}_`, { parse_mode: 'MarkdownV2' });
      }
      return;
    }
    // Нет фото — полная карточка текстом (лимит 4096)
    const fullCaption = [
      `*${esc(catBanner)}*`,
      `${star}*${esc(m.name)}*`,
      '',
      ...lines,
      '',
      avail,
      ...(bioEsc ? ['', `📝 *Описание:*\n_${bioEsc}_`] : []),
    ].join('\n');
    return safeSend(chatId, fullCaption, { parse_mode: 'MarkdownV2', reply_markup: keyboard });
  } catch (e) {
    console.error('[Bot] showModel:', e.message);
  }
}

function ru_plural(n, one, few, many) {
  const m10 = n % 10,
    m100 = n % 100;
  if (m100 >= 11 && m100 <= 19) return many;
  if (m10 === 1) return one;
  if (m10 >= 2 && m10 <= 4) return few;
  return many;
}

// ── Calendar / availability helpers ───────────────────────────────────────────
const MONTHS_RU = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
function formatDateShort(dateStr) {
  // dateStr is YYYY-MM-DD
  const [, m, d] = dateStr.split('-');
  return `${parseInt(d)} ${MONTHS_RU[parseInt(m) - 1]}`;
}

/** Group consecutive busy_date rows into ranges for compact display */
function groupBusyDatesIntoRanges(rows) {
  // rows: [{busy_date: 'YYYY-MM-DD', reason: '...'}] sorted ascending
  if (!rows.length) return [];
  const ranges = [];
  let start = rows[0].busy_date;
  let end = rows[0].busy_date;
  let reason = rows[0].reason || '';
  for (let i = 1; i < rows.length; i++) {
    const prev = new Date(end);
    const cur = new Date(rows[i].busy_date);
    prev.setDate(prev.getDate() + 1);
    const sameReason = (rows[i].reason || '') === reason;
    if (cur.toISOString().slice(0, 10) === prev.toISOString().slice(0, 10) && sameReason) {
      end = rows[i].busy_date;
    } else {
      ranges.push({ start, end, reason });
      start = rows[i].busy_date;
      end = rows[i].busy_date;
      reason = rows[i].reason || '';
    }
  }
  ranges.push({ start, end, reason });
  return ranges;
}

// ── My orders ─────────────────────────────────────────────────────────────────

async function showMyOrders(chatId, page = 0) {
  try {
    page = parseInt(page) || 0;
    const PER_PAGE = 5;
    const totalRow = await get('SELECT COUNT(*) as n FROM orders WHERE client_chat_id=?', [String(chatId)]).catch(
      () => ({ n: 0 })
    );
    const total = totalRow.n;

    if (!total) {
      return safeSend(
        chatId,
        '_🏠 Главная › 📋 Мои заявки_\n\n📭 *Ваши заявки*\n\nУ вас пока нет заявок\\. Оформите первую прямо сейчас\\!',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }],
              [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
            ],
          },
        }
      );
    }

    const orders = await query(
      `SELECT o.*,m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE o.client_chat_id=? ORDER BY o.created_at DESC LIMIT ? OFFSET ?`,
      [String(chatId), PER_PAGE, page * PER_PAGE]
    );

    let text = `_🏠 Главная › 📋 Мои заявки_\n\n📋 *Ваши заявки* \\(${total}\\):\n\n`;
    const btns = [];
    for (const o of orders) {
      text += `${STATUS_LABELS[o.status] || o.status} *${esc(o.order_number)}*\n`;
      text += `${esc(EVENT_TYPES[o.event_type] || o.event_type)}`;
      if (o.event_date) text += ` · ${esc(o.event_date)}`;
      text += '\n\n';
      const row = [
        { text: `${o.order_number}  ${STATUS_LABELS[o.status] || o.status}`, callback_data: `client_order_${o.id}` },
      ];
      if (o.status === 'completed' || o.status === 'cancelled') {
        row.push({ text: '🔁', callback_data: `repeat_order_${o.id}` });
      }
      btns.push(row);
    }

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `my_orders_page_${page - 1}` });
    if ((page + 1) * PER_PAGE < total) nav.push({ text: '▶️', callback_data: `my_orders_page_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...btns,
          ...(nav.length ? [nav] : []),
          [{ text: '📝 Новая заявка', callback_data: 'bk_start' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showMyOrders:', e.message);
  }
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
        reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] },
      });
    }
    const msgs = await query('SELECT * FROM messages WHERE order_id=? ORDER BY created_at DESC LIMIT 3', [orderId]);
    const timeline = await showOrderTimeline(o);
    let text = `_🏠 Главная › 📋 Мои заявки › Заявка \\#${esc(o.order_number)}_\n\n`;
    text += `📋 *Заявка ${esc(o.order_number)}*\n\n`;
    text += `*Статус заявки:*\n${timeline}\n\n`;
    text += `Мероприятие: *${esc(EVENT_TYPES[o.event_type] || o.event_type)}*\n`;
    if (o.event_date) text += `Дата: ${esc(o.event_date)}\n`;
    if (o.event_duration) text += `Продолжительность: ${o.event_duration} ч\\.\n`;
    if (o.location) text += `Место: ${esc(o.location)}\n`;
    // Show all models if multi-model booking
    if (o.model_ids) {
      try {
        const ids = JSON.parse(o.model_ids);
        if (Array.isArray(ids) && ids.length > 1) {
          const modelRows = await query(
            `SELECT id, name FROM models WHERE id IN (${ids.map(() => '?').join(',')})`,
            ids
          );
          const nameMap = Object.fromEntries(modelRows.map(r => [r.id, r.name]));
          text += `Модели \\(${ids.length}\\):\n`;
          ids.forEach((id, i) => {
            text += `  ${i + 1}\\. ${esc(nameMap[id] || String(id))}\n`;
          });
        } else if (o.model_name) {
          text += `Модель: ${esc(o.model_name)}\n`;
        }
      } catch {
        if (o.model_name) text += `Модель: ${esc(o.model_name)}\n`;
      }
    } else if (o.model_name) {
      text += `Модель: ${esc(o.model_name)}\n`;
    }
    if (o.budget) text += `Бюджет: ${esc(o.budget)}\n`;
    if (msgs.length) {
      text += `\n💬 *Последние сообщения:*\n`;
      msgs.reverse().forEach(m => {
        const who = m.sender_type === 'admin' ? '👤 Менеджер' : '🙋 Вы';
        text += `${who}: ${esc(m.content)}\n`;
      });
    }
    const repeatBtn =
      o.status === 'completed' || o.status === 'cancelled'
        ? [{ text: '🔁 Повторить заявку', callback_data: `repeat_order_${o.id}` }]
        : [];
    const reviewBtn =
      o.status === 'completed' ? [{ text: '⭐ Оставить отзыв', callback_data: `leave_review_${o.id}` }] : [];
    // Payment info in message
    if (o.payment_status === 'paid') {
      text += `\n💳 *Оплата:* ✅ Оплачено\n`;
    } else if (o.payment_id && o.payment_status === 'pending') {
      text += `\n💳 *Оплата:* ⏳ Ожидает оплаты\n`;
    }
    // Show Pay button for confirmed orders that are not yet paid
    const payBtn =
      o.status === 'confirmed' && o.payment_status !== 'paid'
        ? [{ text: '💳 Оплатить', callback_data: `pay_order_${o.id}` }]
        : [];

    const kb = [
      [{ text: '← Мои заявки', callback_data: 'my_orders' }],
      [{ text: '🏠 Меню', callback_data: 'main_menu' }],
    ];
    if (payBtn.length) kb.unshift(payBtn);
    if (repeatBtn.length) kb.unshift(repeatBtn);
    if (reviewBtn.length) kb.unshift(reviewBtn);

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: kb },
    });
  } catch (e) {
    console.error('[Bot] showClientOrder:', e.message);
  }
}

// ── Status check ──────────────────────────────────────────────────────────────

async function showStatusInput(chatId) {
  await setSession(chatId, 'check_status', {});
  return safeSend(
    chatId,
    '🔍 *Проверка статуса заявки*\n\nВведите номер вашей заявки \\(например: *NM\\-2025\\-ABCDEF*\\):',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'main_menu' }]] },
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
      return safeSend(chatId, `❌ Заявка *${esc(orderNum)}* не найдена\\. Проверьте номер\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '🔄 Ввести другой номер', callback_data: 'check_status' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
          ],
        },
      });
    }
    let text = `📋 *Заявка ${esc(o.order_number)}*\n\n`;
    text += `Статус: *${STATUS_LABELS[o.status] || o.status}*\n`;
    text += `Мероприятие: ${esc(EVENT_TYPES[o.event_type] || o.event_type)}\n`;
    if (o.event_date) text += `Дата: ${esc(o.event_date)}\n`;
    if (o.model_name) text += `Модель: ${esc(o.model_name)}\n`;
    if (o.admin_notes) text += `\n📝 Примечание: ${esc(o.admin_notes)}\n`;
    await clearSession(chatId);
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '🔄 Проверить другой', callback_data: 'check_status' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showOrderStatus:', e.message);
  }
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
    addr ? `Адрес: ${esc(addr)}` : null,
    `Сайт: ${esc(SITE_URL)}`,
    ``,
    `Пн\\-Вс: 09:00 — 21:00`,
  ]
    .filter(l => l !== null)
    .join('\n');
  return safeSend(chatId, lines, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
  });
}

// ─── Booking wizard — 4 steps (mirrors website exactly) ──────────────────────
//
// Step 1: Choose model (optional)
// Step 2: Event details — type → date → duration → location → budget → comments
// Step 3: Client info  — name → phone → email → telegram
// Step 4: Confirm & submit

function stepHeader(step, title) {
  const dots = ['●', '●', '●', '●'].map((d, i) => (i < step ? '●' : '○')).join(' ');
  return `📝 *Бронирование · Шаг ${step}/4*\n${dots}\n\n*${title}*\n\n`;
}

// STEP 1 — model selection (пропускается если модель уже выбрана)
async function bkStep1(chatId, data = {}) {
  // Если модель уже выбрана (например через кнопку «Заказать эту модель») — пропускаем
  if (data.model_id && data.model_name) {
    await safeSend(chatId, `✅ Модель выбрана: *${esc(data.model_name)}*`, { parse_mode: 'MarkdownV2' });
    return bkStep2EventType(chatId, data);
  }

  await setSession(chatId, 'bk_s1', data);
  resetSessionTimer(chatId);
  try {
    const models = await query(
      'SELECT id,name,height,hair_color FROM models WHERE available=1 AND COALESCE(archived,0)=0 ORDER BY id LIMIT 12'
    );
    const btns = models.map(m => [
      {
        text: `${m.name}  ·  ${m.height}см  ·  ${m.hair_color || ''}`,
        callback_data: `bk_pick_${m.id}`,
      },
    ]);
    return safeSend(
      chatId,
      `_🏠 Главная › 📝 Бронирование_\n\n` +
        stepHeader(1, 'Выберите модель') +
        'Выберите из списка или нажмите «Менеджер подберёт»:',
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            ...btns,
            [{ text: '✨ Менеджер подберёт', callback_data: 'bk_pick_any' }],
            [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
          ],
        },
      }
    );
  } catch (e) {
    console.error('[Bot] bkStep1:', e.message);
  }
}

// STEP 2a — event type
async function bkStep2EventType(chatId, data) {
  await setSession(chatId, 'bk_s2_event', data);
  resetSessionTimer(chatId);
  const btns = Object.entries(EVENT_TYPES).map(([k, v]) => [{ text: v, callback_data: `bk_etype_${k}` }]);
  return safeSend(chatId, stepHeader(2, 'Детали мероприятия') + 'Выберите тип мероприятия:', {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        ...btns,
        [{ text: '← Выбрать модель', callback_data: 'bk_start' }],
        [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
      ],
    },
  });
}

// STEP 2b — date
async function bkStep2Date(chatId, data) {
  await setSession(chatId, 'bk_s2_date', data);
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    stepHeader(2, 'Детали мероприятия') +
      `✅ Тип: *${esc(EVENT_TYPES[data.event_type] || data.event_type)}*\n\nВведите дату мероприятия:\n💡 Формат: ДД\\.ММ\\.ГГГГ, например: 25\\.12\\.2025`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] },
    }
  );
}

// STEP 2c — duration
async function bkStep2Duration(chatId, data) {
  // Auto-skip if duration pre-filled (e.g. from calculator)
  if (data.event_duration && DURATIONS.includes(String(data.event_duration))) {
    await safeSend(chatId, `✅ Длительность: *${esc(String(data.event_duration))} ч\\.* \\(из калькулятора\\)`, {
      parse_mode: 'MarkdownV2',
    });
    return bkStep2Location(chatId, data);
  }
  await setSession(chatId, 'bk_s2_dur', data);
  resetSessionTimer(chatId);
  const row1 = DURATIONS.slice(0, 4).map(h => ({ text: `${h} ч.`, callback_data: `bk_dur_${h}` }));
  const row2 = DURATIONS.slice(4).map(h => ({ text: `${h} ч.`, callback_data: `bk_dur_${h}` }));
  return safeSend(chatId, stepHeader(2, 'Детали мероприятия') + 'Выберите продолжительность мероприятия:', {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        row1,
        row2,
        [{ text: '← Назад', callback_data: 'bk_back_event_type' }],
        [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
      ],
    },
  });
}

// STEP 2d — location
async function bkStep2Location(chatId, data) {
  await setSession(chatId, 'bk_s2_loc', data);
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    stepHeader(2, 'Детали мероприятия') +
      'Введите место проведения \\(город, адрес\\):\n_Пример: Москва, ул\\. Арбат 15_\n\n_/cancel — отменить_',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '← Назад', callback_data: 'bk_back_duration' }],
          [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
        ],
      },
    }
  );
}

// STEP 2e — budget (optional)
async function bkStep2Budget(chatId, data) {
  await setSession(chatId, 'bk_s2_budget', data);
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    stepHeader(2, 'Детали мероприятия') +
      'Укажите бюджет \\(необязательно\\):\n💡 Укажите бюджет в рублях, например: 150000',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '← Назад', callback_data: 'bk_back_location' }],
          [{ text: '⏭ Пропустить', callback_data: 'bk_skip_budget' }],
          [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
        ],
      },
    }
  );
}

// STEP 2f — comments (optional)
async function bkStep2Comments(chatId, data) {
  await setSession(chatId, 'bk_s2_comments', data);
  resetSessionTimer(chatId);
  return safeSend(chatId, stepHeader(2, 'Детали мероприятия') + 'Дополнительные пожелания \\(необязательно\\):', {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '← Назад', callback_data: 'bk_back_budget' }],
        [{ text: '⏭ Пропустить', callback_data: 'bk_skip_comments' }],
        [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
      ],
    },
  });
}

// STEP 3a — name
async function bkStep3Name(chatId, data) {
  await setSession(chatId, 'bk_s3_name', data);
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    stepHeader(3, 'Ваши контакты') + `_${esc(bookingProgress(1, 4))}_\n\n${STRINGS.bookingAskName}`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] },
    }
  );
}

// STEP 3b — phone
async function bkStep3Phone(chatId, data) {
  await setSession(chatId, 'bk_s3_phone', data);
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    stepHeader(3, 'Ваши контакты') + `_${esc(bookingProgress(2, 4))}_\n\n${STRINGS.bookingAskPhone}`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '← Назад', callback_data: 'bk_back_to_name' }],
          [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
        ],
      },
    }
  );
}

// STEP 3c — email (optional unless booking_require_email='1')
async function bkStep3Email(chatId, data) {
  await setSession(chatId, 'bk_s3_email', data);
  resetSessionTimer(chatId);
  const requireEmail = await getSetting('booking_require_email').catch(() => '0');
  const buttons = [[{ text: '← Назад', callback_data: 'bk_back_to_phone' }]];
  if (requireEmail !== '1') {
    buttons.splice(0, 0, [{ text: '⏭ Пропустить', callback_data: 'bk_skip_email' }]);
  }
  buttons.push([{ text: '❌ Отменить', callback_data: 'bk_cancel' }]);
  const hint = requireEmail === '1' ? '\n_Email обязателен для подтверждения заявки\\._' : '';
  return safeSend(
    chatId,
    stepHeader(3, 'Ваши контакты') + `_${esc(bookingProgress(3, 4))}_\n\n${STRINGS.bookingAskEmail}${hint}`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: buttons },
    }
  );
}

// STEP 3d — telegram username (optional)
async function bkStep3Telegram(chatId, data, tgUsername) {
  await setSession(chatId, 'bk_s3_tg', data);
  resetSessionTimer(chatId);
  const hint = tgUsername ? `_Ваш username в Telegram: @${esc(tgUsername)}_\n\n` : '';
  return safeSend(
    chatId,
    stepHeader(3, 'Ваши контакты') +
      `_${esc(bookingProgress(4, 4))}_\n\n` +
      hint +
      'Введите Telegram username для связи \\(необязательно\\):\n_Пример: @username_',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          tgUsername ? [{ text: `✅ Использовать @${tgUsername}`, callback_data: `bk_use_tg_${tgUsername}` }] : [],
          [{ text: '← Назад', callback_data: 'bk_back_to_email' }],
          [{ text: '⏭ Пропустить', callback_data: 'bk_skip_tg' }],
          [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
        ].filter(r => r.length),
      },
    }
  );
}

// STEP 4 — confirmation summary (mirrors website's step 4)
async function bkStep4Confirm(chatId, data) {
  await setSession(chatId, 'bk_s4', data);
  let text = stepHeader(4, 'Подтвердите заявку');

  // Show all selected models if multi-model booking
  const modelIds = Array.isArray(data.model_ids) ? data.model_ids : [];
  if (modelIds.length > 1) {
    // Fetch names for any model_ids that don't have cached names yet
    const modelNames = Array.isArray(data.model_names) ? data.model_names : [];
    if (modelNames.length === modelIds.length) {
      text += `💃 Модели \\(${modelIds.length}\\):\n`;
      modelNames.forEach((name, i) => {
        text += `  ${i + 1}\\. ${esc(name)}\n`;
      });
    } else {
      text += `💃 Модели: *${esc(modelIds.join(', '))}* \\(ID\\)\n`;
    }
  } else {
    text += `💃 Модель: *${data.model_name ? esc(data.model_name) : 'Менеджер подберёт'}*\n`;
  }

  text += `🎭 Мероприятие: *${esc(EVENT_TYPES[data.event_type] || data.event_type)}*\n`;
  if (data.event_date) text += `📅 Дата: ${esc(data.event_date)}\n`;
  text += `⏱ Продолжительность: ${data.event_duration || 4} ч\\.\n`;
  if (data.location) text += `📍 Место: ${esc(data.location)}\n`;
  if (data.budget) text += `💰 Бюджет: ${esc(data.budget)}\n`;
  if (data.comments) text += `💬 Пожелания: ${esc(data.comments)}\n`;
  text += `\n👤 Имя: *${esc(data.client_name)}*\n`;
  text += `📞 Телефон: *${esc(data.client_phone)}*\n`;
  if (data.client_email) text += `📧 Email: ${esc(data.client_email)}\n`;
  if (data.client_telegram) text += `💬 Telegram: @${esc(data.client_telegram)}\n`;
  text += '\nВсё верно?';
  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '✅ Отправить заявку', callback_data: 'bk_submit' }],
        [{ text: '➕ Добавить модель', callback_data: 'bk_add_model' }],
        [{ text: '← Изменить', callback_data: 'bk_start' }],
        [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
      ],
    },
  });
}

async function bkSubmit(chatId, data) {
  try {
    // Check active orders limit
    const maxActive = parseInt(await getSetting('client_max_active_orders').catch(() => '10')) || 10;
    const activeCountRow = await get(
      "SELECT COUNT(*) as cnt FROM orders WHERE client_chat_id=? AND status NOT IN ('completed','cancelled')",
      [String(chatId)]
    ).catch(() => ({ cnt: 0 }));
    if ((activeCountRow?.cnt || 0) >= maxActive) {
      return safeSend(
        chatId,
        '⚠️ *Превышен лимит активных заявок*\\.\nПожалуйста, дождитесь завершения текущих заявок\\.',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
              [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
            ],
          },
        }
      );
    }

    const orderNum = generateOrderNumber();
    // Serialize model_ids if multiple models selected
    const modelIdsJson =
      Array.isArray(data.model_ids) && data.model_ids.length > 1 ? JSON.stringify(data.model_ids) : null;
    await run(
      `INSERT INTO orders
        (order_number,client_name,client_phone,client_email,client_telegram,
         client_chat_id,model_id,model_ids,event_type,event_date,event_duration,
         location,budget,comments,status)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')`,
      [
        orderNum,
        data.client_name,
        data.client_phone,
        data.client_email || null,
        data.client_telegram || null,
        String(chatId),
        data.model_id || null,
        modelIdsJson,
        data.event_type,
        data.event_date || null,
        parseInt(data.event_duration) || 4,
        data.location || null,
        data.budget || null,
        data.comments || null,
      ]
    );
    const order = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]);

    // Post-insert race condition check: verify active order count wasn't exceeded
    // by a concurrent submission that slipped through the pre-check
    const activeAfterInsert = await get(
      "SELECT COUNT(*) as n FROM orders WHERE client_chat_id=? AND status NOT IN ('completed','cancelled')",
      [String(chatId)]
    ).catch(() => ({ n: 0 }));
    if (maxActive > 0 && (activeAfterInsert?.n || 0) > maxActive) {
      // Race condition detected — remove the just-inserted order
      await run('DELETE FROM orders WHERE order_number=?', [orderNum]).catch(() => {});
      await clearSession(chatId);
      return safeSend(
        chatId,
        '❌ У вас уже слишком много активных заявок\\. Пожалуйста, дождитесь завершения текущих\\.',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] },
        }
      );
    }

    await clearSession(chatId);

    // Auto-confirm if setting enabled
    const autoConfirm = await getSetting('booking_auto_confirm').catch(() => '0');
    if (autoConfirm === '1' && order) {
      await run("UPDATE orders SET status='confirmed' WHERE id=?", [order.id]).catch(() => {});
      order.status = 'confirmed';
      notifyStatusChange(chatId, orderNum, 'confirmed').catch(() => {});
      // Always notify manager about auto-confirmed orders regardless of notif_new_order
      notifyAdmin(
        `✅ *Автоподтверждение заявки*\n\n📋 *${esc(orderNum)}*\n👤 ${esc(order.client_name)}\n📞 ${esc(order.client_phone)}`,
        { parse_mode: 'MarkdownV2' }
      ).catch(() => {});
    }

    // Grant "precise_choice" achievement if booking has a specific date set from the start
    if (data.event_date) {
      await grantAchievement(chatId, 'precise_choice').catch(() => {});
    }

    const customConfirmMsg = autoConfirm === '1' ? await getSetting('booking_confirm_msg').catch(() => null) : null;
    const confirmMsg = customConfirmMsg
      ? esc(customConfirmMsg)
      : autoConfirm === '1'
        ? `🎉 *Заявка подтверждена\\!*\n\nНомер: *${esc(orderNum)}*\n\nВаша заявка автоматически подтверждена\\. Менеджер свяжется с вами для уточнения деталей\\.`
        : `🎉 *Заявка принята\\!*\n\nНомер: *${esc(orderNum)}*\n\nМенеджер свяжется с вами в течение 1 часа для подтверждения\\.\n\nСохраните номер — по нему можно проверить статус в любое время\\.`;
    await safeSend(chatId, confirmMsg, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
    });
    if (order) {
      notifyNewOrder(order);
      // Email notifications (non-blocking)
      if (mailer) {
        if (order.client_email) {
          mailer
            .sendOrderConfirmation(order.client_email, order)
            .catch(e => console.error('[mailer] bot order confirm:', e.message));
        }
        mailer.getAdminEmails().forEach(adminEmail => {
          mailer.sendManagerNotification(adminEmail, order).catch(() => {});
        });
      }
      // CRM webhooks (non-blocking)
      try {
        const { notifyCRM } = require('./services/crm');
        notifyCRM('order.created', order, getSetting).catch(e => console.error('[CRM] bot:', e.message));
      } catch {}
    }
  } catch (e) {
    console.error('[Bot] bkSubmit:', e.message);
    await clearSession(chatId);
    return safeSend(chatId, '❌ *Не удалось создать заявку\\.* Попробуйте позже или напишите менеджеру\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '💬 Написать менеджеру', callback_data: 'contact_mgr' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
    });
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
    let text = `${breadcrumb}\n\n📋 *${esc(o.order_number)}*\nСтатус: ${esc(STATUS_LABELS[o.status] || o.status)}\n`;
    if (o.manager_name) text += `👤 Менеджер: *${esc(o.manager_name)}*\n`;
    text += `\n`;
    text += `👤 ${esc(o.client_name)}\n📞 ${esc(o.client_phone)}\n`;
    if (o.client_email) text += `📧 ${esc(o.client_email)}\n`;
    if (o.client_telegram) text += `💬 @${esc(o.client_telegram.replace('@', ''))}\n`;
    text += `\n🎭 ${esc(EVENT_TYPES[o.event_type] || o.event_type)}\n`;
    if (o.event_date) text += `📅 ${esc(o.event_date)}\n`;
    if (o.event_duration) text += `⏱ ${esc(o.event_duration)} ч\\.\n`;
    if (o.location) text += `📍 ${esc(o.location)}\n`;
    // Show all models if multi-model booking
    if (o.model_ids) {
      try {
        const ids = JSON.parse(o.model_ids);
        if (Array.isArray(ids) && ids.length > 1) {
          const modelRows = await query(
            `SELECT id, name FROM models WHERE id IN (${ids.map(() => '?').join(',')})`,
            ids
          );
          const nameMap = Object.fromEntries(modelRows.map(r => [r.id, r.name]));
          text += `💃 Модели \\(${ids.length}\\):\n`;
          ids.forEach((id, i) => {
            text += `  ${i + 1}\\. ${esc(nameMap[id] || String(id))}\n`;
          });
        } else if (o.model_name) {
          text += `💃 ${esc(o.model_name)}\n`;
        }
      } catch {
        if (o.model_name) text += `💃 ${esc(o.model_name)}\n`;
      }
    } else if (o.model_name) {
      text += `💃 ${esc(o.model_name)}\n`;
    }
    if (o.budget) text += `💰 ${esc(o.budget)}\n`;
    if (o.comments) text += `💬 ${esc(o.comments)}\n`;
    if (msgs.length) {
      text += `\n📨 Последние сообщения:\n`;
      msgs.reverse().forEach(m => {
        const who = m.sender_type === 'admin' ? '👤' : '🙋';
        text += `${who} ${esc(m.content)}\n`;
      });
    }
    if (notes.length) {
      text += `\n📝 Заметки:\n`;
      [...notes].reverse().forEach(n => {
        const dt = n.created_at
          ? new Date(n.created_at).toLocaleString('ru', {
              timeZone: 'Europe/Moscow',
              day: '2-digit',
              month: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
            })
          : '';
        text += `_${esc(dt)}_ ${esc(n.admin_note)}\n`;
      });
    }
    if (o.internal_note) {
      text += `\n📝 *Заметка:* ${esc(o.internal_note)}`;
    }

    const actions = [];
    if (!['confirmed', 'completed', 'cancelled'].includes(o.status))
      actions.push({ text: '✅ Подтвердить', callback_data: `adm_confirm_${orderId}` });
    if (!['reviewing', 'completed', 'cancelled'].includes(o.status))
      actions.push({ text: '🔍 В работу', callback_data: `adm_review_${orderId}` });
    if (!['cancelled', 'completed'].includes(o.status))
      actions.push({ text: '❌ Отклонить', callback_data: `adm_reject_${orderId}` });

    const keyboard = [];
    if (actions.length) keyboard.push(actions);
    keyboard.push([
      { text: '💬 Написать клиенту', callback_data: `adm_contact_${orderId}` },
      { text: '🏁 Завершить', callback_data: `adm_complete_${orderId}` },
    ]);
    keyboard.push([
      { text: '👤 Назначить менеджера', callback_data: `adm_assign_mgr_${orderId}` },
      { text: '📝 Добавить заметку', callback_data: `adm_note_${orderId}` },
    ]);
    keyboard.push([{ text: '💃 Назначить модель', callback_data: `adm_assign_model_${orderId}` }]);
    keyboard.push([
      { text: '📋 Все заметки', callback_data: `adm_notes_${orderId}_0` },
      { text: '🕐 История статусов', callback_data: `adm_order_history_${orderId}` },
    ]);
    keyboard.push([{ text: '📝 Заметка', callback_data: `adm_order_note_${orderId}` }]);
    // Quick replies button — shown when order has a client chat ID
    if (o.client_chat_id) {
      keyboard.push([{ text: '⚡ Быстрые ответы', callback_data: `adm_qr_${o.client_chat_id}` }]);
    }
    // Invoice button — shown for confirmed orders with a client chat ID
    if (o.status === 'confirmed' && o.client_chat_id) {
      const invoiceLabel = o.invoice_sent_at ? '💳 Счёт выставлен повторно' : '💳 Выставить счёт';
      keyboard.push([{ text: invoiceLabel, callback_data: `adm_invoice_${orderId}` }]);
    }
    // Paid badge row for paid orders
    if (o.paid_at) {
      const paidDt = new Date(o.paid_at).toLocaleString('ru', {
        timeZone: 'Europe/Moscow',
        day: '2-digit',
        month: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      });
      text += `\n💰 *Оплачено:* ${esc(paidDt)}`;
    }
    keyboard.push([{ text: '← К заявкам', callback_data: 'adm_orders__0' }]);

    return safeSend(chatId, text, { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } });
  } catch (e) {
    console.error('[Bot] showAdminOrder:', e.message);
  }
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
      reply_markup: {
        inline_keyboard: [
          [{ text: '← К заявке', callback_data: `adm_order_${orderId}` }],
          [{ text: '← К заявкам', callback_data: 'adm_orders__0' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showOrderStatusHistory:', e.message);
  }
}

// [MOVED TO handlers/admin.js]
// async function showAdminStats(chatId) { ... }

async function showOrganismStatus(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [lastRun, critCount, highCount, okCount] = await Promise.all([
      get(
        "SELECT message, created_at FROM agent_logs WHERE from_name='Orchestrator' ORDER BY created_at DESC LIMIT 1"
      ).catch(() => null),
      get(
        "SELECT COUNT(*) as n FROM agent_logs WHERE message LIKE '%🔴%' AND created_at > datetime('now','-1 hour')"
      ).catch(() => ({ n: 0 })),
      get(
        "SELECT COUNT(*) as n FROM agent_logs WHERE message LIKE '%🟠%' AND created_at > datetime('now','-1 hour')"
      ).catch(() => ({ n: 0 })),
      get(
        "SELECT COUNT(*) as n FROM agent_logs WHERE message LIKE '%✅%' AND created_at > datetime('now','-1 hour')"
      ).catch(() => ({ n: 0 })),
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
      reply_markup: {
        inline_keyboard: [
          [{ text: '🚀 Запустить проверку', callback_data: 'adm_run_organism' }],
          [{ text: '🔧 Исправить всё и перепроверить', callback_data: 'adm_fix_organism' }],
          [{ text: '📡 Фид агентов', callback_data: 'agent_feed_0' }],
          [{ text: '← Панель', callback_data: 'admin_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showOrganismStatus:', e.message);
  }
}

// [MOVED TO handlers/admin.js]
// async function showAdminModels(chatId, page, opts = {}) { ... }

async function showAdminModel(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена.');

    // Full order stats
    const [stats] = await query(
      `
      SELECT
        COUNT(*) as total_orders,
        SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
        SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) as cancelled,
        AVG(CASE WHEN status='completed' THEN 1.0 ELSE NULL END) * 100 as success_rate
      FROM orders WHERE model_id=?
    `,
      [modelId]
    );

    const total = stats?.total_orders || 0;
    const completed = stats?.completed || 0;
    const cancelled = stats?.cancelled || 0;
    const successRate = Math.round(stats?.success_rate || 0);
    const viewCount = m.view_count || 0;

    let text = `💃 *${esc(m.name)}*\n\n`;
    if (m.age) text += `🎂 Возраст: ${m.age} лет\n`;
    if (m.height) text += `📏 Рост: ${m.height} см\n`;
    if (m.weight) text += `⚖️ Вес: ${m.weight} кг\n`;
    if (m.bust) text += `📐 Параметры: ${m.bust}/${m.waist}/${m.hips}\n`;
    if (m.shoe_size) text += `👟 Обувь: ${esc(m.shoe_size)}\n`;
    if (m.hair_color) text += `💇 Волосы: ${esc(m.hair_color)}\n`;
    if (m.eye_color) text += `👁 Глаза: ${esc(m.eye_color)}\n`;
    if (m.instagram) text += `📸 @${esc(m.instagram)}\n`;
    text += `🏷 Категория: ${esc(MODEL_CATEGORIES[m.category] || m.category)}\n`;
    text += `Статус: ${m.available ? '🟢 Доступна' : '🔴 Недоступна'}\n`;
    text += `\n📊 Заказов: ${total} \\| ✅ Завершено: ${completed} \\| ❌ Отменено: ${cancelled}\n`;
    text += `📈 Успешность: ${successRate}%  👁 Просмотров: ${viewCount}\n`;
    if (m.bio) text += `\n_${esc(m.bio)}_`;

    const archiveBtn = m.archived
      ? { text: '📤 Восстановить', callback_data: `adm_restore_${m.id}` }
      : { text: '📦 В архив', callback_data: `adm_archive_${m.id}` };

    const keyboard = {
      inline_keyboard: [
        [
          { text: '✏️ Редактировать', callback_data: `adm_editmodel_${m.id}` },
          { text: m.available ? '🔴 Недоступна' : '🟢 Доступна', callback_data: `adm_toggle_${m.id}` },
        ],
        [
          { text: '📋 Дублировать', callback_data: `adm_duplicate_${m.id}` },
          { text: '⭐ ' + (m.featured ? 'Убрать из топа' : 'В топ'), callback_data: `adm_featured_${m.id}` },
        ],
        [
          { text: '📊 Статистика модели', callback_data: `adm_model_stats_${m.id}` },
          { text: '📅 Расписание', callback_data: `adm_model_cal_${m.id}` },
        ],
        [archiveBtn],
        [{ text: '← К моделям', callback_data: 'adm_models_p_0_name_0' }],
      ],
    };

    // Галерея: photo_main + photos[]
    let galleryUrls = [];
    try {
      galleryUrls = JSON.parse(m.photos || '[]');
    } catch {}
    if (m.photo_main && !galleryUrls.includes(m.photo_main)) galleryUrls.unshift(m.photo_main);

    if (galleryUrls.length >= 2) {
      const media = galleryUrls.slice(0, 8).map((url, i, arr) => {
        const item = { type: 'photo', media: url };
        if (i === arr.length - 1) {
          item.caption = text;
          item.parse_mode = 'MarkdownV2';
        }
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
  } catch (e) {
    console.error('[Bot] showAdminModel:', e.message);
  }
}

async function showAgentFeed(chatId, page) {
  try {
    const total = (await get('SELECT COUNT(*) as n FROM agent_logs')).n;
    if (!total)
      return safeSend(chatId, '🤖 Фид агентов пуст.', {
        reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] },
      });
    const logs = await query('SELECT * FROM agent_logs ORDER BY created_at DESC LIMIT 10 OFFSET ?', [page * 10]);
    let text = `🤖 *Фид агентов* \\(${total}\\)\n\n`;
    logs.reverse().forEach(l => {
      const ts = new Date(l.created_at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      const msg = (l.message || '').length > 100 ? l.message.slice(0, 100) + '…' : l.message;
      text += `\\[${esc(ts)}\\] *${esc(l.from_name || 'Claude')}*\n${esc(msg)}\n\n`;
    });
    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `agent_feed_${page - 1}` });
    if ((page + 1) * 10 < total) nav.push({ text: '▶️', callback_data: `agent_feed_${page + 1}` });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [...(nav.length ? [nav] : []), [{ text: '← Меню', callback_data: 'admin_menu' }]],
      },
    });
  } catch (e) {
    console.error('[Bot] showAgentFeed:', e.message);
  }
}

async function showAgentDiscussions(chatId, period = '24h', page = 0) {
  try {
    const periodMap = { '1h': '-1 hours', '24h': '-24 hours', '7d': '-7 days', '30d': '-30 days' };
    const since = periodMap[period] || '-24 hours';
    const PAGE_SIZE = 8;

    const [totalRow, rows] = await Promise.all([
      get(`SELECT COUNT(*) as n FROM agent_discussions WHERE created_at > datetime('now', ?)`, [since]),
      query(
        `SELECT * FROM agent_discussions WHERE created_at > datetime('now', ?) ORDER BY created_at DESC LIMIT ? OFFSET ?`,
        [since, PAGE_SIZE, page * PAGE_SIZE]
      ),
    ]);
    const total = totalRow?.n || 0;

    if (!rows.length)
      return safeSend(chatId, `💬 Обсуждений за ${period} нет — агенты ещё не запускались.`, {
        reply_markup: {
          inline_keyboard: [
            [
              { text: '📡 Фид агентов', callback_data: 'agent_feed_0' },
              { text: '← Меню', callback_data: 'admin_menu' },
            ],
          ],
        },
      });

    const now = Date.now();
    let text = `💬 *Обсуждения агентов* \\(${period}, ${total} записей\\)\n\n`;
    rows.forEach(d => {
      const mins = Math.round((now - new Date(d.created_at).getTime()) / 60000);
      const timeStr = mins < 60 ? `${mins}м` : `${Math.round(mins / 60)}ч`;
      const snippet = esc((d.message || '').slice(0, 120));
      text += `*${esc(d.from_agent || '?')}* \\(${esc(timeStr)}\\):\n_${snippet}_\n\n`;
    });

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `adm_disc_${period}_${page - 1}` });
    if ((page + 1) * PAGE_SIZE < total) nav.push({ text: '▶️', callback_data: `adm_disc_${period}_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [
            { text: period === '1h' ? '✓1ч' : '1ч', callback_data: 'adm_disc_1h_0' },
            { text: period === '24h' ? '✓24ч' : '24ч', callback_data: 'adm_disc_24h_0' },
            { text: period === '7d' ? '✓7д' : '7д', callback_data: 'adm_disc_7d_0' },
            { text: period === '30d' ? '✓30д' : '30д', callback_data: 'adm_disc_30d_0' },
          ],
          ...(nav.length ? [nav] : []),
          [
            { text: '🔄 Обновить', callback_data: `adm_disc_${period}_${page}` },
            { text: '← Меню', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showAgentDiscussions:', e.message);
  }
}

// ─── Settings menu ────────────────────────────────────────────────────────────

async function showAdminSettings(chatId, section) {
  if (!isAdmin(chatId)) return;
  section = section || 'main';

  // ── Главное меню настроек ──────────────────────────────────────────────────
  if (section === 'main') {
    const [notifNew, notifSt, revEnabled, quickEnabled] = await Promise.all([
      getSetting('notif_new_order'),
      getSetting('notif_status'),
      getSetting('reviews_enabled'),
      getSetting('quick_booking_enabled'),
    ]);
    const text =
      `⚙️ Настройки бота и агентства\n\n` +
      `🔔 Уведомления: ${notifNew === '1' ? '✅' : '❌'} заявки  ${notifSt === '1' ? '✅' : '❌'} статусы\n` +
      `⭐ Отзывы: ${revEnabled === '0' ? '❌ Выкл' : '✅ Вкл'}  ⚡ Быстрая заявка: ${quickEnabled === '0' ? '❌ Выкл' : '✅ Вкл'}`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '💬 Контакты', callback_data: 'adm_settings_contacts' },
            { text: '🔔 Уведомления', callback_data: 'adm_settings_notifs' },
          ],
          [
            { text: '📋 Каталог', callback_data: 'adm_settings_catalog' },
            { text: '🛒 Бронирование', callback_data: 'adm_settings_booking' },
          ],
          [
            { text: '⭐ Отзывы', callback_data: 'adm_settings_reviews' },
            { text: '🤖 Интерфейс', callback_data: 'adm_settings_ui' },
          ],
          [
            { text: '📊 Лимиты', callback_data: 'adm_settings_limits' },
            { text: '📱 Соцсети', callback_data: 'adm_settings_social' },
          ],
          [{ text: '← Назад', callback_data: 'admin_menu' }],
        ],
      },
    });
  }

  // ── Контакты и тексты ──────────────────────────────────────────────────────
  if (section === 'contacts') {
    const [phone, email, insta, addr, greeting, about, mgrHours, mgrReply, wa] = await Promise.all([
      getSetting('contacts_phone'),
      getSetting('contacts_email'),
      getSetting('contacts_insta'),
      getSetting('contacts_addr'),
      getSetting('greeting'),
      getSetting('about'),
      getSetting('manager_hours'),
      getSetting('manager_reply'),
      getSetting('contacts_whatsapp'),
    ]);
    const trunc = (s, n = 40) => (s ? (s.length > n ? s.slice(0, n) + '…' : s) : '—');
    const text =
      `💬 Контакты и тексты\n\n` +
      `📞 Телефон: ${phone || '—'}\n` +
      `📧 Email: ${email || '—'}\n` +
      `📸 Instagram: ${insta || '—'}\n` +
      `📱 WhatsApp: ${wa || '—'}\n` +
      `📍 Адрес: ${trunc(addr)}\n` +
      `📝 Приветствие: ${trunc(greeting)}\n` +
      `ℹ️ О нас: ${trunc(about)}\n` +
      `🕐 Часы менеджера: ${mgrHours || '—'}\n` +
      `💬 Авто-ответ: ${trunc(mgrReply, 30)}`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '📞 Телефон', callback_data: 'adm_set_phone' },
            { text: '📧 Email', callback_data: 'adm_set_email' },
          ],
          [
            { text: '📸 Instagram', callback_data: 'adm_set_insta' },
            { text: '📱 WhatsApp', callback_data: 'adm_set_whatsapp' },
          ],
          [
            { text: '📍 Адрес', callback_data: 'adm_set_addr' },
            { text: '🌐 Сайт URL', callback_data: 'adm_set_site_url' },
          ],
          [
            { text: '📝 Приветствие', callback_data: 'adm_set_greeting' },
            { text: 'ℹ️ О нас', callback_data: 'adm_set_about' },
          ],
          [
            { text: '🕐 Часы работы', callback_data: 'adm_set_mgr_hours' },
            { text: '💬 Авто-ответ', callback_data: 'adm_set_mgr_reply' },
          ],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }

  // ── Уведомления ───────────────────────────────────────────────────────────
  if (section === 'notifs') {
    const [notifNew, notifSt, notifRev, notifMsg, notifSms, eventReminders] = await Promise.all([
      getSetting('notif_new_order'),
      getSetting('notif_status'),
      getSetting('notif_new_review'),
      getSetting('notif_new_message'),
      getSetting('sms_notifications_enabled'),
      getSetting('event_reminders_enabled'),
    ]);
    const on = v => (v === '1' ? '✅' : '❌');
    const text =
      `🔔 Уведомления\n\n` +
      `${on(notifNew)} Новые заявки\n` +
      `${on(notifSt)} Изменения статуса\n` +
      `${on(notifRev)} Новые отзывы\n` +
      `${on(notifMsg)} Сообщения клиентов\n` +
      `${on(notifSms)} SMS уведомления\n` +
      `${on(eventReminders ?? '1')} Напоминания о мероприятиях`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [
            {
              text: notifNew === '1' ? '🔕 Заявки ВЫКЛ' : '🔔 Заявки ВКЛ',
              callback_data: notifNew === '1' ? 'adm_notif_new_off' : 'adm_notif_new_on',
            },
          ],
          [
            {
              text: notifSt === '1' ? '🔕 Статусы ВЫКЛ' : '🔔 Статусы ВКЛ',
              callback_data: notifSt === '1' ? 'adm_notif_st_off' : 'adm_notif_st_on',
            },
          ],
          [
            {
              text: notifRev === '1' ? '🔕 Отзывы ВЫКЛ' : '🔔 Отзывы ВКЛ',
              callback_data: notifRev === '1' ? 'adm_notif_review_off' : 'adm_notif_review_on',
            },
          ],
          [
            {
              text: notifMsg === '1' ? '🔕 Сообщения ВЫКЛ' : '🔔 Сообщения ВКЛ',
              callback_data: notifMsg === '1' ? 'adm_notif_msg_off' : 'adm_notif_msg_on',
            },
          ],
          [
            {
              text: notifSms === '1' ? '🔕 SMS ВЫКЛ' : '📱 SMS ВКЛ',
              callback_data: notifSms === '1' ? 'adm_notif_sms_off' : 'adm_notif_sms_on',
            },
          ],
          [
            {
              text:
                (eventReminders ?? '1') === '1' ? '🔕 Напоминания о событиях ВЫКЛ' : '🔔 Напоминания о событиях ВКЛ',
              callback_data: 'adm_toggle_event_reminders',
            },
          ],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }

  // ── Каталог и модели ──────────────────────────────────────────────────────
  if (section === 'catalog') {
    const [perPage, sort, showCity, showBadge, catTitle] = await Promise.all([
      getSetting('catalog_per_page'),
      getSetting('catalog_sort'),
      getSetting('catalog_show_city'),
      getSetting('catalog_show_featured_badge'),
      getSetting('catalog_title'),
    ]);
    const text =
      `📋 Каталог и модели\n\n` +
      `📄 Моделей на странице: ${perPage || '5'}\n` +
      `🔃 Сортировка: ${sort === 'date' ? 'По дате' : 'По рейтингу'}\n` +
      `🏙 Показывать город: ${showCity === '0' ? '❌' : '✅'}\n` +
      `⭐ Бейдж «Топ»: ${showBadge === '0' ? '❌' : '✅'}\n` +
      `📌 Заголовок: ${catTitle || 'Наши модели'}`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '📄 Кол-во на странице', callback_data: 'adm_set_catalog_per_page' },
            { text: '📌 Заголовок', callback_data: 'adm_set_catalog_title' },
          ],
          [
            {
              text: sort === 'date' ? '🔃 Сорт: По рейтингу' : '🔃 Сорт: По дате',
              callback_data: sort === 'date' ? 'adm_catalog_sort_featured' : 'adm_catalog_sort_date',
            },
          ],
          [
            {
              text: showCity === '0' ? '🏙 Показать город' : '🏙 Скрыть город',
              callback_data: showCity === '0' ? 'adm_catalog_city_on' : 'adm_catalog_city_off',
            },
          ],
          [
            {
              text: showBadge === '0' ? '⭐ Показать бейдж' : '⭐ Скрыть бейдж',
              callback_data: showBadge === '0' ? 'adm_catalog_badge_on' : 'adm_catalog_badge_off',
            },
          ],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }

  // ── Бронирование ──────────────────────────────────────────────────────────
  if (section === 'booking') {
    const [quickEnabled, autoConfirm, minBudget, bookingMsg, requireEmail] = await Promise.all([
      getSetting('quick_booking_enabled'),
      getSetting('booking_auto_confirm'),
      getSetting('booking_min_budget'),
      getSetting('booking_confirm_msg'),
      getSetting('booking_require_email'),
    ]);
    const text =
      `🛒 Бронирование\n\n` +
      `⚡ Быстрая заявка: ${quickEnabled === '0' ? '❌ Выкл' : '✅ Вкл'}\n` +
      `✅ Авто-подтверждение: ${autoConfirm === '1' ? '✅ Вкл' : '❌ Выкл'}\n` +
      `💰 Мин. бюджет: ${minBudget || 'не задан'}\n` +
      `📧 Требовать email: ${requireEmail === '1' ? '✅' : '❌'}\n` +
      `💬 Сообщение после брони: ${(bookingMsg || '').slice(0, 40) || '—'}`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [
            {
              text: quickEnabled === '0' ? '⚡ Быстрая заявка ВКЛ' : '⚡ Быстрая заявка ВЫКЛ',
              callback_data: quickEnabled === '0' ? 'adm_booking_quick_on' : 'adm_booking_quick_off',
            },
          ],
          [
            {
              text: autoConfirm === '1' ? '✅ Авто-подтвержд. ВЫКЛ' : '✅ Авто-подтвержд. ВКЛ',
              callback_data: autoConfirm === '1' ? 'adm_booking_autoconfirm_off' : 'adm_booking_autoconfirm_on',
            },
          ],
          [
            {
              text: requireEmail === '1' ? '📧 Email необязателен' : '📧 Email обязателен',
              callback_data: requireEmail === '1' ? 'adm_booking_email_off' : 'adm_booking_email_on',
            },
          ],
          [
            { text: '💰 Мин. бюджет', callback_data: 'adm_set_booking_min_budget' },
            { text: '💬 Сообщение', callback_data: 'adm_set_booking_confirm_msg' },
          ],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }

  // ── Отзывы ────────────────────────────────────────────────────────────────
  if (section === 'reviews') {
    const [revEnabled, revAuto, revMin, revPrompt] = await Promise.all([
      getSetting('reviews_enabled'),
      getSetting('reviews_auto_approve'),
      getSetting('reviews_min_completed'),
      getSetting('reviews_prompt_text'),
    ]);
    const text =
      `⭐ Отзывы\n\n` +
      `💬 Включены: ${revEnabled === '0' ? '❌' : '✅'}\n` +
      `✅ Авто-одобрение: ${revAuto === '1' ? '✅' : '❌'}\n` +
      `📋 Мин. завершённых заявок: ${revMin || '1'}\n` +
      `📝 Приглашение: ${(revPrompt || '').slice(0, 40) || '—'}`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [
            {
              text: revEnabled === '0' ? '💬 Отзывы ВКЛ' : '💬 Отзывы ВЫКЛ',
              callback_data: revEnabled === '0' ? 'adm_reviews_on' : 'adm_reviews_off',
            },
          ],
          [
            {
              text: revAuto === '1' ? '✅ Авто-одобр. ВЫКЛ' : '✅ Авто-одобр. ВКЛ',
              callback_data: revAuto === '1' ? 'adm_reviews_auto_off' : 'adm_reviews_auto_on',
            },
          ],
          [
            { text: '🔢 Мин. заявок', callback_data: 'adm_set_reviews_min' },
            { text: '📝 Приглашение', callback_data: 'adm_set_reviews_prompt' },
          ],
          [{ text: '📋 Управление отзывами', callback_data: 'adm_reviews' }],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }

  // ── Города ────────────────────────────────────────────────────────────────
  if (section === 'cities') {
    const cities = await getSetting('cities_list').catch(() => '');
    const cityList = cities
      ? cities
          .split(',')
          .map(c => `• ${c.trim()}`)
          .join('\n')
      : 'Не задано — фильтр по городу скрыт';
    return safeSend(chatId, `🏙 Города\n\nДоступные города для фильтра:\n\n${cityList}`, {
      reply_markup: {
        inline_keyboard: [
          [{ text: '✏️ Изменить список городов', callback_data: 'adm_set_cities_list' }],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }

  // ── Бот и интерфейс ───────────────────────────────────────────────────────
  if (section === 'bot') {
    const [
      welcomePhoto,
      menuText,
      wishlistEnabled,
      searchEnabled,
      botLang,
      quickBooking,
      reviewsEnabled,
      loyaltyEnabled,
      referralEnabled,
      modelStatsEnabled,
      faqEnabled,
      calcEnabled,
      bookingThanks,
      tgChannel,
    ] = await Promise.all([
      getSetting('welcome_photo_url'),
      getSetting('main_menu_text'),
      getSetting('wishlist_enabled'),
      getSetting('search_enabled'),
      getSetting('bot_language'),
      getSetting('quick_booking_enabled'),
      getSetting('reviews_enabled'),
      getSetting('loyalty_enabled'),
      getSetting('referral_enabled'),
      getSetting('model_stats_enabled'),
      getSetting('faq_enabled'),
      getSetting('calc_enabled'),
      getSetting('booking_thanks_text'),
      getSetting('tg_channel'),
    ]);
    const onOff = v => (v === '0' ? '❌' : '✅');
    const trunc = (s, n = 35) => (s ? (s.length > n ? s.slice(0, n) + '…' : s) : '—');
    const text =
      `🤖 Бот и интерфейс\n\n` +
      `🌐 Язык: ${botLang || 'ru'}\n` +
      `🖼 Фото приветствия: ${welcomePhoto ? '✅ Задано' : '❌ Нет'}\n` +
      `📋 Текст главного меню: ${trunc(menuText)}\n` +
      `⚡ Быстрая заявка: ${onOff(quickBooking)}  ❤️ Wishlist: ${onOff(wishlistEnabled)}\n` +
      `🔍 Поиск: ${onOff(searchEnabled)}  ⭐ Отзывы: ${onOff(reviewsEnabled)}\n` +
      `💫 Баллы: ${onOff(loyaltyEnabled)}  🎁 Реферальная: ${onOff(referralEnabled)}\n` +
      `📊 Статистика моделей: ${onOff(modelStatsEnabled)}\n` +
      `❓ FAQ: ${onOff(faqEnabled)}  🧮 Калькулятор: ${onOff(calcEnabled)}\n` +
      `📣 Telegram канал: ${tgChannel || '—'}\n` +
      `🎉 Текст после бронирования: ${trunc(bookingThanks)}`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [{ text: `⚡ Быстрая заявка: ${onOff(quickBooking)}`, callback_data: 'adm_toggle_quick_booking' }],
          [{ text: `❤️ Wishlist: ${onOff(wishlistEnabled)}`, callback_data: 'adm_toggle_wishlist' }],
          [{ text: `🔍 Поиск: ${onOff(searchEnabled)}`, callback_data: 'adm_toggle_search' }],
          [{ text: `⭐ Отзывы: ${onOff(reviewsEnabled)}`, callback_data: 'adm_toggle_reviews' }],
          [{ text: `💫 Баллы лояльности: ${onOff(loyaltyEnabled)}`, callback_data: 'adm_toggle_loyalty' }],
          [{ text: `🎁 Реферальная: ${onOff(referralEnabled)}`, callback_data: 'adm_toggle_referral' }],
          [{ text: `📊 Статистика моделей: ${onOff(modelStatsEnabled)}`, callback_data: 'adm_toggle_model_stats' }],
          [
            { text: `❓ FAQ: ${onOff(faqEnabled)}`, callback_data: 'adm_toggle_faq' },
            { text: `🧮 Калькулятор: ${onOff(calcEnabled)}`, callback_data: 'adm_toggle_calc' },
          ],
          [
            { text: '🖼 Фото приветствия', callback_data: 'adm_set_welcome_photo' },
            { text: '📋 Текст меню', callback_data: 'adm_set_main_menu_text' },
          ],
          [{ text: '🎉 Текст после бронирования', callback_data: 'adm_set_booking_thanks' }],
          [{ text: '📣 Telegram канал', callback_data: 'adm_set_tg_channel' }],
          [{ text: '🔙 Назад', callback_data: 'adm_settings_main' }],
        ],
      },
    });
  }

  // ── Лимиты и доступ ───────────────────────────────────────────────────────
  if (section === 'limits') {
    const [maxPhotos, maxOrders, msgDelay, rateLimit] = await Promise.all([
      getSetting('model_max_photos'),
      getSetting('client_max_active_orders'),
      getSetting('client_msg_delay_sec'),
      getSetting('api_rate_limit'),
    ]);
    const text =
      `📊 Лимиты и доступ\n\n` +
      `🖼 Макс. фото у модели: ${maxPhotos || '8'}\n` +
      `📋 Макс. активных заявок у клиента: ${maxOrders || '3'}\n` +
      `⏱ Мин. интервал сообщений клиента (сек): ${msgDelay || '10'}\n` +
      `🔒 API rate limit (req/min): ${rateLimit || '60'}`;
    return safeSend(chatId, text, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '🖼 Макс. фото', callback_data: 'adm_set_model_max_photos' },
            { text: '📋 Макс. заявок', callback_data: 'adm_set_client_max_orders' },
          ],
          [
            { text: '⏱ Интервал сообщений', callback_data: 'adm_set_client_msg_delay' },
            { text: '🔒 Rate limit', callback_data: 'adm_set_api_rate_limit' },
          ],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }

  // ── Интерфейс (alias for bot section) ────────────────────────────────────
  if (section === 'ui') {
    return showAdminSettings(chatId, 'bot');
  }

  // ── Соцсети ───────────────────────────────────────────────────────────────
  if (section === 'social') {
    const [instaEnabled, insta] = await Promise.all([getSetting('instagram_enabled'), getSetting('contacts_insta')]);
    let socialCount = 0;
    try {
      const row = await get('SELECT COUNT(*) as cnt FROM social_posts');
      socialCount = row ? row.cnt : 0;
    } catch (_) {}
    const on = v => (v === '0' ? '❌' : '✅');
    const text =
      `*📱 Соцсети*\n\n` +
      `📸 Instagram: ${esc(insta || '—')}\n` +
      `${on(instaEnabled ?? '1')} Instagram включён\n` +
      `📋 Постов в очереди: ${esc(String(socialCount))}`;
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [
            {
              text: (instaEnabled ?? '1') === '0' ? '📸 Instagram ВКЛ' : '📸 Instagram ВЫКЛ',
              callback_data: (instaEnabled ?? '1') === '0' ? 'adm_instagram_on' : 'adm_instagram_off',
            },
          ],
          [{ text: '📸 Изменить Instagram', callback_data: 'adm_set_insta' }],
          [{ text: '← Настройки', callback_data: 'adm_settings' }],
        ],
      },
    });
  }
}

// ─── Add Model wizard ─────────────────────────────────────────────────────────

async function showAddModelStep(chatId, d) {
  const step = d._step || 'name';
  const progress = {
    name: 1,
    age: 2,
    height: 3,
    params: 4,
    shoe: 5,
    hair: 6,
    eye: 7,
    category: 8,
    instagram: 9,
    bio: 10,
    photo: 11,
  };
  const pct = Math.round(((progress[step] || 1) / 11) * 100);
  const bar = '█'.repeat(Math.round(pct / 10)) + '░'.repeat(10 - Math.round(pct / 10));

  const header = `➕ *Добавление модели* [${bar}]\n\n`;

  if (step === 'name') {
    await setSession(chatId, 'adm_mdl_name', d);
    return safeSend(chatId, header + '👤 Введите имя модели:', {
      reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'admin_menu' }]] },
    });
  }
  if (step === 'age') {
    await setSession(chatId, 'adm_mdl_age', d);
    return safeSend(chatId, header + `Имя: ${d.name}\n\n🎂 Введите возраст (лет):`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_age' },
            { text: '❌ Отмена', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  }
  if (step === 'height') {
    await setSession(chatId, 'adm_mdl_height', d);
    return safeSend(chatId, header + `Имя: ${d.name}\n\n📏 Введите рост (см, например: 176):`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_height' },
            { text: '❌ Отмена', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  }
  if (step === 'params') {
    await setSession(chatId, 'adm_mdl_params', d);
    return safeSend(chatId, header + `📐 Введите параметры в формате ОГ/ОТ/ОБ (например: 86/60/88)\nили пропустите:`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_params' },
            { text: '❌ Отмена', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  }
  if (step === 'shoe') {
    await setSession(chatId, 'adm_mdl_shoe', d);
    return safeSend(chatId, header + `👟 Введите размер обуви:`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_shoe' },
            { text: '❌ Отмена', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
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
    const btns = Object.entries(MODEL_CATEGORIES).map(([k, v]) => [{ text: v, callback_data: `adm_mdl_cat_${k}` }]);
    return safeSend(chatId, header + `🏷 Выберите категорию:`, { reply_markup: { inline_keyboard: btns } });
  }
  if (step === 'instagram') {
    await setSession(chatId, 'adm_mdl_instagram', d);
    return safeSend(chatId, header + `📸 Введите Instagram (без @, например: anna_model):`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_instagram' },
            { text: '❌ Отмена', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  }
  if (step === 'bio') {
    await setSession(chatId, 'adm_mdl_bio', d);
    return safeSend(chatId, header + `📝 Введите описание/портфолио модели:`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_bio' },
            { text: '❌ Отмена', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  }
  if (step === 'photo') {
    await setSession(chatId, 'adm_mdl_photo', d);
    return safeSend(chatId, header + `📷 Отправьте фото модели (главное фото карточки):`, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '⏭ Пропустить', callback_data: 'adm_mdl_skip_photo' },
            { text: '❌ Отмена', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  }
  if (step === 'confirm') {
    await setSession(chatId, 'adm_mdl_confirm', d);
    const params = d.bust ? `${d.bust}/${d.waist}/${d.hips}` : '—';
    let summary = `✅ Подтвердите добавление модели:\n\n`;
    summary += `👤 Имя: ${d.name}\n`;
    if (d.age) summary += `🎂 Возраст: ${d.age} лет\n`;
    if (d.height) summary += `📏 Рост: ${d.height} см\n`;
    if (d.bust) summary += `📐 Параметры: ${params}\n`;
    if (d.shoe_size) summary += `👟 Обувь: ${d.shoe_size}\n`;
    if (d.hair_color) summary += `💇 Волосы: ${d.hair_color}\n`;
    if (d.eye_color) summary += `👁 Глаза: ${d.eye_color}\n`;
    if (d.category) summary += `🏷 Категория: ${MODEL_CATEGORIES[d.category] || d.category}\n`;
    if (d.instagram) summary += `📸 Instagram: @${d.instagram}\n`;
    if (d.bio) summary += `📝 Описание: ${d.bio.slice(0, 80)}${d.bio.length > 80 ? '...' : ''}\n`;
    if (d.photo_id) summary += `📷 Фото: ✅ загружено\n`;
    return safeSend(chatId, summary, {
      reply_markup: {
        inline_keyboard: [
          [{ text: '✅ Добавить модель', callback_data: 'adm_mdl_save' }],
          [{ text: '❌ Отмена', callback_data: 'admin_menu' }],
        ],
      },
    });
  }
}

async function saveNewModel(chatId, d) {
  try {
    const res = await run(
      `INSERT INTO models (name,age,height,weight,bust,waist,hips,shoe_size,hair_color,eye_color,bio,instagram,category,photo_main,available)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)`,
      [
        d.name,
        d.age || null,
        d.height || null,
        d.weight || null,
        d.bust || null,
        d.waist || null,
        d.hips || null,
        d.shoe_size || null,
        d.hair_color || null,
        d.eye_color || null,
        d.bio || null,
        d.instagram || null,
        d.category || 'fashion',
        d.photo_file_id || null,
      ]
    );
    await logAdminAction(chatId, 'add_model', 'model', res.id, { name: d.name });
    await clearSession(chatId);
    return safeSend(chatId, `✅ Модель «${d.name}» добавлена!\n\nID: ${res.id}`, {
      reply_markup: {
        inline_keyboard: [
          [{ text: '👁 Просмотреть карточку', callback_data: `adm_model_${res.id}` }],
          [{ text: '➕ Добавить ещё', callback_data: 'adm_addmodel' }],
          [{ text: '← Меню', callback_data: 'admin_menu' }],
        ],
      },
    });
  } catch (e) {
    return safeSend(chatId, `❌ Ошибка сохранения: ${e.message}`);
  }
}

// ─── Edit Model ───────────────────────────────────────────────────────────────

async function showModelEditMenu(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');
  return safeSend(chatId, `✏️ *Редактировать: ${m.name}*\n\nВыберите поле:`, {
    reply_markup: {
      inline_keyboard: [
        [
          { text: '👤 Имя', callback_data: `adm_ef_${modelId}_name` },
          { text: '🎂 Возраст', callback_data: `adm_ef_${modelId}_age` },
        ],
        [
          { text: '📏 Рост', callback_data: `adm_ef_${modelId}_height` },
          { text: '⚖️ Вес', callback_data: `adm_ef_${modelId}_weight` },
        ],
        [
          { text: '📐 Параметры', callback_data: `adm_ef_${modelId}_params` },
          { text: '👟 Обувь', callback_data: `adm_ef_${modelId}_shoe_size` },
        ],
        [
          { text: '💇 Волосы', callback_data: `adm_ef_${modelId}_hair_color` },
          { text: '👁 Глаза', callback_data: `adm_ef_${modelId}_eye_color` },
        ],
        [
          { text: '📸 Instagram', callback_data: `adm_ef_${modelId}_instagram` },
          { text: '🏷 Категория', callback_data: `adm_ef_${modelId}_category` },
        ],
        [
          { text: '📞 Телефон', callback_data: `adm_ef_${modelId}_phone` },
          { text: '🏙 Город', callback_data: `adm_ef_${modelId}_city` },
        ],
        [{ text: '📝 Описание', callback_data: `adm_ef_${modelId}_bio` }],
        [{ text: '🎬 Видео URL', callback_data: `adm_ef_${modelId}_video_url` }],
        [{ text: '🤖 AI описание', callback_data: `adm_ai_bio_${modelId}` }],
        [{ text: '📷 Галерея фото', callback_data: `adm_gallery_${modelId}` }],
        [
          { text: m.available ? '🔴 Недоступна' : '🟢 Доступна', callback_data: `adm_toggle_${modelId}` },
          { text: m.featured ? '⭐ Убрать из топа' : '⭐ В топ', callback_data: `adm_featured_${modelId}` },
        ],
        [{ text: '🗑 Удалить модель', callback_data: `adm_del_model_${modelId}` }],
        [{ text: '← Карточка', callback_data: `adm_model_${modelId}` }],
      ],
    },
  });
}

// ─── Model Comparison ─────────────────────────────────────────────────────────

// In-memory compare lists per chat (up to 3 models)
const _compareLists = new Map(); // chatId → Set of modelIds
// Cleanup stale compare lists every 24 hours (users rarely compare for >24h)
setInterval(
  () => {
    _compareLists.clear();
  },
  24 * 60 * 60 * 1000
).unref();

async function addToCompare(chatId, modelId) {
  const key = String(chatId);
  if (!_compareLists.has(key)) _compareLists.set(key, new Set());
  const list = _compareLists.get(key);
  if (list.has(modelId)) {
    return safeSend(chatId, '⚖️ Эта модель уже в списке сравнения\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '⚖️ Показать сравнение', callback_data: 'compare_show' }],
          [{ text: '💃 Каталог', callback_data: 'cat_cat__0' }],
        ],
      },
    });
  }
  if (list.size >= 3) {
    return safeSend(chatId, '⚖️ Можно сравнивать не более 3 моделей\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '⚖️ Показать сравнение', callback_data: 'compare_show' }],
          [{ text: '🗑 Очистить список', callback_data: 'compare_clear' }],
        ],
      },
    });
  }
  list.add(modelId);
  const m = await get('SELECT name FROM models WHERE id=?', [modelId]).catch(() => null);
  return safeSend(chatId, `✅ *${esc(m?.name || String(modelId))}* добавлена в сравнение \\(${list.size}/3\\)`, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '⚖️ Показать сравнение', callback_data: 'compare_show' }],
        [{ text: '💃 Продолжить каталог', callback_data: 'cat_cat__0' }],
        [{ text: '🗑 Очистить список', callback_data: 'compare_clear' }],
      ],
    },
  });
}

async function showComparison(chatId) {
  const key = String(chatId);
  const list = _compareLists.get(key);
  if (!list || list.size === 0) {
    return safeSend(chatId, '⚖️ Список сравнения пуст\\. Добавьте модели из каталога\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] },
    });
  }
  const modelIds = [...list];
  const models = await Promise.all(modelIds.map(id => get('SELECT * FROM models WHERE id=?', [id]).catch(() => null)));
  const valid = models.filter(Boolean);
  if (!valid.length) {
    _compareLists.delete(key);
    return safeSend(chatId, '⚖️ Список сравнения пуст\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] },
    });
  }

  const catMap = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };
  const pad = (s, n) => {
    const str = String(s ?? '—');
    return str.length >= n ? str.slice(0, n) : str + ' '.repeat(n - str.length);
  };
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
  for (const m of valid) table += pad(m.bust && m.waist && m.hips ? `${m.bust}/${m.waist}/${m.hips}` : '—', COL);
  table += '\n';
  table += pad('Категория:', LABEL);
  for (const m of valid) table += pad(catMap[m.category] || m.category || '—', COL);
  table += '\n';
  table += pad('Статус:', LABEL);
  for (const m of valid) table += pad(m.available ? 'Свободна' : 'Занята', COL);

  const text = `⚖️ *Сравнение моделей*\n\n\`\`\`\n${table}\n\`\`\``;
  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        valid.map(m => ({ text: m.name.split(' ')[0], callback_data: `cat_model_${m.id}` })),
        [{ text: '🗑 Очистить список', callback_data: 'compare_clear' }],
        [{ text: '💃 Каталог', callback_data: 'cat_cat__0' }],
      ],
    },
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

  proc.stdout.on('data', d => {
    output += d.toString();
  });
  proc.stderr.on('data', d => {
    errorOut += d.toString();
  });

  proc.on('close', async code => {
    const bio = output.trim();
    if (!bio || code !== 0) {
      console.error('[Bot] AI bio error:', errorOut);
      return safeSend(chatId, '❌ Ошибка генерации AI описания\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Редактировать', callback_data: `adm_editmodel_${modelId}` }]] },
      });
    }
    await setSession(chatId, `adm_ai_bio_preview_${modelId}`, { ai_bio: bio });
    return safeSend(chatId, `🤖 *AI описание для ${esc(m.name)}:*\n\n_${esc(bio)}_\n\nПрименить это описание?`, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '✅ Применить', callback_data: `adm_ai_bio_apply_${modelId}` }],
          [{ text: '🔄 Сгенерировать ещё', callback_data: `adm_ai_bio_${modelId}` }],
          [{ text: '← Отмена', callback_data: `adm_editmodel_${modelId}` }],
        ],
      },
    });
  });

  proc.on('error', async err => {
    console.error('[Bot] AI bio spawn error:', err.message);
    return safeSend(chatId, `❌ Не удалось запустить AI: ${esc(err.message)}`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Редактировать', callback_data: `adm_editmodel_${modelId}` }]] },
    });
  });
}

// ─── Photo Gallery Manager ─────────────────────────────────────────────────────

async function showPhotoGalleryManager(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT id, name, photo_main, photos FROM models WHERE id=?', [modelId]);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');
  let gallery = [];
  try {
    gallery = JSON.parse(m.photos || '[]');
  } catch {}
  const all = m.photo_main ? [m.photo_main, ...gallery] : gallery;
  const count = all.length;
  await setSession(chatId, `adm_gallery_${modelId}`, {});
  return safeSend(
    chatId,
    `📷 Галерея: *${esc(m.name)}*\nФото: *${count}/8* загружено\n\nОтправляйте фото одно за другим \\(до 8 штук\\)\\.\nПервое фото станет главным\\.`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '🗑 Очистить все фото', callback_data: `adm_gallery_clear_${modelId}` }],
          [{ text: '✅ Готово', callback_data: `adm_model_${modelId}` }],
          [{ text: '← Редактировать', callback_data: `adm_editmodel_${modelId}` }],
        ],
      },
    }
  );
}

// ─── Audit log viewer ─────────────────────────────────────────────────────────

async function showAuditLog(chatId, page = 0) {
  if (!isAdmin(chatId)) return;
  const logs = await query(
    `
    SELECT al.*, a.username FROM audit_log al
    LEFT JOIN admins a ON al.admin_chat_id = a.chat_id
    ORDER BY al.created_at DESC LIMIT 10 OFFSET ?`,
    [page * 10]
  ).catch(() => []);

  if (!logs.length)
    return safeSend(chatId, '📋 Журнал действий пуст\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] },
    });

  const actionLabels = {
    change_order_status: '🔄 Статус заявки',
    delete_model: '🗑 Удаление модели',
    update_setting: '⚙️ Настройки',
    broadcast: '📢 Рассылка',
    add_model: '➕ Добавление модели',
    archive_model: '📦 Архивация',
    toggle_availability: '🟢 Доступность модели',
  };

  const lines = logs.map(l => {
    const dt = new Date(l.created_at).toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
    const action = actionLabels[l.action] || esc(l.action);
    const who = l.username ? ` \\(${esc(l.username)}\\)` : '';
    return `• *${esc(dt)}*${who} — ${action}`;
  });

  await safeSend(chatId, `📋 *Журнал действий*\n\n${lines.join('\n')}`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] },
  });
}

// ─── Broadcast ────────────────────────────────────────────────────────────────

function _bcSegmentLabel(segment) {
  if (segment === 'all') return 'Все клиенты';
  if (segment === 'completed') return 'Завершённые заявки';
  if (segment === 'new') return 'Новые (без заявок)';
  if (segment === 'active') return 'Активные (30 дней)';
  if (segment && segment.startsWith('city:')) return `Город: ${segment.slice(5)}`;
  if (segment && segment.startsWith('city_')) return `Город: ${segment.slice(5)}`;
  return 'Все клиенты';
}

// Filter out admin IDs from broadcast recipients
function _filterAdminRecipients(clients) {
  return clients.filter(c => !ADMIN_IDS.includes(String(c.client_chat_id)));
}

async function _getBroadcastClients(segment) {
  try {
    let rows = [];
    if (segment === 'completed') {
      rows = await query(
        "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND status='completed'"
      ).catch(() => []);
    } else if (segment === 'active') {
      // Clients who had any order in the last 30 days
      rows = await query(
        "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND created_at >= datetime('now', '-30 days')"
      ).catch(() => []);
    } else if (segment && (segment.startsWith('city:') || segment.startsWith('city_'))) {
      const city = segment.startsWith('city:') ? segment.slice(5) : segment.slice(5);
      rows = await query(
        `SELECT DISTINCT o.client_chat_id
         FROM orders o
         JOIN models m ON o.model_id = m.id
         WHERE o.client_chat_id IS NOT NULL
           AND o.client_chat_id != ''
           AND m.city = ?`,
        [city]
      ).catch(() => []);
    } else if (segment === 'new') {
      rows = await query(
        "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND client_chat_id NOT IN (SELECT DISTINCT client_chat_id FROM orders WHERE status IN ('confirmed','in_progress','completed') AND client_chat_id IS NOT NULL AND client_chat_id != '')"
      ).catch(() => []);
    } else {
      // default: all
      rows = await query(
        "SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != ''"
      ).catch(() => []);
    }
    return _filterAdminRecipients(rows);
  } catch {
    return [];
  }
}

async function _bcCountRecipients(segment) {
  const clients = await _getBroadcastClients(segment);
  return clients.length;
}

async function showBroadcast(chatId) {
  if (!isAdmin(chatId)) return;
  const [allCount, completedCount, activeCount, newCount] = await Promise.all([
    _bcCountRecipients('all'),
    _bcCountRecipients('completed'),
    _bcCountRecipients('active'),
    _bcCountRecipients('new'),
  ]);

  return safeSend(chatId, `📢 *Рассылка* — Выберите аудиторию:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [
          { text: `👥 Всем клиентам (${allCount})`, callback_data: 'adm_bc_seg_all' },
          { text: `✅ Завершённые (${completedCount})`, callback_data: 'adm_bc_seg_completed' },
        ],
        [
          { text: `🕐 Активные 30д (${activeCount})`, callback_data: 'adm_bc_seg_active' },
          { text: `🆕 Новые (${newCount})`, callback_data: 'adm_bc_seg_new' },
        ],
        [{ text: '🏙 По городу', callback_data: 'adm_bc_seg_city' }],
        [{ text: '📋 История рассылок', callback_data: 'adm_broadcast_history' }],
        [{ text: '← Назад', callback_data: 'admin_menu' }],
      ],
    },
  });
}

async function showBroadcastCitySelection(chatId) {
  if (!isAdmin(chatId)) return;
  const citiesSetting = await getSetting('cities_list').catch(() => '');
  let cityList = citiesSetting
    ? citiesSetting
        .split(',')
        .map(c => c.trim())
        .filter(Boolean)
        .slice(0, 8)
    : [];
  if (!cityList.length) {
    const rows = await query(
      "SELECT DISTINCT city FROM models WHERE city IS NOT NULL AND city != '' ORDER BY city LIMIT 8"
    ).catch(() => []);
    cityList = rows.map(r => r.city);
  }
  if (!cityList.length) {
    return safeSend(chatId, '❌ Нет городов для выбора. Добавьте города в настройках или добавьте моделям города.', {
      reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_broadcast' }]] },
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
    reply_markup: { inline_keyboard: cityButtons },
  });
}

async function _askBroadcastText(chatId, segment) {
  const label = _bcSegmentLabel(segment);
  const count = await _bcCountRecipients(segment);
  const sess = await getSession(chatId);
  const sd = sessionData(sess);
  await setSession(chatId, 'adm_broadcast_msg', { ...sd, broadcastSegment: segment });
  return safeSend(
    chatId,
    `📢 *Рассылка*\nАудитория: *${esc(label)}* \\(${count} получ\\.\\)\n\nВведите текст сообщения:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] },
    }
  );
}

async function _askBroadcastPhoto(chatId) {
  return safeSend(chatId, `✅ *Текст получен\\!*\n\nДобавить фото к рассылке?`, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [
          { text: '🖼 Добавить фото', callback_data: 'adm_bc_photo' },
          { text: '▶ Отправить без фото', callback_data: 'adm_bc_send_now' },
        ],
        [{ text: '❌ Отмена', callback_data: 'adm_broadcast' }],
      ],
    },
  });
}

async function previewBroadcast(chatId) {
  const sess = await getSession(chatId);
  const sd = sessionData(sess);
  const segment = sd.broadcastSegment || 'all';
  const text = sd.broadcastText || '';
  const photoId = sd.broadcastPhotoId || null;
  const label = _bcSegmentLabel(segment);
  const recipients = sd.broadcastRecipients || [];
  const count = recipients.length;

  const headerText = `📢 *Предпросмотр рассылки:*\nАудитория: *${esc(label)}* \\(${count} получ\\.\\)\n─────`;
  await safeSend(chatId, headerText, { parse_mode: 'MarkdownV2' });

  const msgBody = text ? `📢 *Сообщение от Nevesty Models*\n\n${esc(text)}` : '📢 *Nevesty Models*';
  if (photoId) {
    await safePhoto(chatId, photoId, { caption: msgBody.slice(0, 1020), parse_mode: 'MarkdownV2' }).catch(() => {});
  } else {
    await safeSend(chatId, msgBody.slice(0, 4096), { parse_mode: 'MarkdownV2' }).catch(() => {});
  }

  const photoRowBtn = photoId
    ? [
        { text: '🖼 Изменить фото', callback_data: 'adm_bc_edit_photo' },
        { text: '🗑 Убрать фото', callback_data: 'adm_bc_remove_photo' },
      ]
    : [{ text: '📷 Добавить фото', callback_data: 'adm_bc_edit_photo' }];

  return safeSend(chatId, '─────\n📤 Отправить рассылку?', {
    reply_markup: {
      inline_keyboard: [
        [
          { text: '✅ Отправить сейчас', callback_data: 'adm_bc_confirm' },
          { text: '🕐 Запланировать', callback_data: 'adm_bc_schedule' },
        ],
        photoRowBtn,
        [
          { text: '✏️ Изменить текст', callback_data: 'adm_bc_edit' },
          { text: '❌ Отмена', callback_data: 'adm_bc_cancel_preview' },
        ],
      ],
    },
  });
}

async function sendBroadcast(chatId, text, preservePhoto = false) {
  const sess = await getSession(chatId);
  const sd = sessionData(sess);
  const segment = sd.broadcastSegment || 'all';
  const clients = await _getBroadcastClients(segment);
  if (!clients.length)
    return safeSend(chatId, '❌ Нет клиентов для рассылки.', {
      reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] },
    });
  // Preserve existing photo when admin is only editing text from preview
  const photoId = preservePhoto ? sd.broadcastPhotoId || null : null;
  const newSd = {
    ...sd,
    broadcastRecipients: clients.map(c => c.client_chat_id),
    broadcastText: text,
    broadcastPhotoId: photoId,
  };
  await setSession(chatId, 'adm_broadcast_preview', newSd);
  if (preservePhoto) {
    // Coming from "edit text" in preview — skip photo prompt, go straight to preview
    return previewBroadcast(chatId);
  }
  return _askBroadcastPhoto(chatId);
}

async function _sendOneBroadcastMsg(cid, photoId, text) {
  const caption = text ? `📢 *Сообщение от Nevesty Models*\n\n${esc(text)}` : '📢 *Nevesty Models*';
  const MAX_RETRIES = 3;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      if (photoId) {
        await bot.sendPhoto(cid, photoId, { caption: caption.slice(0, 1020), parse_mode: 'MarkdownV2' });
      } else {
        await bot.sendMessage(cid, caption.slice(0, 4096), { parse_mode: 'MarkdownV2' });
      }
      return 'ok';
    } catch (err) {
      // Handle Telegram 429 Too Many Requests — wait retry_after seconds then retry
      const retryAfter =
        err?.response?.parameters?.retry_after ||
        (err?.message && /retry after (\d+)/i.test(err.message)
          ? parseInt(err.message.match(/retry after (\d+)/i)[1])
          : null);
      if (retryAfter && attempt < MAX_RETRIES - 1) {
        await new Promise(r => setTimeout(r, (retryAfter + 1) * 1000));
        continue;
      }
      return 'fail';
    }
  }
  return 'fail';
}

async function doSendBroadcast(chatId) {
  const sess = await getSession(chatId);
  const sd = sessionData(sess);
  const recipients = sd.broadcastRecipients || [];
  const text = sd.broadcastText || '';
  const photoId = sd.broadcastPhotoId || null;
  const segment = sd.broadcastSegment || 'all';

  // Create broadcast record in DB
  let broadcastId = null;
  try {
    const bcRow = await run(
      `INSERT INTO bot_broadcasts (message, photo_id, segment, sent_by, total_recipients, status)
       VALUES (?, ?, ?, ?, ?, 'sending')`,
      [text, photoId || null, segment, String(chatId), recipients.length]
    );
    broadcastId = bcRow.id;
  } catch (e) {
    console.error('[Broadcast] Failed to create broadcast record:', e.message);
  }

  // Notify admin that sending started
  await safeSend(chatId, `📤 Начинаю рассылку для *${recipients.length}* получателей\\.\\.\\.`, {
    parse_mode: 'MarkdownV2',
  }).catch(() => {});

  const startTime = Date.now();
  let sent = 0,
    failed = 0;
  for (const cid of recipients) {
    const result = await _sendOneBroadcastMsg(cid, photoId, text);
    if (result === 'ok') sent++;
    else failed++;
    await new Promise(r => setTimeout(r, 50)); // 50ms delay between sends (rate limit)
  }
  const durationSec = Math.round((Date.now() - startTime) / 1000);

  // Update broadcast record with final stats
  if (broadcastId) {
    run(`UPDATE bot_broadcasts SET delivered=?, failed=?, status='done', finished_at=datetime('now') WHERE id=?`, [
      sent,
      failed,
      broadcastId,
    ]).catch(e => console.error('[Broadcast] Failed to update broadcast stats:', e.message));
  }

  await logAdminAction(chatId, 'broadcast', null, null, { sent, failed, segment, duration: durationSec });
  await clearSession(chatId);
  const total = recipients.length;
  const segLabel = _bcSegmentLabel(segment);
  return safeSend(
    chatId,
    `📊 *Рассылка завершена\\!*\n\n✅ Доставлено: *${sent}*\n❌ Ошибок: *${failed}*\n📬 Всего: *${total}*\n🎯 Аудитория: *${esc(segLabel)}*\n⏱ Время: *${durationSec}с*`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '📋 История рассылок', callback_data: 'adm_broadcast_history' }],
          [{ text: '← Меню', callback_data: 'admin_menu' }],
        ],
      },
    }
  );
}

async function showBroadcastHistory(chatId) {
  if (!isAdmin(chatId)) return;
  const rows = await query(
    `SELECT id, message, photo_id, segment, delivered, failed, total_recipients, status, started_at, finished_at
     FROM bot_broadcasts
     ORDER BY started_at DESC
     LIMIT 5`
  ).catch(() => []);

  let text = `📋 *История рассылок \\(последние 5\\)*\n\n`;
  if (!rows.length) {
    text += '_Рассылок ещё не было_';
  } else {
    for (const b of rows) {
      const dt = b.started_at
        ? new Date(b.started_at).toLocaleString('ru', {
            timeZone: 'Europe/Moscow',
            day: '2-digit',
            month: '2-digit',
            year: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
          })
        : '—';
      const segLabel = _bcSegmentLabel(b.segment || 'all');
      const msgType = b.photo_id ? '🖼' : '📝';
      const statusEmoji = b.status === 'done' ? '✅' : b.status === 'sending' ? '🔄' : '⏳';
      const delivered = b.delivered ?? 0;
      const failed = b.failed ?? 0;
      const total = b.total_recipients ?? delivered + failed;
      const preview = String(b.message || '').slice(0, 50);
      const previewText = preview ? esc(preview) + ((b.message || '').length > 50 ? '…' : '') : '_без текста_';
      text += `${statusEmoji} ${msgType} *${esc(dt)}* — ${esc(segLabel)}\n`;
      text += `✅ ${delivered}  ❌ ${failed}  📬 ${total}\n`;
      text += `${previewText}\n\n`;
    }
  }

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '📢 Новая рассылка', callback_data: 'adm_broadcast' }],
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ],
    },
  });
}

async function sendBroadcastWithPhoto(chatId, photoFileId, caption) {
  const sess = await getSession(chatId);
  const sd = sessionData(sess);
  const segment = sd.broadcastSegment || 'all';
  const clients = await _getBroadcastClients(segment);
  if (!clients.length)
    return safeSend(chatId, '❌ Нет клиентов для рассылки.', {
      reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] },
    });
  const newSd = {
    ...sd,
    broadcastRecipients: clients.map(c => c.client_chat_id),
    broadcastPhotoId: photoFileId,
    broadcastText: caption,
  };
  await setSession(chatId, 'adm_broadcast_preview', newSd);
  return previewBroadcast(chatId);
}

// ─── Scheduled Broadcasts ────────────────────────────────────────────────────

async function showScheduledBroadcasts(chatId) {
  if (!isAdmin(chatId)) return;
  const broadcasts = await query(`SELECT * FROM scheduled_broadcasts ORDER BY scheduled_at ASC LIMIT 20`).catch(
    () => []
  );

  let text = `📅 *Запланированные рассылки*\n\n`;
  if (!broadcasts.length) {
    text += '_Нет запланированных рассылок_';
  } else {
    for (const b of broadcasts) {
      const dt = b.scheduled_at
        ? new Date(b.scheduled_at).toLocaleString('ru', {
            timeZone: 'Europe/Moscow',
            day: '2-digit',
            month: '2-digit',
            year: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
          })
        : '—';
      const statusEmoji = b.status === 'sent' ? '✅' : b.status === 'cancelled' ? '❌' : '⏳';
      const segLabel = b.segment === 'completed' ? 'Завершившие' : b.segment === 'active' ? 'Активные' : 'Все';
      const stats = b.sent_count ? ` ✅${b.sent_count}` : '';
      const errStats = b.error_count ? ` ❌${b.error_count}` : '';
      text += `${statusEmoji} *${esc(dt)}* \\[${esc(segLabel)}\\]${esc(stats)}${esc(errStats)}\n${esc(String(b.text || '').slice(0, 60))}${(b.text || '').length > 60 ? '…' : ''}\n\n`;
    }
  }

  const keyboard = [];
  for (const b of broadcasts.filter(b => b.status === 'pending')) {
    const dtShort = b.scheduled_at
      ? new Date(b.scheduled_at).toLocaleString('ru', {
          timeZone: 'Europe/Moscow',
          day: '2-digit',
          month: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
      : '—';
    keyboard.push([{ text: `❌ Отменить #${b.id} (${dtShort})`, callback_data: `adm_bc_cancel_${b.id}` }]);
  }
  keyboard.push([{ text: '➕ Создать рассылку', callback_data: 'adm_new_sched_bcast' }]);
  keyboard.push([{ text: '← Назад', callback_data: 'admin_menu' }]);

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard },
  });
}

// ─── Model Stats (admin) ──────────────────────────────────────────────────────

async function showModelStats(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT * FROM models WHERE id=?', [modelId]).catch(() => null);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');

  const [
    totalOrders,
    completedOrders,
    cancelledOrders,
    activeOrders,
    revenue,
    avgBudget,
    avgRating,
    topCities,
    topEventTypes,
  ] = await Promise.all([
    get('SELECT COUNT(*) as n FROM orders WHERE model_id=?', [modelId]).catch(() => ({ n: 0 })),
    get("SELECT COUNT(*) as n FROM orders WHERE model_id=? AND status='completed'", [modelId]).catch(() => ({ n: 0 })),
    get("SELECT COUNT(*) as n FROM orders WHERE model_id=? AND status='cancelled'", [modelId]).catch(() => ({ n: 0 })),
    get("SELECT COUNT(*) as n FROM orders WHERE model_id=? AND status NOT IN ('completed','cancelled')", [
      modelId,
    ]).catch(() => ({ n: 0 })),
    get(
      "SELECT SUM(CAST(REPLACE(REPLACE(REPLACE(budget,' ',''),'₽',''),',','.') AS REAL)) as total FROM orders WHERE model_id=? AND status='completed' AND budget IS NOT NULL AND budget != ''",
      [modelId]
    ).catch(() => ({ total: null })),
    get(
      "SELECT AVG(CAST(REPLACE(REPLACE(REPLACE(budget,' ',''),'₽',''),',','.') AS REAL)) as avg FROM orders WHERE model_id=? AND budget IS NOT NULL AND budget != ''",
      [modelId]
    ).catch(() => ({ avg: null })),
    get('SELECT AVG(rating) as avg, COUNT(*) as cnt FROM reviews WHERE model_id=? AND approved=1', [modelId]).catch(
      () => ({ avg: null, cnt: 0 })
    ),
    query(
      "SELECT location, COUNT(*) as cnt FROM orders WHERE model_id=? AND location IS NOT NULL AND location != '' GROUP BY location ORDER BY cnt DESC LIMIT 3",
      [modelId]
    ).catch(() => []),
    query(
      "SELECT event_type, COUNT(*) as cnt FROM orders WHERE model_id=? AND event_type IS NOT NULL AND event_type != '' GROUP BY event_type ORDER BY cnt DESC LIMIT 5",
      [modelId]
    ).catch(() => []),
  ]);

  let text = `📊 *Статистика модели*\n\n`;
  text += `💃 *${esc(m.name)}*\n`;
  if (m.city) text += `📍 ${esc(m.city)}\n`;
  text += `\n`;

  // Orders breakdown
  text += `📋 *Заявки:*\n`;
  text += `• Всего: *${totalOrders?.n || 0}*\n`;
  text += `• Активных: *${activeOrders?.n || 0}*\n`;
  text += `• Завершено: *${completedOrders?.n || 0}*\n`;
  text += `• Отменено: *${cancelledOrders?.n || 0}*\n`;
  text += `\n`;

  // Revenue & budget
  text += `💰 *Финансы:*\n`;
  if (revenue?.total) {
    text += `• Выручка \\(завершённые\\): *${esc(Math.round(revenue.total).toLocaleString('ru'))} ₽*\n`;
  } else {
    text += `• Выручка: нет данных\n`;
  }
  if (avgBudget?.avg) {
    text += `• Средний бюджет: *${esc(Math.round(avgBudget.avg).toLocaleString('ru'))} ₽*\n`;
  }
  text += `\n`;

  // Views & rating
  text += `👁 Просмотров: *${m.view_count || 0}*\n`;
  if (avgRating?.cnt > 0) {
    const stars = '⭐'.repeat(Math.min(5, Math.round(avgRating.avg)));
    text += `${stars} Рейтинг: *${esc(Number(avgRating.avg).toFixed(1))}* \\(${avgRating.cnt} отз\\.\\.\\)\n`;
  } else {
    text += `⭐ Отзывов пока нет\n`;
  }

  // Top cities
  if (topCities.length) {
    text += `\n🏙 *Топ городов:*\n`;
    for (const c of topCities) {
      text += `• ${esc(c.location)} \\(${c.cnt}\\)\n`;
    }
  }

  // Top event types
  if (topEventTypes.length) {
    text += `\n🎭 *Типы мероприятий:*\n`;
    for (const e of topEventTypes) {
      const label = EVENT_TYPES[e.event_type] || e.event_type;
      text += `• ${esc(label)} \\(${e.cnt}\\)\n`;
    }
  }

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '← К карточке модели', callback_data: `adm_model_${modelId}` }],
        [{ text: '← Модели', callback_data: 'adm_models_0' }],
      ],
    },
  });
}

// ─── Admin: Model Calendar ────────────────────────────────────────────────────

async function showAdminModelCalendar(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  const m = await get('SELECT id, name FROM models WHERE id=?', [modelId]).catch(() => null);
  if (!m) return safeSend(chatId, '❌ Модель не найдена.');

  // Upcoming 3 months of busy dates
  const threeMonthsLater = new Date();
  threeMonthsLater.setMonth(threeMonthsLater.getMonth() + 3);
  const dateTo = threeMonthsLater.toISOString().slice(0, 10);

  const busyRows = await query(
    `SELECT id, busy_date, reason FROM model_busy_dates
     WHERE model_id=? AND busy_date >= date('now') AND busy_date <= ?
     ORDER BY busy_date`,
    [modelId, dateTo]
  ).catch(() => []);

  let text = `📅 *Расписание: ${esc(m.name)}*\n\n`;

  const busyButtons = [];

  if (busyRows.length === 0) {
    text += '✅ Свободна в ближайшие 3 месяца';
  } else {
    const ranges = groupBusyDatesIntoRanges(busyRows);
    for (const r of ranges) {
      const rangeStr =
        r.start === r.end ? formatDateShort(r.start) : `${formatDateShort(r.start)}–${formatDateShort(r.end)}`;
      const reasonPart = r.reason ? `: ${esc(r.reason)}` : '';
      text += `🔴 ${rangeStr}${reasonPart}\n`;
      // Add delete button for the start date of the range (deletes that date entry)
      // Find matching row id for start date
      const startRow = busyRows.find(row => row.busy_date === r.start);
      if (startRow) {
        busyButtons.push([
          {
            text: `🗑 Удалить: ${formatDateShort(r.start)}`,
            callback_data: `adm_del_busy_${modelId}_${r.start}`,
          },
        ]);
      }
    }
    text += '\n✅ Свободна в остальные дни';
  }

  const keyboard = {
    inline_keyboard: [
      ...busyButtons,
      [{ text: '➕ Добавить период', callback_data: `adm_add_busy_${modelId}` }],
      [{ text: '← Назад', callback_data: `adm_model_${modelId}` }],
    ],
  };

  return safeSend(chatId, text, { parse_mode: 'MarkdownV2', reply_markup: keyboard });
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
  const keyboard = QUICK_REPLY_TEMPLATES.map((t, i) => [
    {
      text: t.slice(0, 50),
      callback_data: `qr_send_${i}_${clientChatId}`,
    },
  ]);
  keyboard.push([{ text: '❌ Закрыть', callback_data: 'adm_orders__0' }]);

  return safeSend(chatId, `💬 *Быстрые ответы*\nВыберите шаблон для клиента \\(ID: ${esc(String(clientChatId))}\\):`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard },
  });
}

// ─── Quick note templates ─────────────────────────────────────────────────────

const QUICK_NOTE_TEMPLATES = {
  call: '📞 Связались с клиентом',
  budget: '💰 Уточнение бюджета',
  date: '🗓 Дата мероприятия согласована',
  logistics: '🚗 Логистика и расположение обсуждены',
};

async function showQuickNoteTemplates(chatId, orderId) {
  if (!isAdmin(chatId)) return;
  return safeSend(chatId, `📝 *Быстрые заметки* — выберите шаблон:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [
          { text: '📞 Связались с клиентом', callback_data: `adm_qnote_${orderId}_call` },
          { text: '💰 Уточняем бюджет', callback_data: `adm_qnote_${orderId}_budget` },
        ],
        [
          { text: '🗓 Дата согласована', callback_data: `adm_qnote_${orderId}_date` },
          { text: '🚗 Логистика', callback_data: `adm_qnote_${orderId}_logistics` },
        ],
        [{ text: '✏️ Своя заметка', callback_data: `adm_qnote_${orderId}_custom` }],
        [{ text: '❌ Отмена', callback_data: `adm_order_${orderId}` }],
      ],
    },
  });
}

// ─── All order notes (paginated) ──────────────────────────────────────────────

// Format date for note display: "15 мая 14:30"
function formatNoteDate(dateStr) {
  if (!dateStr) return '—';
  try {
    const d = new Date(dateStr);
    return d
      .toLocaleString('ru', {
        timeZone: 'Europe/Moscow',
        day: 'numeric',
        month: 'long',
        hour: '2-digit',
        minute: '2-digit',
      })
      .replace(' г.', '');
  } catch {
    return dateStr;
  }
}

async function showAllOrderNotes(chatId, orderId, page = 0) {
  if (!isAdmin(chatId)) return;
  const LIMIT = 5;
  const [order, notes, total] = await Promise.all([
    get('SELECT order_number FROM orders WHERE id=?', [orderId]).catch(() => null),
    query('SELECT * FROM order_notes WHERE order_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?', [
      orderId,
      LIMIT,
      page * LIMIT,
    ]).catch(() => []),
    get('SELECT COUNT(*) as n FROM order_notes WHERE order_id=?', [orderId]).catch(() => ({ n: 0 })),
  ]);
  if (!order) return safeSend(chatId, RU.ORDER_NOT_FOUND);

  const totalCount = total?.n || 0;
  let text = `📝 *Все заметки*\nЗаявка *${esc(order.order_number)}* \\(${esc(String(totalCount))} шт\\.\\)\n\n`;
  const keyboard = [];

  if (!notes.length) {
    text += '_Заметок пока нет_';
  } else {
    for (const n of notes) {
      const dt = formatNoteDate(n.created_at);
      const noteNum = page * LIMIT + notes.indexOf(n) + 1;
      text += `*${esc(String(noteNum))}\\.* _${esc(dt)}_\n${esc(n.admin_note)}\n\n`;
      // Delete button per note
      keyboard.push([
        { text: `🗑 Удалить заметку #${noteNum}`, callback_data: `adm_note_del_${n.id}_${orderId}_${page}` },
      ]);
    }
  }

  const nav = [];
  if (page > 0) nav.push({ text: '◀ Назад', callback_data: `adm_notes_${orderId}_${page - 1}` });
  if ((page + 1) * LIMIT < totalCount)
    nav.push({ text: 'Вперёд ▶', callback_data: `adm_notes_${orderId}_${page + 1}` });
  if (nav.length) keyboard.push(nav);
  keyboard.push([
    { text: '📝 Добавить заметку', callback_data: `adm_note_${orderId}` },
    { text: '← К заявке', callback_data: `adm_order_${orderId}` },
  ]);

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: keyboard },
  });
}

// ─── Admin search orders ──────────────────────────────────────────────────────

async function showAdminSearchOrder(chatId) {
  if (!isAdmin(chatId)) return;
  await setSession(chatId, 'adm_search_order_input', {});
  return safeSend(chatId, `🔍 *Поиск заявки*\n\nВведите номер заявки, имя клиента или телефон:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_orders__0' }]] },
  });
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
      return safeSend(chatId, `🔍 По запросу *«${esc(q)}»* заявок не найдено\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '🔍 Искать снова', callback_data: 'adm_search_order' }],
            [{ text: '← Заявки', callback_data: 'adm_orders__0' }],
          ],
        },
      });
    }
    let text = `🔍 *Результаты поиска «${esc(q)}»*\n\nНайдено: ${rows.length}\n\n`;
    const btns = rows.map(o => {
      const icon = STATUS_LABELS[o.status]?.split(' ')[0] || '';
      text += `${icon} *${esc(o.order_number)}* — ${esc(o.client_name)}, ${esc(o.client_phone)}\n`;
      return [{ text: `${o.order_number}  ·  ${o.client_name}`, callback_data: `adm_order_${o.id}` }];
    });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...btns,
          [
            { text: '🔍 Новый поиск', callback_data: 'adm_search_order' },
            { text: '← Заявки', callback_data: 'adm_orders__0' },
          ],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] searchAdminOrders:', e.message);
  }
}

// ─── Admin order search by number (exact ID) ─────────────────────────────────

async function showAdminOrderSearch(chatId) {
  if (!isAdmin(chatId)) return;
  await setSession(chatId, 'adm_order_search_input', {});
  return safeSend(chatId, `🔍 *Поиск по номеру заявки*\n\nВведите номер заявки для поиска:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_orders__0' }]] },
  });
}

async function handleAdminOrderSearchInput(chatId, text) {
  if (!isAdmin(chatId)) return;
  await clearSession(chatId);
  const orderId = parseInt(text.trim(), 10);
  if (!orderId || orderId <= 0 || String(orderId) !== text.trim()) {
    return safeSend(chatId, `❌ Неверный номер заявки\\. Введите положительное целое число\\.`, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '🔍 Попробовать снова', callback_data: 'adm_order_search' }],
          [{ text: '← Заявки', callback_data: 'adm_orders__0' }],
        ],
      },
    });
  }
  const order = await get('SELECT id FROM orders WHERE id=?', [orderId]).catch(() => null);
  if (!order) {
    return safeSend(chatId, `❌ Заявка *\\#${esc(String(orderId))}* не найдена\\.`, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '🔍 Попробовать снова', callback_data: 'adm_order_search' }],
          [{ text: '← Заявки', callback_data: 'adm_orders__0' }],
        ],
      },
    });
  }
  return showAdminOrder(chatId, orderId);
}

// ─── Admin orders filter by model ─────────────────────────────────────────────

async function showAdminOrdersFilterModel(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const models = await query(
      `SELECT m.id, m.name, COUNT(o.id) as cnt
       FROM models m
       JOIN orders o ON o.model_id = m.id
       GROUP BY m.id, m.name
       ORDER BY cnt DESC
       LIMIT 10`
    );
    if (!models.length) {
      return safeSend(chatId, '📭 Заявок с привязкой к моделям не найдено\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← К заявкам', callback_data: 'adm_orders__0' }]] },
      });
    }
    const btns = models.map(m => [
      {
        text: `${esc(m.name)} (${m.cnt})`,
        callback_data: `adm_orders_model_${m.id}`,
      },
    ]);
    return safeSend(chatId, `🔽 *Фильтр по модели*\n\nВыберите модель:`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [...btns, [{ text: '← К заявкам', callback_data: 'adm_orders__0' }]] },
    });
  } catch (e) {
    console.error('[Bot] showAdminOrdersFilterModel:', e.message);
  }
}

async function showAdminOrdersByModel(chatId, modelId) {
  if (!isAdmin(chatId)) return;
  try {
    const model = await get('SELECT id, name FROM models WHERE id=?', [modelId]).catch(() => null);
    const orders = await query(
      `SELECT o.*, m.name as model_name,
        (SELECT COUNT(*) FROM order_notes WHERE order_id=o.id) as note_count
       FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE o.model_id=?
       ORDER BY o.created_at DESC LIMIT 20`,
      [modelId]
    );
    const modelName = model ? model.name : `#${modelId}`;
    if (!orders.length) {
      return safeSend(chatId, `📭 Заявок для модели *${esc(modelName)}* не найдено\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '← Фильтр по модели', callback_data: 'adm_orders_filter_model' }],
            [{ text: '← К заявкам', callback_data: 'adm_orders__0' }],
          ],
        },
      });
    }
    let text = `💃 *Заявки — ${esc(modelName)}* \\(${orders.length}\\)\n\n`;
    const btns = orders.map(o => {
      const icon = STATUS_LABELS[o.status]?.split(' ')[0] || '';
      const noteBadge = o.note_count > 0 ? ` \\(📝 ${esc(String(o.note_count))}\\)` : '';
      text += `${icon} *${esc(o.order_number)}* — ${esc(o.client_name)}${noteBadge}\n`;
      const noteLabel = o.note_count > 0 ? ` (📝 ${o.note_count})` : '';
      const row = [{ text: `${o.order_number}  ·  ${o.client_name}${noteLabel}`, callback_data: `adm_order_${o.id}` }];
      if (o.status === 'new') row.push({ text: '✅ Принять', callback_data: `adm_quick_confirm_${o.id}` });
      if (o.status === 'confirmed') row.push({ text: '🏁 Завершить', callback_data: `adm_quick_complete_${o.id}` });
      return row;
    });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...btns,
          [{ text: '← Фильтр по модели', callback_data: 'adm_orders_filter_model' }],
          [{ text: '← К заявкам', callback_data: 'adm_orders__0' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showAdminOrdersByModel:', e.message);
  }
}

// ─── Note search ──────────────────────────────────────────────────────────────

async function showAdminSearchNotes(chatId) {
  if (!isAdmin(chatId)) return;
  await setSession(chatId, 'adm_search_notes_input', {});
  return safeSend(chatId, `🔍 *Поиск по заметкам*\n\nВведите текст для поиска в заметках:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_orders__0' }]] },
  });
}

async function searchAdminNotes(chatId, searchText) {
  if (!isAdmin(chatId)) return;
  try {
    const q = searchText.trim();
    if (!q) {
      await clearSession(chatId);
      return showAdminSearchNotes(chatId);
    }
    const rows = await query(
      `SELECT n.id, n.admin_note, n.created_at, o.id as order_id, o.order_number, o.client_name
       FROM order_notes n
       JOIN orders o ON n.order_id = o.id
       WHERE n.admin_note LIKE ?
       ORDER BY n.created_at DESC LIMIT 15`,
      [`%${q}%`]
    );
    await clearSession(chatId);
    if (!rows.length) {
      return safeSend(chatId, `🔍 По запросу *«${esc(q)}»* заметок не найдено\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '🔍 Искать снова', callback_data: 'adm_search_notes' }],
            [{ text: '← Заявки', callback_data: 'adm_orders__0' }],
          ],
        },
      });
    }
    let text = `🔍 *Поиск по заметкам «${esc(q)}»*\nНайдено: ${esc(String(rows.length))}\n\n`;
    const btns = rows.map(n => {
      const dt = formatNoteDate(n.created_at);
      const preview = n.admin_note.length > 60 ? n.admin_note.slice(0, 60) + '…' : n.admin_note;
      text += `📋 *${esc(n.order_number)}* — ${esc(n.client_name)}\n_${esc(dt)}_\n${esc(preview)}\n\n`;
      return [{ text: `${n.order_number} · ${n.client_name}`, callback_data: `adm_order_${n.order_id}` }];
    });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...btns,
          [
            { text: '🔍 Новый поиск', callback_data: 'adm_search_notes' },
            { text: '← Заявки', callback_data: 'adm_orders__0' },
          ],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] searchAdminNotes:', e.message);
  }
}

// ─── Admin management ─────────────────────────────────────────────────────────

async function showAdminManagement(chatId) {
  if (!isAdmin(chatId)) return;
  const dbAdmins = await query('SELECT username, telegram_id, role FROM admins').catch(() => []);
  let text = `👑 *Управление администраторами*\n\n`;
  text += `*Из .env (ADMIN_TELEGRAM_IDS):*\n`;
  ADMIN_IDS.forEach(id => {
    text += `• \`${id}\`\n`;
  });
  text += `\n*В базе данных:*\n`;
  dbAdmins.forEach(a => {
    text += `• ${a.username} (\`${a.telegram_id || '—'}\`) — ${a.role}\n`;
  });
  text += `\n_Чтобы добавить admin — нажмите «Добавить Telegram ID»_`;
  return safeSend(chatId, text, {
    reply_markup: {
      inline_keyboard: [
        [{ text: '➕ Добавить Telegram ID', callback_data: 'adm_add_admin_id' }],
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ],
    },
  });
}

// ─── Managers List & Stats ────────────────────────────────────────────────────

async function showManagersList(chatId) {
  if (!isAdmin(chatId)) return;
  const admins = await query(
    `SELECT a.id, a.username, a.telegram_id, a.role,
            COUNT(o.id) as orders_count,
            COUNT(CASE WHEN o.status = 'completed' THEN 1 END) as completed_count
     FROM admins a
     LEFT JOIN orders o ON o.manager_id = a.id
     GROUP BY a.id
     ORDER BY a.role, a.username`,
    []
  );

  if (!admins.length) {
    return safeSend(chatId, '👥 *Менеджеры*\n\nНет менеджеров в системе\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] },
    });
  }

  let text = '👥 *Список менеджеров:*\n\n';
  const kb = [];

  for (const a of admins) {
    const roleLabel = a.role === 'superadmin' ? '👑' : a.role === 'manager' ? '👤' : '🔧';
    text += `${roleLabel} *${esc(a.username)}*`;
    if (a.telegram_id) text += ` \\(ID: ${esc(String(a.telegram_id))}\\)`;
    text += `\n📋 Заявок: ${a.orders_count || 0}, завершено: ${a.completed_count || 0}\n\n`;
    kb.push([{ text: `📊 Статистика: ${a.username}`, callback_data: `adm_mgr_stat_${a.id}` }]);
  }

  kb.push([{ text: '← Назад', callback_data: 'admin_menu' }]);

  return safeSend(chatId, text.trim(), { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: kb } });
}

async function showManagerStats(chatId, managerId) {
  if (!isAdmin(chatId)) return;
  const manager = await get('SELECT id, username, telegram_id, role FROM admins WHERE id=?', [managerId]);
  if (!manager) return safeSend(chatId, 'Менеджер не найден\\.', { parse_mode: 'MarkdownV2' });

  const stats = await get(
    `SELECT
       COUNT(*) as total_orders,
       COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
       COUNT(CASE WHEN status = 'cancelled' THEN 1 END) as cancelled,
       COUNT(CASE WHEN status IN ('new','confirmed') THEN 1 END) as active,
       COALESCE(AVG(CASE WHEN status = 'completed' AND CAST(budget AS REAL) > 0 THEN CAST(budget AS REAL) END), 0) as avg_check,
       COALESCE(SUM(CASE WHEN status = 'completed' THEN CAST(budget AS REAL) ELSE 0 END), 0) as total_revenue
     FROM orders WHERE manager_id = ?`,
    [managerId]
  );

  const roleLabel =
    manager.role === 'superadmin' ? '👑 Суперадмин' : manager.role === 'manager' ? '👤 Менеджер' : '🔧 Администратор';

  let text = `📊 *Статистика менеджера: ${esc(manager.username)}*\n\n`;
  text += `Роль: ${roleLabel}\n`;
  if (manager.telegram_id) text += `Telegram ID: ${esc(String(manager.telegram_id))}\n`;
  text += `\n📋 *Заявки:*\n`;
  text += `• Всего: ${stats?.total_orders || 0}\n`;
  text += `• Активных: ${stats?.active || 0}\n`;
  text += `• Завершено: ${stats?.completed || 0}\n`;
  text += `• Отменено: ${stats?.cancelled || 0}\n`;
  if (stats?.total_revenue > 0) {
    text += `\n💰 *Финансы:*\n`;
    text += `• Общая выручка: ${esc(String(Math.round(stats.total_revenue)))} руб\\.\n`;
    text += `• Средний чек: ${esc(String(Math.round(stats.avg_check)))} руб\\.\n`;
  }

  const kb = [[{ text: '← К списку', callback_data: 'adm_managers' }]];
  return safeSend(chatId, text, { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: kb } });
}

// ─── Export ───────────────────────────────────────────────────────────────────

async function showExportMenu(chatId) {
  if (!isAdmin(chatId)) return;
  return safeSend(chatId, `📥 *Экспорт данных*\n\nВыберите тип экспорта:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [
          { text: '📋 Заявки (CSV)', callback_data: 'adm_export_orders_csv' },
          { text: '💃 Модели (CSV)', callback_data: 'adm_export_models_csv' },
        ],
        [{ text: '👥 Клиенты (CSV)', callback_data: 'adm_export_clients_csv' }],
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ],
    },
  });
}

// Keep legacy alias for existing KB_MAIN_ADMIN button
async function exportOrders(chatId) {
  return showExportMenu(chatId);
}

async function showExportOrdersMenu(chatId) {
  if (!isAdmin(chatId)) return;
  return safeSend(chatId, `📋 *Экспорт заявок*\n\nВыберите период:`, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [
          { text: '📅 За сегодня', callback_data: 'adm_export_today' },
          { text: '📅 За неделю', callback_data: 'adm_export_week' },
        ],
        [
          { text: '📅 За месяц', callback_data: 'adm_export_month' },
          { text: '📋 Все заявки', callback_data: 'adm_export_all' },
        ],
        [{ text: '← Экспорт', callback_data: 'adm_export' }],
      ],
    },
  });
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
    const header = [
      'Номер',
      'Клиент',
      'Телефон',
      'Email',
      'Telegram',
      'Тип события',
      'Дата',
      'Длит(ч)',
      'Место',
      'Бюджет',
      'Комментарий',
      'Статус',
      'Создан',
      'Модель',
      'ID менеджера',
      'Первая заметка',
    ];
    const rows = orders.map(o =>
      [
        o.order_number,
        o.client_name,
        o.client_phone,
        o.client_email || '',
        o.client_telegram || '',
        o.event_type,
        o.event_date || '',
        o.event_duration || '',
        o.location || '',
        o.budget || '',
        (o.comments || '').replace(/"/g, '""'),
        o.status,
        new Date(o.created_at).toLocaleString('ru'),
        o.model_name || '',
        o.manager_id || '',
        (o.first_note || '').replace(/"/g, '""'),
      ]
        .map(v => `"${v}"`)
        .join(SEP)
    );
    const csv = [header.join(SEP), ...rows].join('\n');
    const buf = Buffer.from('﻿' + csv, 'utf8'); // UTF-8 BOM для Excel
    await bot.sendDocument(
      chatId,
      buf,
      {
        caption: `📤 Экспорт заявок (${periodLabel}) — ${orders.length} записей\n${new Date().toLocaleString('ru')}`,
      },
      { filename: `orders_${period}_${Date.now()}.csv`, contentType: 'text/csv' }
    );
  } catch (e) {
    return safeSend(chatId, `❌ Ошибка экспорта: ${e.message}`);
  }
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
    const header = [
      'ID',
      'Имя',
      'Возраст',
      'Рост',
      'Параметры',
      'Категория',
      'Instagram',
      'Доступна',
      'Топ',
      'Просмотры',
      'Заявок',
      'Создана',
    ];
    const rows = models.map(m =>
      [
        m.id,
        m.name || '',
        m.age || '',
        m.height || '',
        m.params || '',
        m.category || '',
        m.instagram || '',
        m.available ? 'Да' : 'Нет',
        m.featured ? 'Да' : 'Нет',
        m.view_count || 0,
        m.orders_count || 0,
        m.created_at ? new Date(m.created_at).toLocaleString('ru') : '',
      ]
        .map(v => `"${String(v).replace(/"/g, '""')}"`)
        .join(SEP)
    );
    const csv = [header.join(SEP), ...rows].join('\n');
    const buf = Buffer.from('﻿' + csv, 'utf8');
    await bot.sendDocument(
      chatId,
      buf,
      {
        caption: `💃 Экспорт моделей — ${models.length} записей\n${new Date().toLocaleString('ru')}`,
      },
      { filename: `models_${Date.now()}.csv`, contentType: 'text/csv' }
    );
  } catch (e) {
    return safeSend(chatId, `❌ Ошибка экспорта моделей: ${e.message}`);
  }
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
    const header = [
      'Chat ID',
      'Имя',
      'Телефон',
      'Email',
      'Telegram',
      'Всего заявок',
      'Завершено',
      'Отменено',
      'Последняя заявка',
    ];
    const rows = clients.map(c =>
      [
        c.chat_id || '',
        c.name || '',
        c.phone || '',
        c.email || '',
        c.telegram || '',
        c.total_orders || 0,
        c.completed || 0,
        c.cancelled || 0,
        c.last_order ? new Date(c.last_order).toLocaleString('ru') : '',
      ]
        .map(v => `"${String(v).replace(/"/g, '""')}"`)
        .join(SEP)
    );
    const csv = [header.join(SEP), ...rows].join('\n');
    const buf = Buffer.from('﻿' + csv, 'utf8');
    await bot.sendDocument(
      chatId,
      buf,
      {
        caption: `👥 Экспорт клиентов — ${clients.length} записей\n${new Date().toLocaleString('ru')}`,
      },
      { filename: `clients_${Date.now()}.csv`, contentType: 'text/csv' }
    );
  } catch (e) {
    return safeSend(chatId, `❌ Ошибка экспорта клиентов: ${e.message}`);
  }
}

// ─── Loyalty system ───────────────────────────────────────────────────────────

const LOYALTY_LEVELS = [
  { key: 'platinum', label: '💎 Платиновый', minEarned: 5000, discount: 15 },
  { key: 'gold', label: '🥇 Золотой', minEarned: 2000, discount: 10 },
  { key: 'silver', label: '🥈 Серебряный', minEarned: 500, discount: 5 },
  { key: 'bronze', label: '🥉 Бронзовый', minEarned: 0, discount: 0 },
];

function getLoyaltyLevel(totalEarned) {
  for (const lvl of LOYALTY_LEVELS) {
    if (totalEarned >= lvl.minEarned) return lvl;
  }
  return LOYALTY_LEVELS[LOYALTY_LEVELS.length - 1];
}

async function addLoyaltyPoints(chatId, points, type, description, orderId = null) {
  // Get previous state before update
  const prevLp = await get(`SELECT total_earned FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(() => null);
  const prevLevel = prevLp ? getLoyaltyLevel(prevLp.total_earned) : null;

  await run(
    `INSERT INTO loyalty_points (chat_id, points, total_earned) VALUES (?,?,?)
    ON CONFLICT(chat_id) DO UPDATE SET
      points = points + excluded.points,
      total_earned = total_earned + excluded.points,
      updated_at = CURRENT_TIMESTAMP`,
    [chatId, points, points]
  ).catch(() => {});
  await run(`INSERT INTO loyalty_transactions (chat_id, points, type, description, order_id) VALUES (?,?,?,?,?)`, [
    chatId,
    points,
    type,
    description,
    orderId,
  ]).catch(() => {});

  // Check for level-up notification
  if (points > 0) {
    const newLp = await get(`SELECT total_earned FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(() => null);
    if (newLp) {
      const newLevel = getLoyaltyLevel(newLp.total_earned);
      if (prevLevel && newLevel.key !== prevLevel.key && newLevel.minEarned > prevLevel.minEarned) {
        const discountText =
          newLevel.discount > 0 ? ` Теперь вам доступна скидка ${newLevel.discount}% на следующую заявку\\.` : '';
        await safeSend(chatId, `🎉 *Поздравляем\\!* Вы достигли уровня *${esc(newLevel.label)}*\\!${discountText}`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '💫 Мои баллы', callback_data: 'loyalty' }]] },
        }).catch(() => {});
      }
    }
  }
}

// ─── Achievements ─────────────────────────────────────────────────────────────

const ACHIEVEMENTS_LIST = [
  { key: 'first_order', icon: '🥇', title: 'Первая заявка', desc: 'Оформил первую успешную заявку' },
  { key: 'loyal_client', icon: '🔥', title: 'Постоянный клиент', desc: '3+ завершённых заявки' },
  { key: 'vip_client', icon: '💎', title: 'VIP клиент', desc: '10+ завершённых заявок' },
  { key: 'first_review', icon: '⭐', title: 'Критик', desc: 'Оставил первый отзыв' },
  { key: 'talkative', icon: '💬', title: 'Общительный', desc: 'Написал менеджеру более 5 раз' },
  { key: 'precise_choice', icon: '🎯', title: 'Точный выбор', desc: 'Забронировал без изменений даты' },
  { key: 'traveler', icon: '🌍', title: 'Путешественник', desc: 'Заявки из 2+ разных городов' },
];

async function grantAchievement(chatId, achievementKey) {
  try {
    const result = await run(`INSERT OR IGNORE INTO achievements (chat_id, achievement_key) VALUES (?,?)`, [
      chatId,
      achievementKey,
    ]);
    if (result.changes > 0) {
      const ach = ACHIEVEMENTS_LIST.find(a => a.key === achievementKey);
      if (ach) {
        await safeSend(chatId, `🏆 *Новое достижение\\!*\n\n${esc(ach.icon)} *${esc(ach.title)}*\n_${esc(ach.desc)}_`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '🏆 Мои достижения', callback_data: 'my_achievements' }]] },
        }).catch(() => {});
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
    ).catch(() => null);
    const cnt = completedOrders?.cnt || 0;
    if (cnt >= 1) await grantAchievement(chatId, 'first_order');
    if (cnt >= 3) await grantAchievement(chatId, 'loyal_client');
    if (cnt >= 10) await grantAchievement(chatId, 'vip_client');

    // Traveler achievement — orders from 2+ different cities
    const cities = await query(
      `SELECT DISTINCT location FROM orders WHERE client_chat_id=? AND location IS NOT NULL AND location != ''`,
      [String(chatId)]
    ).catch(() => []);
    if (cities.length >= 2) await grantAchievement(chatId, 'traveler');

    // Talkative — 5+ messages sent to manager
    const msgCount = await get(
      `SELECT COUNT(*) as cnt FROM messages m
       JOIN orders o ON o.id = m.order_id
       WHERE o.client_chat_id=? AND m.sender_type='client'`,
      [String(chatId)]
    ).catch(() => null);
    if ((msgCount?.cnt || 0) >= 5) await grantAchievement(chatId, 'talkative');
  } catch {}
}

async function showAchievements(chatId) {
  const earned = await query(
    `SELECT achievement_key, achieved_at FROM achievements WHERE chat_id=? ORDER BY achieved_at ASC`,
    [chatId]
  ).catch(() => []);

  const earnedKeys = new Set(earned.map(a => a.achievement_key));
  const earnedMap = Object.fromEntries(earned.map(a => [a.achievement_key, a.achieved_at]));

  let text = `🏆 *Мои достижения*\n\n`;
  text += `Получено: *${earnedKeys.size}* из *${ACHIEVEMENTS_LIST.length}*\n\n`;

  for (const ach of ACHIEVEMENTS_LIST) {
    if (earnedKeys.has(ach.key)) {
      const dt = earnedMap[ach.key] ? new Date(earnedMap[ach.key]).toLocaleDateString('ru') : '';
      text += `${esc(ach.icon)} *${esc(ach.title)}* ✅\n_${esc(ach.desc)}_${dt ? `\n📅 ${esc(dt)}` : ''}\n\n`;
    } else {
      text += `🔒 *${esc(ach.title)}*\n_${esc(ach.desc)}_\n\n`;
    }
  }

  return safeSend(chatId, text.trim(), {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '💫 Мои баллы', callback_data: 'loyalty' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ],
    },
  });
}

// ─── Loyalty Leaderboard ──────────────────────────────────────────────────────

async function showLoyaltyLeaderboard(chatId) {
  const top = await query(
    `SELECT lp.chat_id, lp.points, lp.total_earned,
            (SELECT o.client_name FROM orders o WHERE o.client_chat_id=CAST(lp.chat_id AS TEXT) ORDER BY o.created_at DESC LIMIT 1) as client_name
     FROM loyalty_points lp
     ORDER BY lp.points DESC LIMIT 10`
  ).catch(() => []);

  const myRankRow = await get(
    `SELECT COUNT(*) as pos FROM loyalty_points WHERE points > (SELECT COALESCE(points,0) FROM loyalty_points WHERE chat_id=?)`,
    [chatId]
  ).catch(() => null);
  const myPos = (myRankRow?.pos ?? 0) + 1;
  const myLp = await get(`SELECT points FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(() => null);

  function maskName(name) {
    if (!name) return 'Клиент';
    const parts = name.trim().split(/\s+/);
    if (parts.length === 1) return parts[0][0] + '***';
    return parts[0][0] + '***' + parts[parts.length - 1][0] + '\\.';
  }

  const medals = ['🥇', '🥈', '🥉'];
  let text = `🏆 *Топ клиентов по баллам*\n\n`;

  top.forEach((row, i) => {
    const medal = medals[i] || `${i + 1}\\.`;
    const masked = esc(maskName(row.client_name));
    const isMe = String(row.chat_id) === String(chatId);
    const meTag = isMe ? ' \\(вы\\)' : '';
    text += `${medal} ${masked}${meTag} — *${row.points} баллов*\n`;
  });

  text += `\n📌 Ваша позиция: *${myPos}*`;
  if (myLp) text += ` — *${myLp.points} баллов*`;

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '💫 Мои баллы', callback_data: 'loyalty' }],
        [{ text: '🏆 Мои достижения', callback_data: 'my_achievements' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ],
    },
  });
}

async function showLoyaltyProfile(chatId) {
  const lp = await get(`SELECT * FROM loyalty_points WHERE chat_id=?`, [chatId]);
  if (!lp) {
    return safeSend(chatId, '💫 У вас пока нет баллов лояльности\\.\n\nЗаработайте баллы, оформив первую заявку\\!', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] },
    });
  }
  const level = getLoyaltyLevel(lp.total_earned);

  const transactions = await query(
    `SELECT * FROM loyalty_transactions WHERE chat_id=? ORDER BY created_at DESC LIMIT 5`,
    [chatId]
  );
  const txText =
    transactions.map(t => `${t.points > 0 ? '\\+' : ''}${esc(String(t.points))} — ${esc(t.description)}`).join('\n') ||
    'Нет операций';

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
    txText,
  ]
    .filter(l => l !== '')
    .join('\n');

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '🏆 Мои достижения', callback_data: 'my_achievements' }],
        [{ text: '🏆 Топ клиентов', callback_data: 'loyalty_leaderboard' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ],
    },
  });
}

// ─── Referral Program ─────────────────────────────────────────────────────────

async function showReferralProgram(chatId) {
  try {
    const refCode = String(chatId);
    const botInfo = await bot.getMe();
    const refLink = `https://t.me/${botInfo.username}?start=ref${refCode}`;

    const refs = await query(`SELECT COUNT(*) as cnt FROM referrals WHERE referrer_chat_id=?`, [chatId]).catch(() => [
      { cnt: 0 },
    ]);
    const refCount = refs[0]?.cnt || 0;
    const points = (await get(`SELECT points FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(() => null)) || {
      points: 0,
    };

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
      `_Если реферал создаёт первую заявку \\— вам дополнительно 300 баллов\\._`,
    ].join('\n');

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [
            {
              text: '📋 Поделиться ссылкой',
              switch_inline_query: `Используй мою ссылку для записи модели: ${refLink}`,
            },
          ],
          [{ text: '💫 Мои баллы', callback_data: 'loyalty' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showReferralProgram:', e.message);
  }
}

// ─── Price Calculator ──────────────────────────────────────────────────────────

// Default pricing rates (can be overridden via bot_settings)
const DEFAULT_RATES = {
  base_per_hour: 10000, // per model per hour
  type_multipliers: {
    fashion_show: 1.5,
    photo_shoot: 1.2,
    event: 1.0,
    commercial: 1.4,
    runway: 1.3,
    other: 1.0,
  },
  organization_fee: 15000, // flat organization fee
};

// Tier multipliers: Эконом / Стандарт / Премиум
const CALC_TIERS = {
  econ: { label: 'Эконом', mult: 0.8 },
  standard: { label: 'Стандарт', mult: 1.0 },
  premium: { label: 'Премиум', mult: 1.35 },
};

async function getCalcRates() {
  try {
    const [bph, orgFee] = await Promise.all([
      getSetting('calc_base_per_hour').catch(() => null),
      getSetting('calc_organization_fee').catch(() => null),
    ]);
    return {
      base_per_hour: bph ? parseInt(bph) : DEFAULT_RATES.base_per_hour,
      organization_fee: orgFee ? parseInt(orgFee) : DEFAULT_RATES.organization_fee,
      type_multipliers: DEFAULT_RATES.type_multipliers,
    };
  } catch {
    return DEFAULT_RATES;
  }
}

async function showPriceCalculator(chatId, params = {}) {
  const { models = 1, hours = 4, eventType = 'other' } = params;

  // Use EVENT_TYPES labels (from constants) with fallback for calc-specific display
  const calcEventLabels = {
    fashion_show: 'Показ мод',
    photo_shoot: 'Фотосессия',
    event: 'Корпоратив',
    commercial: 'Коммерческая съёмка',
    runway: 'Подиум',
    other: 'Другое',
  };

  const rates = await getCalcRates();
  const typeMult = rates.type_multipliers[eventType] ?? 1.0;

  // Base model cost (before tier)
  const modelCost = rates.base_per_hour * models * hours * typeMult;
  const orgFee = rates.organization_fee;

  // Three tiers
  const tiers = Object.entries(CALC_TIERS).map(([key, tier]) => {
    const mc = Math.round((modelCost * tier.mult) / 1000) * 1000;
    const total = mc + orgFee;
    return { key, label: tier.label, modelCost: mc, total };
  });

  const econTotal = tiers[0].total;
  const premiumTotal = tiers[2].total;

  function fmt(n) {
    return n.toLocaleString('ru-RU');
  }

  const breakdownLines = tiers.map(
    t => `  *${esc(t.label)}*: ${esc(fmt(t.modelCost))} \\+ ${esc(fmt(orgFee))} \\= ${esc(fmt(t.total))} ₽`
  );

  const text = [
    `🧮 *Калькулятор стоимости*`,
    ``,
    `📌 Тип: *${esc(calcEventLabels[eventType] || eventType)}*   👤 Моделей: *${models}*   ⏱ Часов: *${hours}*`,
    ``,
    `💰 *От ${esc(fmt(econTotal))} до ${esc(fmt(premiumTotal))} ₽*`,
    ``,
    `📊 *Разбивка по уровням:*`,
    `_Модели \\(${models} × ${hours} ч\\) \\+ организация ${esc(fmt(orgFee))} ₽_`,
    ...breakdownLines,
    ``,
    `_Цена ориентировочная\\. Точная стоимость согласуется с менеджером\\._`,
  ].join('\n');

  const modelsButtons = [1, 2, 3, 5].map(n => ({
    text: models === n ? `✓ ${n}` : String(n),
    callback_data: `calc_models_${n}_${hours}_${eventType}`,
  }));
  const hoursButtons = [2, 4, 8, 16].map(h => ({
    text: hours === h ? `✓ ${h}ч` : `${h}ч`,
    callback_data: `calc_hours_${models}_${h}_${eventType}`,
  }));
  const typeEntries = Object.entries(calcEventLabels);
  const typeButtons = typeEntries.slice(0, 3).map(([key, label]) => ({
    text: eventType === key ? `✓ ${label}` : label,
    callback_data: `calc_type_${models}_${hours}_${key}`,
  }));
  const typeButtons2 = typeEntries.slice(3).map(([key, label]) => ({
    text: eventType === key ? `✓ ${label}` : label,
    callback_data: `calc_type_${models}_${hours}_${key}`,
  }));

  // Pre-fill booking: map calc event type to booking EVENT_TYPES key
  const bookingEtype = Object.keys(EVENT_TYPES).includes(eventType) ? eventType : 'other';
  const bookingDur = String(Math.min(hours, 12));

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [{ text: '👤 Кол-во моделей:', callback_data: 'noop' }],
        modelsButtons,
        [{ text: '⏱ Часов:', callback_data: 'noop' }],
        hoursButtons,
        [{ text: '📌 Тип события:', callback_data: 'noop' }],
        typeButtons,
        ...(typeButtons2.length ? [typeButtons2] : []),
        [
          { text: '📋 Оставить заявку', callback_data: `calc_book_${bookingEtype}_${bookingDur}` },
          { text: '🔄 Пересчитать', callback_data: 'calculator' },
        ],
        [{ text: '💬 Уточнить у менеджера', callback_data: 'msg_manager_start' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ],
    },
  });
}

// ─── Order Timeline ────────────────────────────────────────────────────────────

async function showOrderTimeline(order) {
  const statuses = ['new', 'reviewing', 'confirmed', 'in_progress', 'completed'];
  const statusEmoji = {
    new: '🆕',
    reviewing: '🔍',
    confirmed: '✅',
    in_progress: '🔄',
    completed: '🏁',
    cancelled: '❌',
  };
  const statusName = {
    new: 'Новая',
    reviewing: 'На рассмотрении',
    confirmed: 'Подтверждена',
    in_progress: 'В работе',
    completed: 'Завершена',
    cancelled: 'Отменена',
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
      result = await run(
        "UPDATE orders SET status='confirmed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",
        [orderId]
      );
    } else if (newStatus === 'reviewing') {
      result = await run(
        "UPDATE orders SET status='reviewing',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",
        [orderId]
      );
    } else if (newStatus === 'cancelled') {
      result = await run(
        "UPDATE orders SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('completed','cancelled')",
        [orderId]
      );
    } else if (newStatus === 'completed') {
      result = await run(
        "UPDATE orders SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status!='cancelled'",
        [orderId]
      );
    }

    if (!result || result.changes === 0) return safeSend(chatId, '⚠️ Заявка уже обработана.');

    // Log status change to history
    await run('INSERT INTO order_status_history (order_id, old_status, new_status, changed_by) VALUES (?,?,?,?)', [
      orderId,
      oldStatus,
      newStatus,
      String(chatId),
    ]).catch(e => console.warn('[Bot] history log:', e.message));

    // Audit log
    await logAdminAction(chatId, 'change_order_status', 'order', orderId, { from: oldStatus, to: newStatus });

    const order = await get('SELECT * FROM orders WHERE id=?', [orderId]);
    if (order?.client_chat_id)
      notifyStatusChange(order.client_chat_id, order.order_number, newStatus, order.client_phone || null);

    // Send custom booking confirmation message from settings
    if (newStatus === 'confirmed' && order?.client_chat_id) {
      const confirmMsg = await getSetting('booking_confirm_msg');
      if (confirmMsg) {
        await bot.sendMessage(order.client_chat_id, esc(confirmMsg), { parse_mode: 'MarkdownV2' }).catch(() => {});
      }
    }

    // Award loyalty points on order completion
    if (newStatus === 'completed' && order?.client_chat_id) {
      await addLoyaltyPoints(order.client_chat_id, 100, 'order_complete', 'Завершена заявка #' + orderId, orderId);

      // Referral first-order bonus: if this client was referred, give 300 extra points to referrer
      const refRow = await get(`SELECT referrer_chat_id FROM referrals WHERE referred_chat_id=?`, [
        order.client_chat_id,
      ]).catch(() => null);
      if (refRow) {
        // Only give first-order bonus once (check if referrer already got it for this client)
        const alreadyGiven = await get(
          `SELECT id FROM loyalty_transactions WHERE chat_id=? AND type='referral_first_order' AND description LIKE ?`,
          [refRow.referrer_chat_id, `%${order.client_chat_id}%`]
        ).catch(() => null);
        if (!alreadyGiven) {
          await addLoyaltyPoints(
            refRow.referrer_chat_id,
            300,
            'referral_first_order',
            `Реферал ${order.client_chat_id} создал первую заявку`
          ).catch(() => {});
          await safeSend(refRow.referrer_chat_id, `👥 Ваш реферал создал заявку\\! *\\+300 бонусов* зачислено\\.`, {
            parse_mode: 'MarkdownV2',
          }).catch(() => {});
        }
      }

      // Check and grant achievements for this client
      await checkAndGrantAchievements(order.client_chat_id).catch(() => {});
    }

    // When completing a confirmed order, ask about payment status
    if (newStatus === 'completed' && oldStatus === 'confirmed') {
      try {
        await safeSend(
          chatId,
          `✅ Заявка №${esc(order?.order_number || String(orderId))} завершена\\.\n\n*Получена ли оплата?*`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [
                  { text: '✅ Да, оплачено', callback_data: `adm_paid_${orderId}` },
                  { text: '⏳ Ждём оплаты', callback_data: `adm_await_pay_${orderId}` },
                ],
              ],
            },
          }
        );
        return;
      } catch {}
    }

    return showAdminOrder(chatId, orderId);
  } catch (e) {
    console.error('[Bot] adminChangeStatus:', e.message);
  }
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
    const path = '/api/tg-webhook';
    const full = WEBHOOK_URL.replace(/\/$/, '') + path;
    bot
      .setWebHook(full, { secret_token: WEBHOOK_SECRET })
      .then(() => console.log(`🤖 Bot (webhook: ${full})`))
      .catch(e => console.error('[Bot] setWebHook:', e.message));
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
      const code = err.code || err.response?.statusCode || 'UNKNOWN';
      console.error(`[Bot] Polling error (${code}): ${err.message}`);
    });
  }

  // Регистрация команд в меню "/" Telegram
  bot
    .setMyCommands([
      { command: 'start', description: '🏠 Главное меню' },
      { command: 'catalog', description: '💃 Каталог моделей' },
      { command: 'booking', description: '📋 Оформить заявку' },
      { command: 'orders', description: '📂 Мои заявки' },
      { command: 'profile', description: '👤 Мой профиль' },
      { command: 'wishlist', description: '❤️ Избранные модели' },
      { command: 'calculator', description: '🧮 Калькулятор стоимости' },
      { command: 'reviews', description: '⭐ Отзывы' },
      { command: 'faq', description: '❓ Частые вопросы' },
      { command: 'help', description: '🆘 Помощь' },
      { command: 'cancel', description: '❌ Отменить действие' },
    ])
    .catch(e => console.warn('[Bot] setMyCommands:', e.message));

  // ── Factory health check at startup ────────────────────────────────────────
  setTimeout(async () => {
    try {
      const resp = await fetch('http://localhost:5500/api/health');
      const health = await resp.json();
      if (health.factory && health.factory.status !== 'ok') {
        const admins = await getAdminChatIds().catch(() => [...ADMIN_IDS]);
        for (const adminId of admins) {
          await safeSend(adminId, '⚠️ *Factory health warning*: ' + esc(health.factory.message || 'Unknown'), {
            parse_mode: 'MarkdownV2',
          });
        }
      }
    } catch (e) {
      /* factory may not be running — ignore */
    }
  }, 5000);

  // ── /start ─────────────────────────────────────────────────────────────────
  bot.onText(/\/start(.*)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const firstName = msg.from.first_name;
    await setSession(chatId, 'idle', {});

    // Deep-link: /start model_NNN  — прямая ссылка на карточку модели
    const ref = match[1]?.trim();
    if (ref) {
      const modelMatch = ref.match(/^model_(\d+)$/);
      if (modelMatch) {
        const modelId = parseInt(modelMatch[1]);
        const m = await get('SELECT id FROM models WHERE id=? AND available=1', [modelId]).catch(() => null);
        if (m) return showModel(chatId, modelId);
      }
      // Deep-link: /start booking_NNN — начать бронирование модели NNN
      const bookingDeepMatch = ref.match(/^booking_(\d+)$/);
      if (bookingDeepMatch) {
        const modelId = parseInt(bookingDeepMatch[1]);
        const bm = await get('SELECT id,name FROM models WHERE id=? AND available=1', [modelId]).catch(() => null);
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
            await run(`INSERT INTO referrals (referrer_chat_id, referred_chat_id) VALUES (?,?)`, [
              referrerId,
              chatId,
            ]).catch(() => {});
            await addLoyaltyPoints(referrerId, 500, 'referral', `Приглашён новый пользователь`).catch(() => {});
            await addLoyaltyPoints(chatId, 200, 'referral_welcome', `Приветственный бонус по реферальной ссылке`).catch(
              () => {}
            );
            await bot
              .sendMessage(
                referrerId,
                `👥 По вашей реферальной ссылке зарегистрировался новый пользователь\\! *\\+500 баллов* зачислено\\.`,
                { parse_mode: 'MarkdownV2' }
              )
              .catch(() => {});
          }
        }
        // Fall through to show main menu
      }
      // Deep-link: /start ORDER_NUMBER
      const order = await get('SELECT * FROM orders WHERE order_number=?', [ref]).catch(() => null);
      if (order) {
        if (order.client_chat_id && order.client_chat_id !== String(chatId))
          return safeSend(chatId, '❌ Эта заявка уже привязана к другому чату.');
        await run('UPDATE orders SET client_chat_id=? WHERE order_number=?', [String(chatId), ref]);
        return safeSend(
          chatId,
          `✅ Заявка *${esc(ref)}* привязана к вашему чату\\!\n\nВы будете получать уведомления о статусе\\.`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [{ text: '📋 Статус заявки', callback_data: `client_order_${order.id}` }],
                [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
              ],
            },
          }
        );
      }
    }

    if (isAdmin(chatId)) return showAdminMenu(chatId, firstName);
    await showMainMenu(chatId, firstName);

    // Welcome follow-up for new clients (no orders yet)
    const hasOrders = await get('SELECT id FROM orders WHERE client_chat_id=? LIMIT 1', [String(chatId)]).catch(
      () => null
    );
    if (!hasOrders && !isAdmin(chatId)) {
      // Notify admins about new user
      const username = msg.from.username ? `@${msg.from.username}` : firstName || String(chatId);
      const adminIds = await getAdminChatIds().catch(() => [...ADMIN_IDS]);
      for (const adminId of adminIds) {
        safeSend(adminId, `👤 Новый пользователь: ${esc(username)} открыл бота\\.`, { parse_mode: 'MarkdownV2' }).catch(
          () => {}
        );
      }

      // Schedule welcome follow-up hint in 1 hour
      setTimeout(
        async () => {
          try {
            const stillNew = await get('SELECT id FROM orders WHERE client_chat_id=? LIMIT 1', [String(chatId)]).catch(
              () => null
            );
            if (!stillNew) {
              await bot.sendMessage(
                chatId,
                `💡 *Подсказка*: Нажмите *Каталог* чтобы посмотреть наших моделей, или воспользуйтесь *Калькулятором* чтобы оценить стоимость вашего события\\.`,
                {
                  parse_mode: 'MarkdownV2',
                  reply_markup: {
                    inline_keyboard: [
                      [
                        { text: '💃 Каталог', callback_data: 'cat_cat__0' },
                        { text: '🧮 Калькулятор', callback_data: 'calculator' },
                      ],
                    ],
                  },
                }
              );
            }
          } catch {}
        },
        60 * 60 * 1000
      ); // 1 hour
    }
  });

  // ── /admin ─────────────────────────────────────────────────────────────────
  bot.onText(/\/admin/, async msg => {
    if (!isAdmin(msg.chat.id)) return;
    return showAdminMenu(msg.chat.id, msg.from.first_name);
  });

  // ── /cancel ────────────────────────────────────────────────────────────────
  bot.onText(/\/cancel/, async msg => {
    const chatId = msg.chat.id;
    // Check if there is an active state before clearing
    const sess = await getSession(chatId);
    const hadActiveState = isActiveInputState(sess?.state);

    // Clear session timeout if any active flow was running
    clearTimeout(sessionTimers.get(chatId));
    sessionTimers.delete(chatId);
    clearSessionWarning(chatId);
    clearSessionReminder(chatId);
    // Clear any active state/flow
    await clearSession(chatId);

    const cancelText = hadActiveState
      ? '❌ Действие отменено\\. Возвращаю вас в главное меню\\.'
      : 'ℹ️ Активного действия нет\\. Вы уже в главном меню\\.';

    await safeSend(chatId, cancelText, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
    });

    if (isAdmin(chatId)) {
      return showAdminMenu(chatId, msg.from.first_name);
    }
    return showMainMenu(chatId, msg.from.first_name);
  });

  // ── /status ────────────────────────────────────────────────────────────────
  bot.onText(/\/status (.+)/, async (msg, match) => {
    await showOrderStatus(msg.chat.id, match[1].trim());
  });

  // ── /help ──────────────────────────────────────────────────────────────────
  bot.onText(/\/help/, async msg => {
    const chatId = msg.chat.id;
    // Get manager contact from settings for client help text
    const managerContact =
      (await getSetting('manager_contact').catch(() => null)) ||
      (await getSetting('contacts_phone').catch(() => null)) ||
      '@manager';
    const managerLine = managerContact ? `\n\nПо вопросам: ${esc(managerContact)}` : '';
    const text = isAdmin(chatId)
      ? `📖 *Команды администратора:*\n\n` +
        `/start — главное меню\n` +
        `/cancel — отменить текущее действие\n` +
        `/help — эта справка\n` +
        `/admin — панель управления\n\n` +
        `*Подсказка:* если бот завис во время ввода — напишите /cancel, чтобы сбросить состояние и вернуться в меню\\.`
      : `📖 *Справка по боту Nevesty Models*\n\n` +
        `*Основные команды:*\n` +
        `/start — главное меню\n` +
        `/catalog — каталог моделей\n` +
        `/booking — создать заявку на модель\n` +
        `/orders — мои заявки\n` +
        `/profile — мой профиль и баланс\n` +
        `/wishlist — избранные модели\n` +
        `/status — проверить статус заявки\n` +
        `/faq — часто задаваемые вопросы\n` +
        `/cancel — отменить текущее действие\n` +
        `/help — эта справка\n\n` +
        `*Быстрые действия:*\n` +
        `💃 Выбрать модель → Каталог → нажмите на модель\n` +
        `📝 Оформить заявку → кнопка «Оформить заявку»\n` +
        `📋 Следить за заявкой → Мои заявки\n` +
        `❤️ Сохранить модель → кнопка «В избранное»\n\n` +
        `*Что\\-то пошло не так?*\n` +
        `Напишите /cancel чтобы сбросить состояние, или /start чтобы начать заново\\.` +
        managerLine;
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
    });
  });

  // ── /faq ───────────────────────────────────────────────────────────────────
  bot.onText(/\/faq/, async msg => {
    return showFaq(msg.chat.id);
  });

  // ── /profile ───────────────────────────────────────────────────────────────
  bot.onText(/\/profile/, async msg => {
    const chatId = msg.chat.id;
    const firstName = msg.from.first_name;
    return showUserProfile(chatId, firstName);
  });

  // ── /catalog ───────────────────────────────────────────────────────────────
  bot.onText(/\/catalog/, async msg => {
    return showCatalog(msg.chat.id, null, 0);
  });

  // ── /booking ───────────────────────────────────────────────────────────────
  bot.onText(/\/booking/, async msg => {
    return bkStep1(msg.chat.id);
  });

  // ── /orders ────────────────────────────────────────────────────────────────
  bot.onText(/\/orders/, async msg => {
    return showMyOrders(msg.chat.id);
  });

  // ── /myorders (alias for /orders) ──────────────────────────────────────────
  bot.onText(/\/myorders/, async msg => {
    return showMyOrders(msg.chat.id);
  });

  // ── /contacts ──────────────────────────────────────────────────────────────
  bot.onText(/\/contacts/, async msg => {
    return showContacts(msg.chat.id);
  });

  // ── /wishlist ──────────────────────────────────────────────────────────────
  bot.onText(/^\/wishlist/, async msg => {
    return showWishlist(msg.chat.id, 0);
  });

  // ── /calculator ────────────────────────────────────────────────────────────
  bot.onText(/^\/calculator/, async msg => {
    return showPriceCalculator(msg.chat.id);
  });

  // ── /reviews ───────────────────────────────────────────────────────────────
  bot.onText(/^\/reviews/, async msg => {
    return showPublicReviews(msg.chat.id, 0);
  });

  // ── /achievements ──────────────────────────────────────────────────────────
  bot.onText(/^\/achievements/, async msg => {
    return showAchievements(msg.chat.id);
  });

  // ── /referral ──────────────────────────────────────────────────────────────
  bot.onText(/^\/referral/, async msg => {
    return showReferralProgram(msg.chat.id);
  });

  // ── /register_model — link Telegram account to model by phone ────────────────
  bot.onText(/^\/register_model/, async msg => {
    const chatId = msg.chat.id;
    if (isAdmin(chatId)) {
      return safeSend(chatId, '⚠️ Эта команда предназначена для моделей, а не для администраторов\\.', {
        parse_mode: 'MarkdownV2',
      });
    }
    await setSession(chatId, 'model_reg_phone', {});
    return safeSend(
      chatId,
      '📱 *Регистрация модели*\n\nВведите ваш номер телефона в формате \\+7XXXXXXXXXX или 8XXXXXXXXXX:\n\n_Номер должен совпадать с тем, который зарегистрирован в базе агентства\\._ ',
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] },
      }
    );
  });

  // ── /factory_content (admin: view & send AI-generated posts) ─────────────────
  bot.onText(/^\/factory_content/, async msg => {
    if (!isAdmin(msg.chat.id)) return;
    return showFactoryContent(msg.chat.id);
  });

  // ── /msg (admin direct reply) ──────────────────────────────────────────────
  bot.onText(/\/msg (\S+) (.+)/, async (msg, match) => {
    if (!isAdmin(msg.chat.id)) return;
    const chatId = msg.chat.id;
    const orderNum = match[1].trim().toUpperCase();
    const text = match[2].trim();
    const order = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]).catch(() => null);
    if (!order) return safeSend(chatId, `❌ Заявка *${esc(orderNum)}* не найдена.`, {});
    const admin = await get('SELECT username FROM admins WHERE telegram_id=?', [String(chatId)]).catch(() => null);
    await run('INSERT INTO messages (order_id,sender_type,sender_name,content) VALUES (?,?,?,?)', [
      order.id,
      'admin',
      admin?.username || 'Менеджер',
      text,
    ]);
    if (order.client_chat_id) {
      await sendMessageToClient(order.client_chat_id, order.order_number, text);
      return safeSend(chatId, `✅ Отправлено клиенту ${order.client_name}.`);
    }
    return safeSend(chatId, `⚠️ Сообщение сохранено, но клиент ещё не подключил бот.`);
  });

  // ── Callback query router ──────────────────────────────────────────────────
  bot.on('callback_query', async q => {
    const chatId = q.message.chat.id;
    const data = q.data;
    try {
      await bot.answerCallbackQuery(q.id);
    } catch {}

    // ── Navigation
    if (data === 'main_menu')
      return isAdmin(chatId) ? showAdminMenu(chatId, q.from.first_name) : showMainMenu(chatId, q.from.first_name);
    if (data === 'admin_menu') {
      if (!isAdmin(chatId)) return;
      return showAdminMenu(chatId, q.from.first_name);
    }
    if (data === 'contacts') return showContacts(chatId);
    if (data === 'faq') return showFaq(chatId);
    if (data === 'about_us') return showAboutUs(chatId);
    if (data === 'pricing') return showPricing(chatId);
    if (data === 'show_pricing') {
      const pricingText = await getSetting('pricing_text').catch(() => '');
      const siteUrl =
        (await getSetting('site_url').catch(() => 'https://nevesty-models.ru')) || 'https://nevesty-models.ru';
      const msg = pricingText
        ? esc(pricingText)
        : `💰 *Стоимость услуг*\n\nПодробный прайс\\-лист доступен на сайте:\n[Смотреть цены](${esc(siteUrl + '/pricing.html')})`;
      return safeSend(chatId, msg, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '🌐 Открыть прайс', url: siteUrl + '/pricing.html' }],
            [{ text: '← Меню', callback_data: 'main_menu' }],
          ],
        },
      });
    }
    if (data === 'profile') return showUserProfile(chatId, q.from.first_name);
    if (data === 'loyalty') return showLoyaltyProfile(chatId);
    if (data === 'my_achievements') return showAchievements(chatId);
    if (data === 'loyalty_leaderboard') return showLoyaltyLeaderboard(chatId);
    if (data === 'referral') return showReferralProgram(chatId);
    if (data === 'calculator') return showPriceCalculator(chatId);
    if (data === 'noop') return; // label-only buttons
    if (data === 'my_orders') return showMyOrders(chatId);
    if (data === 'check_status') return showStatusInput(chatId);
    if (data === 'adm_stats' || data === 'adm_stats_refresh') {
      if (!isAdmin(chatId)) {
        await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(() => {});
        return;
      }
      return showAdminStats(chatId);
    }
    if (data === 'adm_stats_csv') {
      if (!isAdmin(chatId)) {
        await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(() => {});
        return;
      }
      const csvUrl = `${SITE_URL.replace(/\/$/, '')}/api/admin/orders/export?format=csv`;
      return safeSend(chatId, esc(`📎 Ссылка на экспорт заявок (CSV):\n${csvUrl}`), { parse_mode: 'MarkdownV2' });
    }
    if (data === 'adm_organism') {
      if (!isAdmin(chatId)) {
        await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(() => {});
        return;
      }
      return showOrganismStatus(chatId);
    }
    if (data === 'adm_run_organism') {
      if (!isAdmin(chatId)) return;
      await safeSend(chatId, '🌿 Запускаю проверку организма...\n\nРезультаты придут через 1-2 минуты.', {
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_organism' }]] },
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
      await safeSend(
        chatId,
        '🔧 *Запускаю авто-исправление и перепроверку*\n\n' +
          'Агенты:\n' +
          '1\\. 🔧 Auto Fixer — исправляет базовые проблемы\n' +
          '2\\. 🐛 Bug Hunter — проверяет код\n' +
          '3\\. 🧠 Orchestrator — полная перепроверка всех 25 агентов\n\n' +
          '_Результаты придут в чат через 2-3 минуты_',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_organism' }]] },
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
      const page = parseInt(parts.pop()) || 0;
      const cat = parts.join('_');
      return showCatalog(chatId, cat, page);
    }

    // ── Model detail (client)
    if (data.startsWith('cat_model_')) {
      const id = parseInt(data.replace('cat_model_', ''));
      return showModel(chatId, id);
    }

    // ── Client order detail
    if (data.startsWith('client_order_')) {
      const id = parseInt(data.replace('client_order_', ''));
      return showClientOrder(chatId, id);
    }
    if (data.startsWith('my_order_')) {
      const id = parseInt(data.replace('my_order_', ''));
      await bot.answerCallbackQuery(q.id).catch(() => {});
      return showClientOrder(chatId, id);
    }

    // ── Pay order
    if (data.startsWith('pay_order_')) {
      const orderId = parseInt(data.replace('pay_order_', ''));
      const ord = await get('SELECT * FROM orders WHERE id=?', [orderId]).catch(() => null);
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
            {
              hostname: url.hostname,
              port: url.port || 443,
              path: url.pathname,
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(bodyStr) },
            },
            res => {
              const ch = [];
              res.on('data', d => ch.push(d));
              res.on('end', () => resolve({ status: res.statusCode, data: JSON.parse(Buffer.concat(ch).toString()) }));
            }
          );
          req.on('error', reject);
          req.write(bodyStr);
          req.end();
        });
        if (resp.data.error) {
          return safeSend(chatId, `❌ ${esc(resp.data.error)}`, { parse_mode: 'MarkdownV2' });
        }
        if (resp.data.payment_url) {
          return safeSend(
            chatId,
            `💳 *Оплата заявки ${esc(ord.order_number)}*\n\nНажмите кнопку ниже для перехода к оплате:`,
            {
              parse_mode: 'MarkdownV2',
              reply_markup: {
                inline_keyboard: [
                  [{ text: '💳 Перейти к оплате', url: resp.data.payment_url }],
                  [{ text: '← Назад к заявке', callback_data: `client_order_${orderId}` }],
                ],
              },
            }
          );
        } else {
          // Stripe: no hosted URL, show client_secret info
          return safeSend(
            chatId,
            `💳 *Оплата инициирована*\n\nID платежа: \`${esc(resp.data.payment_id || '')}\`\n\nОбратитесь к менеджеру для завершения оплаты\\.`,
            { parse_mode: 'MarkdownV2' }
          );
        }
      } catch (e) {
        console.error('[Bot] pay_order:', e.message);
        return safeSend(chatId, '❌ Ошибка при создании платежа\\. Обратитесь к менеджеру\\.', {
          parse_mode: 'MarkdownV2',
        });
      }
    }

    // ── Booking: start
    if (data === 'bk_start') return bkStep1(chatId, {});

    // ── Booking: book from model card
    if (data.startsWith('bk_model_')) {
      const id = parseInt(data.replace('bk_model_', ''));
      const m = await get('SELECT id,name FROM models WHERE id=?', [id]).catch(() => null);
      return bkStep1(chatId, m ? { model_id: m.id, model_name: m.name } : {});
    }

    // ── Booking: model selection step 1
    if (data.startsWith('bk_pick_')) {
      const key = data.replace('bk_pick_', '');
      // Preserve any pre-filled session data (e.g. from calculator)
      const existingSession = await getSession(chatId);
      const d = { ...sessionData(existingSession) };
      if (key !== 'any') {
        const m = await get('SELECT id,name FROM models WHERE id=?', [parseInt(key)]).catch(() => null);
        if (m) {
          d.model_id = m.id;
          d.model_name = m.name;
        }
      } else {
        d.model_id = null;
        d.model_name = 'Менеджер подберёт';
      }
      // If event_type already pre-filled (from calculator), skip event type step
      if (d.event_type && Object.keys(EVENT_TYPES).includes(d.event_type)) {
        await safeSend(chatId, `✅ Тип события: *${esc(EVENT_TYPES[d.event_type])}* \\(из калькулятора\\)`, {
          parse_mode: 'MarkdownV2',
        });
        return bkStep2Date(chatId, d);
      }
      return bkStep2EventType(chatId, d);
    }

    // ── Booking: event type
    if (data.startsWith('bk_etype_')) {
      const session = await getSession(chatId);
      const d = sessionData(session);
      const etype = data.replace('bk_etype_', '');
      if (!Object.keys(EVENT_TYPES).includes(etype)) return;
      d.event_type = etype;
      return bkStep2Date(chatId, d);
    }

    // ── Booking: duration
    if (data.startsWith('bk_dur_')) {
      const session = await getSession(chatId);
      const d = sessionData(session);
      d.event_duration = data.replace('bk_dur_', '');
      return bkStep2Location(chatId, d);
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
      // calc_book_ETYPE_HOURS — start booking pre-filled from calculator
      const cbm = data.match(/^calc_book_(.+)_(\d+)$/);
      if (cbm) {
        const [, etype, durStr] = cbm;
        const preData = {};
        if (Object.keys(EVENT_TYPES).includes(etype)) preData.event_type = etype;
        if (durStr) preData.event_duration = durStr;
        return bkStep1(chatId, preData);
      }

      const cm = data.match(/^calc_(models|hours|type)_(\d+)_(\d+)_(.+)$/);
      if (cm) {
        const [, , modelsStr, hoursStr, type] = cm;
        const calcModels = parseInt(modelsStr);
        const calcHours = parseInt(hoursStr);
        const VALID_CALC_EVENT_TYPES = Object.keys(DEFAULT_RATES.type_multipliers);
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

    // ── Booking: budget confirmation (budget below minimum)
    if (data === 'bk_budget_continue') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep2Comments(chatId, d);
    }
    if (data === 'bk_budget_change') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      delete d.budget;
      return bkStep2Budget(chatId, d);
    }

    // ── Booking: back navigation
    if (data === 'bk_back_event_type') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep2EventType(chatId, d);
    }
    if (data === 'bk_back_duration') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep2Duration(chatId, d);
    }
    if (data === 'bk_back_location') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep2Location(chatId, d);
    }
    if (data === 'bk_back_budget') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      return bkStep2Budget(chatId, d);
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
      const requireEmail = await getSetting('booking_require_email').catch(() => '0');
      if (requireEmail === '1') {
        await bot.answerCallbackQuery(q.id, { text: '❌ Email обязателен для заявки' }).catch(() => {});
        return;
      }
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
      const username = data.replace('bk_use_tg_', '');
      const session = await getSession(chatId);
      const d = sessionData(session);
      d.client_telegram = username;
      return bkStep4Confirm(chatId, d);
    }

    // ── Booking: add another model (multi-model selection)
    if (data === 'bk_add_model') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      // Save state as bk_s1_add so we know we're adding, not starting fresh
      await setSession(chatId, 'bk_s1_add', d);
      resetSessionTimer(chatId);
      try {
        const currentModelIds = Array.isArray(d.model_ids) ? d.model_ids : d.model_id ? [d.model_id] : [];
        const models = await query(
          'SELECT id,name,height,hair_color FROM models WHERE available=1 AND COALESCE(archived,0)=0 ORDER BY id LIMIT 12'
        );
        // Exclude already selected models
        const available = models.filter(m => !currentModelIds.includes(m.id));
        const btns = available.map(m => [
          {
            text: `${m.name}  ·  ${m.height}см  ·  ${m.hair_color || ''}`,
            callback_data: `bk_pick2_${m.id}`,
          },
        ]);
        if (!btns.length) {
          return safeSend(chatId, '⚠️ Нет доступных моделей для добавления\\.', {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'bk_s4_back' }]] },
          });
        }
        return safeSend(chatId, `_Добавить ещё одну модель_\n\nВыберите дополнительную модель:`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [...btns, [{ text: '← Назад к заявке', callback_data: 'bk_s4_back' }]] },
        });
      } catch (e) {
        console.error('[Bot] bk_add_model:', e.message);
      }
    }

    // ── Booking: pick second/additional model
    if (data.startsWith('bk_pick2_')) {
      const modelId = parseInt(data.replace('bk_pick2_', ''));
      const session = await getSession(chatId);
      const d = sessionData(session);
      const m = await get('SELECT id,name FROM models WHERE id=?', [modelId]).catch(() => null);
      if (m) {
        // Build model_ids array
        const currentIds = Array.isArray(d.model_ids) ? d.model_ids : d.model_id ? [d.model_id] : [];
        const currentNames = Array.isArray(d.model_names) ? d.model_names : d.model_name ? [d.model_name] : [];
        if (!currentIds.includes(m.id)) {
          currentIds.push(m.id);
          currentNames.push(m.name);
        }
        d.model_ids = currentIds;
        d.model_names = currentNames;
        // Keep primary model_id/model_name as first entry
        if (currentIds.length > 0) {
          d.model_id = currentIds[0];
          d.model_name = currentNames[0];
        }
      }
      return bkStep4Confirm(chatId, d);
    }

    // ── Booking: back to step 4 (from add-model screen)
    if (data === 'bk_s4_back') {
      const session = await getSession(chatId);
      const d = sessionData(session);
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

    // ── Booking: cancel (show confirmation first)
    if (data === 'bk_cancel') {
      await bot.answerCallbackQuery(q.id);
      return safeSend(chatId, '⚠️ *Отменить бронирование?*\nВесь прогресс будет потерян\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '✅ Да, отменить', callback_data: 'bk_cancel_confirm' }],
            [{ text: '↩️ Продолжить оформление', callback_data: 'bk_resume' }],
          ],
        },
      });
    }

    // ── Booking: cancel confirmed
    if (data === 'bk_cancel_confirm') {
      clearTimeout(sessionTimers.get(chatId));
      sessionTimers.delete(chatId);
      clearSessionWarning(chatId);
      clearSessionReminder(chatId);
      await clearSession(chatId);
      return isAdmin(chatId) ? showAdminMenu(chatId, q.from.first_name) : showMainMenu(chatId, q.from.first_name);
    }

    // ── Booking soft-reminder: resume or cancel session
    if (data === 'bk_resume') {
      const sess = await getSession(chatId);
      const st = sess?.state;
      resetSessionTimer(chatId); // reset hard timeout + soft reminder
      if (!st || !st.startsWith('bk_') || st === 'bk_quick_name') {
        // No active booking session or session expired — send back to menu
        await clearSession(chatId);
        return showMainMenu(chatId, q.from.first_name);
      }
      return safeSend(chatId, '✅ *Продолжаем бронирование\\!*\n\nОтвечайте на последний вопрос\\.', {
        parse_mode: 'MarkdownV2',
      });
    }
    if (data === 'bk_cancel_session') {
      clearTimeout(sessionTimers.get(chatId));
      sessionTimers.delete(chatId);
      clearSessionWarning(chatId);
      clearSessionReminder(chatId);
      await clearSession(chatId);
      await safeSend(chatId, '❌ *Бронирование отменено\\.*', { parse_mode: 'MarkdownV2' });
      return showMainMenu(chatId, q.from.first_name);
    }

    // ── Generic session reminder: resume or cancel
    if (data === 'resume_session') {
      resetSessionTimer(chatId); // reset hard timeout + soft reminder
      return safeSend(chatId, '✅ Продолжаем\\!', { parse_mode: 'MarkdownV2' });
    }
    if (data === 'cancel_session') {
      clearTimeout(sessionTimers.get(chatId));
      sessionTimers.delete(chatId);
      clearSessionWarning(chatId);
      clearSessionReminder(chatId);
      await clearSession(chatId);
      await safeSend(chatId, '❌ Действие отменено\\.', { parse_mode: 'MarkdownV2' });
      return showMainMenu(chatId, q.from.first_name);
    }

    // ── Session: keepalive (triggered from warning message before timeout)
    if (data === 'session_keepalive') {
      resetSessionTimer(chatId);
      await bot.answerCallbackQuery(q.id, { text: '✅ Время продлено!' });
      return safeSend(chatId, '✅ Хорошо\\! Время сессии продлено\\. Продолжайте заполнение\\.', {
        parse_mode: 'MarkdownV2',
      });
    }

    // ── Session: continue / restart
    if (data === 'session_continue') {
      return safeSend(chatId, '✅ Хорошо, продолжаем с того места где остановились\\.', { parse_mode: 'MarkdownV2' });
    }
    if (data === 'session_restart') {
      clearTimeout(sessionTimers.get(chatId));
      sessionTimers.delete(chatId);
      clearSessionWarning(chatId);
      clearSessionReminder(chatId);
      await clearSession(chatId);
      return safeSend(chatId, '🔄 Начинаем заново\\. Используйте кнопки меню для навигации\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }],
            [{ text: '⚡ Быстрая заявка', callback_data: 'bk_quick' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
          ],
        },
      });
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

    // ── Admin orders today filter
    if (data === 'adm_orders_today') {
      if (!isAdmin(chatId)) return;
      return showAdminOrdersToday(chatId);
    }

    // ── Admin orders filter by model — picker
    if (data === 'adm_orders_filter_model') {
      if (!isAdmin(chatId)) return;
      return showAdminOrdersFilterModel(chatId);
    }

    // ── Admin orders filter by model — results: adm_orders_model_{id}
    if (data.startsWith('adm_orders_model_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_orders_model_', ''));
      if (modelId > 0) return showAdminOrdersByModel(chatId, modelId);
    }

    // ── Admin orders list: adm_orders_{status}_{page}
    if (data.startsWith('adm_orders_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_orders_', '').split('_');
      const page = parseInt(parts.pop()) || 0;
      const status = parts.join('_');
      return showAdminOrders(chatId, status, page);
    }

    // ── Admin order search by number
    if (data === 'adm_order_search') {
      if (!isAdmin(chatId)) return;
      return showAdminOrderSearch(chatId);
    }

    // ── Admin order status history
    if (data.startsWith('adm_order_history_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_order_history_', ''));
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
      return safeSend(
        chatId,
        `📝 *Внутренняя заметка для заявки \\#${esc(order.order_number || String(orderId))}*\n\nТекущая: ${order.internal_note ? esc(order.internal_note) : '_нет_'}\n\nВведите новую заметку \\(до 1000 символов\\) или /cancel:`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [[{ text: '🗑 Удалить заметку', callback_data: `adm_order_note_del_${orderId}` }]],
          },
        }
      );
    }

    // ── Admin order detail
    if (data.startsWith('adm_order_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_order_', ''));
      return showAdminOrder(chatId, id);
    }

    // ── Admin order actions
    if (data.startsWith('adm_confirm_')) {
      if (!isAdmin(chatId)) return;
      return adminChangeStatus(chatId, parseInt(data.replace('adm_confirm_', '')), 'confirmed');
    }
    if (data.startsWith('adm_review_')) {
      if (!isAdmin(chatId)) return;
      return adminChangeStatus(chatId, parseInt(data.replace('adm_review_', '')), 'reviewing');
    }
    if (data.startsWith('adm_reject_confirm_')) {
      if (!isAdmin(chatId)) return;
      return adminChangeStatus(chatId, parseInt(data.replace('adm_reject_confirm_', '')), 'cancelled');
    }
    if (data.startsWith('adm_reject_')) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_reject_', ''));
      const order = await get('SELECT order_number, client_name FROM orders WHERE id=?', [orderId]).catch(() => null);
      const label = order
        ? `${esc(order.order_number)}${order.client_name ? ` \\(${esc(order.client_name)}\\)` : ''}`
        : String(orderId);
      return safeSend(chatId, `❓ *Отклонить заявку ${label}?*\n\nКлиент получит уведомление об отмене\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '✅ Да, отклонить', callback_data: `adm_reject_confirm_${orderId}` }],
            [{ text: '❌ Назад', callback_data: `adm_order_${orderId}` }],
          ],
        },
      });
    }
    if (data.startsWith('adm_complete_')) {
      if (!isAdmin(chatId)) return;
      return adminChangeStatus(chatId, parseInt(data.replace('adm_complete_', '')), 'completed');
    }

    // ── Invoice: send payment info to client
    if (data.startsWith('adm_invoice_')) {
      if (!isAdmin(chatId)) return;
      try {
        const orderId = parseInt(data.replace('adm_invoice_', ''));
        const order = await get('SELECT * FROM orders WHERE id=?', [orderId]).catch(() => null);
        if (!order) return safeSend(chatId, '❌ Заявка не найдена');
        if (!order.client_chat_id) return safeSend(chatId, '❌ У клиента нет Telegram чата');
        const budgetStr = order.budget ? String(order.budget) : 'уточняется';
        const invoiceText =
          `💳 *Счёт на оплату*\n\n` +
          `Заявка №${esc(order.order_number)}\n` +
          `Сумма: ${esc(budgetStr)} ₽\n\n` +
          `Для оплаты свяжитесь с менеджером или оплатите по реквизитам:\n` +
          `📞 \\+7 \\(999\\) 123\\-45\\-67\n` +
          `💳 Карта: 1234 5678 9012 3456\n\n` +
          `После оплаты статус заявки будет изменён\\.`;
        await bot.sendMessage(order.client_chat_id, invoiceText, { parse_mode: 'MarkdownV2' }).catch(() => {});
        await run('UPDATE orders SET invoice_sent_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?', [
          orderId,
        ]).catch(() => {});
        return safeSend(chatId, '✅ Счёт выставлен клиенту');
      } catch (e) {
        console.error('[Bot] adm_invoice_:', e.message);
      }
    }

    // ── Payment confirmation: mark as paid
    if (data.startsWith('adm_paid_')) {
      if (!isAdmin(chatId)) return;
      try {
        const orderId = parseInt(data.replace('adm_paid_', ''));
        await run('UPDATE orders SET paid_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?', [
          orderId,
        ]).catch(() => {});
        await safeSend(chatId, '💰 Оплата зафиксирована\\.', { parse_mode: 'MarkdownV2' });
        return showAdminOrder(chatId, orderId);
      } catch (e) {
        console.error('[Bot] adm_paid_:', e.message);
      }
    }

    // ── Payment confirmation: still awaiting
    if (data.startsWith('adm_await_pay_')) {
      if (!isAdmin(chatId)) return;
      try {
        const orderId = parseInt(data.replace('adm_await_pay_', ''));
        await safeSend(chatId, '⏳ Статус оплаты: ожидаем\\.', { parse_mode: 'MarkdownV2' });
        return showAdminOrder(chatId, orderId);
      } catch (e) {
        console.error('[Bot] adm_await_pay_:', e.message);
      }
    }

    if (data.startsWith('adm_contact_')) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_contact_', ''));
      const order = await get('SELECT * FROM orders WHERE id=?', [orderId]).catch(() => null);
      if (!order) return safeSend(chatId, RU.ORDER_NOT_FOUND);
      await setSession(chatId, 'replying', {
        order_id: orderId,
        order_number: order.order_number,
        client_name: order.client_name,
      });
      return safeSend(
        chatId,
        `💬 Введите сообщение для клиента *${order.client_name}* \\(${esc(order.order_number)}\\):\n\n_/cancel — отменить_`,
        { parse_mode: 'MarkdownV2' }
      );
    }

    // ── Admin models
    // New paginated format: adm_models_p_{page}_{sort}_{archived}
    if (data.startsWith('adm_models_p_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_models_p_', '').split('_');
      const page = parseInt(parts[0]) || 0;
      const sort = parts[1] || 'name';
      const archived = parts[2] === '1';
      return showAdminModels(chatId, page, { sort, archived });
    }
    // Legacy format: adm_models_{page}
    if (data.startsWith('adm_models_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('adm_models_', '')) || 0;
      return showAdminModels(chatId, page, {});
    }
    // adm_models (no suffix) — main menu
    if (data === 'adm_models') {
      if (!isAdmin(chatId)) return;
      return showAdminModels(chatId, 0, {});
    }
    // ── Admin model calendar (must be before generic adm_model_ handler)
    if (data.startsWith('adm_model_cal_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_model_cal_', ''));
      return showAdminModelCalendar(chatId, modelId);
    }

    // ── Admin model stats (must be before generic adm_model_ handler)
    if (data.startsWith('adm_model_stats_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_model_stats_', ''));
      return showModelStats(chatId, modelId);
    }

    // ── Archive model (adm_model_archive_ prefix, must be before generic adm_model_ handler)
    if (data.startsWith('adm_model_archive_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_model_archive_', ''));
      await run('UPDATE models SET archived=1, available=0, updated_at=CURRENT_TIMESTAMP WHERE id=?', [modelId]);
      await logAdminAction(chatId, 'archive_model', 'model', modelId);
      await bot.answerCallbackQuery(q.id, { text: '📦 Модель перемещена в архив' }).catch(() => {});
      return safeSend(chatId, `✅ Модель перемещена в архив\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [
              { text: '↩️ Восстановить', callback_data: `adm_model_restore_${modelId}` },
              { text: '← Назад', callback_data: 'adm_models' },
            ],
          ],
        },
      });
    }

    // ── Restore model (adm_model_restore_ prefix, must be before generic adm_model_ handler)
    if (data.startsWith('adm_model_restore_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_model_restore_', ''));
      await run('UPDATE models SET archived=0, updated_at=CURRENT_TIMESTAMP WHERE id=?', [modelId]);
      await logAdminAction(chatId, 'restore_model', 'model', modelId);
      await bot.answerCallbackQuery(q.id, { text: '✅ Модель восстановлена' }).catch(() => {});
      return safeSend(chatId, `✅ Модель восстановлена\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Список моделей', callback_data: 'adm_models' }]] },
      });
    }

    // ── Duplicate model (adm_model_dup_ prefix, must be before generic adm_model_ handler)
    if (data.startsWith('adm_model_dup_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_model_dup_', ''));
      const orig = await get('SELECT * FROM models WHERE id=?', [modelId]).catch(() => null);
      if (!orig) return;
      const { id: newId } = await run(
        `INSERT INTO models (name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color,
          bio, instagram, phone, category, city, featured, available, archived, photos)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?)`,
        [
          orig.name + ' (копия)',
          orig.age,
          orig.height,
          orig.weight,
          orig.bust,
          orig.waist,
          orig.hips,
          orig.shoe_size,
          orig.hair_color,
          orig.eye_color,
          orig.bio,
          orig.instagram,
          orig.phone,
          orig.category,
          orig.city,
          orig.photos,
        ]
      );
      await bot.answerCallbackQuery(q.id, { text: `✅ Создана копия: ID ${newId}` }).catch(() => {});
      return safeSend(chatId, `✅ Модель скопирована\\. ID новой карточки: *${newId}*`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: `✏️ Редактировать копию`, callback_data: `adm_model_${newId}` }]] },
      });
    }

    if (data.startsWith('adm_model_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_model_', ''));
      return showAdminModel(chatId, id);
    }
    if (data.startsWith('adm_toggle_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_toggle_', ''));
      const m = await get('SELECT available FROM models WHERE id=?', [id]).catch(() => null);
      if (m) await run('UPDATE models SET available=? WHERE id=?', [m.available ? 0 : 1, id]);
      return showAdminModel(chatId, id);
    }
    if (data.startsWith('adm_featured_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_featured_', ''));
      const m = await get('SELECT featured FROM models WHERE id=?', [id]).catch(() => null);
      if (m) await run('UPDATE models SET featured=? WHERE id=?', [m.featured ? 0 : 1, id]);
      await bot
        .answerCallbackQuery(q.id, { text: m?.featured ? '⭐ Убрано из топа' : '⭐ Добавлено в топ' })
        .catch(() => {});
      return showAdminModel(chatId, id);
    }
    // ── Archive / Restore model
    if (data.startsWith('adm_archive_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_archive_', ''));
      await run('UPDATE models SET archived=1, available=0 WHERE id=?', [id]);
      await logAdminAction(chatId, 'archive_model', 'model', id);
      await bot.answerCallbackQuery(q.id, { text: '📦 Модель перемещена в архив' }).catch(() => {});
      return showAdminModels(chatId, 0, {});
    }
    if (data.startsWith('adm_restore_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_restore_', ''));
      await run('UPDATE models SET archived=0 WHERE id=?', [id]);
      await logAdminAction(chatId, 'restore_model', 'model', id);
      await bot.answerCallbackQuery(q.id, { text: '✅ Модель восстановлена из архива' }).catch(() => {});
      return showAdminModels(chatId, 0, { archived: true });
    }
    // ── Duplicate model
    if (data.startsWith('adm_duplicate_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_duplicate_', ''));
      const m = await get('SELECT * FROM models WHERE id=?', [id]);
      if (!m) return;
      const { id: newId } = await run(
        `INSERT INTO models (name, age, height, weight, bust, waist, hips, shoe_size, hair_color, eye_color,
          bio, instagram, phone, category, city, featured, available, archived, photos)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?)`,
        [
          m.name + ' (копия)',
          m.age,
          m.height,
          m.weight,
          m.bust,
          m.waist,
          m.hips,
          m.shoe_size,
          m.hair_color,
          m.eye_color,
          m.bio,
          m.instagram,
          m.phone,
          m.category,
          m.city,
          m.photos,
        ]
      );
      await bot.answerCallbackQuery(q.id, { text: `✅ Создана копия: ID ${newId}` }).catch(() => {});
      return safeSend(
        chatId,
        `✅ Модель *${esc(m.name)}* скопирована\\.\nНовый ID: *${newId}*\n\nОтредактируйте детали новой карточки\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [[{ text: '✏️ Редактировать копию', callback_data: `adm_model_${newId}` }]],
          },
        }
      );
    }
    // ── Search model by name
    if (data === 'adm_search_model') {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, 'adm_search_model_input', {});
      return safeSend(chatId, '🔍 Введите имя или часть имени модели:', {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_models_p_0_name_0' }]] },
      });
    }

    // ── Settings
    if (data === 'adm_settings') {
      if (!isAdmin(chatId)) {
        await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(() => {});
        return;
      }
      return showAdminSettings(chatId, 'main');
    }
    // Підрозділи налаштувань
    if (data === 'adm_settings_contacts') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'contacts');
    }
    if (data === 'adm_settings_notifs') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'notifs');
    }
    if (data === 'adm_settings_notif') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'notifs');
    } // alias (singular)
    if (data === 'adm_settings_catalog') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'catalog');
    }
    if (data === 'adm_settings_booking') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'booking');
    }
    if (data === 'adm_settings_reviews') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'reviews');
    }
    if (data === 'adm_settings_cities') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'cities');
    }
    if (data === 'adm_settings_bot') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'bot');
    }
    if (data === 'adm_settings_limits') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'limits');
    }
    if (data === 'adm_settings_ui') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'ui');
    }
    if (data === 'adm_settings_social') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'social');
    }
    if (data === 'adm_instagram_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('instagram_enabled', '1');
      return showAdminSettings(chatId, 'social');
    }
    if (data === 'adm_instagram_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('instagram_enabled', '0');
      return showAdminSettings(chatId, 'social');
    }
    // Toggle налаштування каталогу
    if (data === 'adm_catalog_sort_date') {
      if (!isAdmin(chatId)) return;
      await setSetting('catalog_sort', 'date');
      return showAdminSettings(chatId, 'catalog');
    }
    if (data === 'adm_catalog_sort_featured') {
      if (!isAdmin(chatId)) return;
      await setSetting('catalog_sort', 'featured');
      return showAdminSettings(chatId, 'catalog');
    }
    if (data === 'adm_catalog_sort_toggle') {
      if (!isAdmin(chatId)) return;
      const cur = (await getSetting('catalog_sort')) || 'featured';
      const next = cur === 'alpha' || cur === 'name' ? 'featured' : cur === 'featured' ? 'date' : 'alpha';
      await setSetting('catalog_sort', next);
      return showAdminSettings(chatId, 'catalog');
    }
    if (data === 'adm_catalog_city_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('catalog_show_city', '1');
      return showAdminSettings(chatId, 'catalog');
    }
    if (data === 'adm_catalog_city_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('catalog_show_city', '0');
      return showAdminSettings(chatId, 'catalog');
    }
    if (data === 'adm_catalog_badge_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('catalog_show_featured_badge', '1');
      return showAdminSettings(chatId, 'catalog');
    }
    if (data === 'adm_catalog_badge_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('catalog_show_featured_badge', '0');
      return showAdminSettings(chatId, 'catalog');
    }
    // Toggle настройки бронирования
    if (data === 'adm_booking_quick_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('quick_booking_enabled', '1');
      return showAdminSettings(chatId, 'booking');
    }
    if (data === 'adm_booking_quick_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('quick_booking_enabled', '0');
      return showAdminSettings(chatId, 'booking');
    }
    if (data === 'adm_booking_autoconfirm_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('booking_auto_confirm', '1');
      return showAdminSettings(chatId, 'booking');
    }
    if (data === 'adm_booking_autoconfirm_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('booking_auto_confirm', '0');
      return showAdminSettings(chatId, 'booking');
    }
    if (data === 'adm_booking_email_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('booking_require_email', '1');
      return showAdminSettings(chatId, 'booking');
    }
    if (data === 'adm_booking_email_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('booking_require_email', '0');
      return showAdminSettings(chatId, 'booking');
    }
    // Toggle настройки отзывов
    if (data === 'adm_reviews_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('reviews_enabled', '1');
      return showAdminSettings(chatId, 'reviews');
    }
    if (data === 'adm_reviews_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('reviews_enabled', '0');
      return showAdminSettings(chatId, 'reviews');
    }
    if (data === 'adm_reviews_auto_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('reviews_auto_approve', '1');
      return showAdminSettings(chatId, 'reviews');
    }
    if (data === 'adm_reviews_auto_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('reviews_auto_approve', '0');
      return showAdminSettings(chatId, 'reviews');
    }
    // Toggle настройки бота
    if (data === 'adm_wishlist_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('wishlist_enabled', '1');
      return showAdminSettings(chatId, 'bot');
    }
    if (data === 'adm_wishlist_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('wishlist_enabled', '0');
      return showAdminSettings(chatId, 'bot');
    }
    if (data === 'adm_search_on') {
      if (!isAdmin(chatId)) return;
      await setSetting('search_enabled', '1');
      return showAdminSettings(chatId, 'bot');
    }
    if (data === 'adm_search_off') {
      if (!isAdmin(chatId)) return;
      await setSetting('search_enabled', '0');
      return showAdminSettings(chatId, 'bot');
    }
    // adm_settings_main — alias for adm_settings (go to main settings menu)
    if (data === 'adm_settings_main') {
      if (!isAdmin(chatId)) return;
      return showAdminSettings(chatId, 'main');
    }
    // Unified feature toggle handler for bot section (adm_toggle_{feature})
    if (data.startsWith('adm_toggle_') && isAdmin(chatId)) {
      const TOGGLE_FEATURES = {
        quick_booking: 'quick_booking_enabled',
        wishlist: 'wishlist_enabled',
        search: 'search_enabled',
        reviews: 'reviews_enabled',
        loyalty: 'loyalty_enabled',
        referral: 'referral_enabled',
        model_stats: 'model_stats_enabled',
        faq: 'faq_enabled',
        calc: 'calc_enabled',
      };
      const featureKey = data.replace('adm_toggle_', '');
      const settingKey = TOGGLE_FEATURES[featureKey];
      if (settingKey) {
        const current = await getSetting(settingKey);
        const newVal = current === '0' ? '1' : '0';
        await setSetting(settingKey, newVal);
        await bot.answerCallbackQuery(q.id, { text: newVal === '1' ? '✅ Включено' : '🔕 Выключено' }).catch(() => {});
        return showAdminSettings(chatId, 'bot');
      }
    }
    if (data === 'adm_broadcast') {
      if (!isAdmin(chatId)) return;
      return showBroadcast(chatId);
    }
    if (data === 'adm_broadcast_history') {
      if (!isAdmin(chatId)) return;
      return showBroadcastHistory(chatId);
    }

    // ── Scheduled broadcasts
    if (data === 'adm_sched_bcast') {
      if (!isAdmin(chatId)) return;
      return showScheduledBroadcasts(chatId);
    }
    if (data === 'adm_new_sched_bcast') {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, 'adm_sched_bcast_text', {});
      return safeSend(chatId, `📅 *Новая запланированная рассылка*\n\nВведите текст рассылки:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_sched_bcast' }]] },
      });
    }
    if (data.startsWith('sched_bcast_cancel_')) {
      if (!isAdmin(chatId)) return;
      const sbId = parseInt(data.replace('sched_bcast_cancel_', ''));
      await run("UPDATE scheduled_broadcasts SET status='cancelled' WHERE id=? AND status='pending'", [sbId]).catch(
        () => {}
      );
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
      await run(`INSERT INTO scheduled_broadcasts (text, scheduled_at, segment, created_by) VALUES (?,?,?,?)`, [
        d2.sched_text,
        d2.sched_time,
        seg,
        String(chatId),
      ]).catch(() => {});
      await clearSession(chatId);
      const segLabel =
        seg === 'completed' ? 'Завершившие заявку' : seg === 'active' ? 'Активные клиенты' : 'Все клиенты';
      return safeSend(
        chatId,
        `✅ *Рассылка запланирована\\!*\n\nВремя: *${esc(d2.sched_time)}*\nСегмент: *${esc(segLabel)}*\n\nТекст: _${esc(String(d2.sched_text || '').slice(0, 100))}_`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '📅 Все рассылки', callback_data: 'adm_sched_bcast' }]] },
        }
      );
    }

    // ── Admin: add busy period (start state)
    if (data.startsWith('adm_add_busy_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_add_busy_', ''));
      await setSession(chatId, `adm_add_busy_${modelId}`, { modelId });
      return safeSend(
        chatId,
        `📅 *Добавить занятый период*\n\nВведите дату или диапазон дат и причину через пробел:\n\n` +
          `_Примеры:_\n\`15\\.05\\.2026 Съёмка Nike\`\n\`15\\.05\\.2026\\-20\\.05\\.2026 Мероприятие\``,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: `adm_model_cal_${modelId}` }]] },
        }
      );
    }

    // ── Admin: delete busy date
    if (data.startsWith('adm_del_busy_')) {
      if (!isAdmin(chatId)) return;
      // format: adm_del_busy_{modelId}_{YYYY-MM-DD}
      const rest = data.replace('adm_del_busy_', '');
      const underIdx = rest.indexOf('_');
      const modelId = parseInt(rest.slice(0, underIdx));
      const busyDate = rest.slice(underIdx + 1);
      // Validate date format to avoid operating on unexpected input
      if (!modelId || modelId <= 0 || !/^\d{4}-\d{2}-\d{2}$/.test(busyDate)) {
        return bot.answerCallbackQuery(q.id, { text: '❌ Неверный формат данных' }).catch(() => {});
      }
      await run('DELETE FROM model_busy_dates WHERE model_id=? AND busy_date=?', [modelId, busyDate]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: '🗑 Дата удалена' }).catch(() => {});
      return showAdminModelCalendar(chatId, modelId);
    }

    // ── Admin model calendar
    if (data.startsWith('adm_model_cal_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_model_cal_', ''));
      return showAdminModelCalendar(chatId, modelId);
    }

    // ── Admin: add busy period (start state)
    if (data.startsWith('adm_add_busy_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_add_busy_', ''));
      await setSession(chatId, `adm_add_busy_${modelId}`, { modelId });
      return safeSend(
        chatId,
        `📅 *Добавить занятый период*\n\nВведите дату или диапазон дат и причину через пробел:\n\n` +
          `_Примеры:_\n\`15\\.05\\.2026 Съёмка Nike\`\n\`15\\.05\\.2026\\-20\\.05\\.2026 Мероприятие\``,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: `adm_model_cal_${modelId}` }]] },
        }
      );
    }

    // ── Admin: delete busy date
    if (data.startsWith('adm_del_busy_')) {
      if (!isAdmin(chatId)) return;
      // format: adm_del_busy_{modelId}_{YYYY-MM-DD}
      const rest = data.replace('adm_del_busy_', '');
      const underIdx = rest.indexOf('_');
      const modelId = parseInt(rest.slice(0, underIdx));
      const busyDate = rest.slice(underIdx + 1);
      await run('DELETE FROM model_busy_dates WHERE model_id=? AND busy_date=?', [modelId, busyDate]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: '🗑 Дата удалена' }).catch(() => {});
      return showAdminModelCalendar(chatId, modelId);
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
    if (data === 'adm_audit_log') {
      if (!isAdmin(chatId)) return;
      return showAuditLog(chatId, 0);
    }
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
    if (data === 'adm_bc_seg_active') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '🕐 Активные (30 дней)' }).catch(() => {});
      return _askBroadcastText(chatId, 'active');
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
      const sd = sessionData(sess);
      await setSession(chatId, 'adm_broadcast_photo_wait', { ...sd });
      return safeSend(chatId, `🖼 *Рассылка — добавить фото*\n\nОтправьте фото:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] },
      });
    }
    if (data === 'adm_bc_send_now') {
      if (!isAdmin(chatId)) return;
      // Send without photo — go straight to preview
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      if (!sd.broadcastText && !sd.broadcastRecipients) return showBroadcast(chatId);
      return previewBroadcast(chatId);
    }
    // ── Broadcast: confirm send
    if (data === 'adm_bc_confirm') {
      if (!isAdmin(chatId)) return;
      return doSendBroadcast(chatId);
    }
    // ── Broadcast: edit text (preserve photo)
    if (data === 'adm_bc_edit') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      await setSession(chatId, 'adm_broadcast_edit_text', { ...sd });
      return safeSend(
        chatId,
        `✏️ *Изменить текст рассылки*\n\nВведите новый текст${sd.broadcastPhotoId ? ' \\(фото сохранится\\)' : ''}:`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [[{ text: '← Назад к предпросмотру', callback_data: 'adm_bc_back_preview' }]],
          },
        }
      );
    }

    // ── Broadcast: edit/add photo from preview
    if (data === 'adm_bc_edit_photo') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      await setSession(chatId, 'adm_broadcast_photo_wait', { ...sd });
      return safeSend(
        chatId,
        `🖼 *Рассылка — ${sd.broadcastPhotoId ? 'изменить' : 'добавить'} фото*\n\nОтправьте фото для рассылки:`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [[{ text: '← Назад к предпросмотру', callback_data: 'adm_bc_back_preview' }]],
          },
        }
      );
    }

    // ── Broadcast: remove photo from preview
    if (data === 'adm_bc_remove_photo') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      sd.broadcastPhotoId = null;
      await setSession(chatId, 'adm_broadcast_preview', sd);
      await bot.answerCallbackQuery(q.id, { text: '🗑 Фото удалено' }).catch(() => {});
      return previewBroadcast(chatId);
    }
    // ── Broadcast: cancel from preview
    if (data === 'adm_bc_cancel_preview') {
      if (!isAdmin(chatId)) return;
      await clearSession(chatId);
      await bot.answerCallbackQuery(q.id, { text: '❌ Рассылка отменена' }).catch(() => {});
      return showBroadcast(chatId);
    }
    // ── Broadcast: back to preview (from edit text/photo screens) without clearing
    if (data === 'adm_bc_back_preview') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      await setSession(chatId, 'adm_broadcast_preview', sd);
      return previewBroadcast(chatId);
    }
    // ── Broadcast: schedule from preview
    if (data === 'adm_bc_schedule') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      await setSession(chatId, 'broadcast_schedule_time', { ...sd });
      return safeSend(
        chatId,
        `🕐 *Запланировать рассылку*\n\nВведите дату и время в формате:\n\`ДД\\.ММ\\.ГГГГ ЧЧ:ММ\`\n\nПример: \`20\\.05\\.2026 14:00\``,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_bc_cancel_preview' }]] },
        }
      );
    }
    // ── Broadcast: view scheduled list
    if (data === 'adm_bc_scheduled') {
      if (!isAdmin(chatId)) return;
      return showScheduledBroadcasts(chatId);
    }
    // ── Broadcast: cancel scheduled by ID (adm_bc_cancel_ID)
    if (data.startsWith('adm_bc_cancel_') && data !== 'adm_bc_cancel_preview') {
      if (!isAdmin(chatId)) return;
      const bcId = parseInt(data.replace('adm_bc_cancel_', ''));
      if (bcId > 0) {
        await run("UPDATE scheduled_broadcasts SET status='cancelled' WHERE id=? AND status='pending'", [bcId]).catch(
          () => {}
        );
        await bot.answerCallbackQuery(q.id, { text: '❌ Рассылка отменена' }).catch(() => {});
        return showScheduledBroadcasts(chatId);
      }
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
      const sd2 = sessionData(sess2);
      await setSession(chatId, 'adm_broadcast_msg', { broadcastSegment: sd2.broadcastSegment || 'all' });
      return safeSend(chatId, `📝 *Рассылка — текст*\n\nВведите текст сообщения для рассылки:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] },
      });
    }
    if (data === 'adm_broadcast_photo') {
      if (!isAdmin(chatId)) return;
      const sess3 = await getSession(chatId);
      const sd3 = sessionData(sess3);
      await setSession(chatId, 'adm_broadcast_photo_wait', { broadcastSegment: sd3.broadcastSegment || 'all' });
      return safeSend(chatId, `🖼 *Рассылка — фото*\n\nОтправьте фото для рассылки:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_broadcast' }]] },
      });
    }
    // ── Quick toggle model availability
    if (data.startsWith('adm_toggle_avail_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_toggle_avail_', ''));
      const m = await get('SELECT available FROM models WHERE id=?', [id]).catch(() => null);
      if (!m) return;
      const newVal = m.available ? 0 : 1;
      await run('UPDATE models SET available=? WHERE id=?', [newVal, id]);
      await logAdminAction(chatId, 'toggle_availability', 'model', id, { available: newVal });
      await bot
        .answerCallbackQuery(q.id, { text: newVal ? '🟢 Модель доступна' : '🔴 Модель недоступна' })
        .catch(() => {});
      return showAdminModels(chatId, 0, {});
    }
    // ── Admin search order
    if (data === 'adm_search_order') {
      if (!isAdmin(chatId)) return;
      return showAdminSearchOrder(chatId);
    }
    // ── Admin search notes
    if (data === 'adm_search_notes') {
      if (!isAdmin(chatId)) return;
      return showAdminSearchNotes(chatId);
    }
    // ── My orders pagination
    if (data.startsWith('my_orders_page_')) {
      const pg = parseInt(data.replace('my_orders_page_', '')) || 0;
      return showMyOrders(chatId, pg);
    }
    // ── Broadcast with photo: skip caption
    if (data === 'adm_broadcast_photo_nosend') {
      if (!isAdmin(chatId)) return;
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      if (!sd.broadcast_photo_id) return safeSend(chatId, '❌ Фото не найдено. Попробуйте заново.');
      return sendBroadcastWithPhoto(chatId, sd.broadcast_photo_id, '');
    }
    if (data === 'adm_reviews') {
      if (!isAdmin(chatId)) return;
      return showAdminReviewsPanel(chatId, 'pending', 0);
    }
    if (data === 'adm_reviews_pending' || data === 'adm_rev_pending') {
      if (!isAdmin(chatId)) return;
      return showAdminReviewsPanel(chatId, 'pending', 0);
    }
    if (data === 'adm_reviews_approved' || data === 'adm_rev_approved') {
      if (!isAdmin(chatId)) return;
      return showAdminReviewsPanel(chatId, 'approved', 0);
    }
    if (data === 'adm_rev_all') {
      if (!isAdmin(chatId)) return;
      return showAdminReviewsPanel(chatId, 'all', 0);
    }
    if (data.startsWith('adm_rev_p_')) {
      if (!isAdmin(chatId)) return;
      // Format: adm_rev_p_{filter}_{page}
      const parts = data.replace('adm_rev_p_', '').split('_');
      const revPage = Math.max(0, parseInt(parts[parts.length - 1]) || 0);
      const rawFilter = parts.slice(0, parts.length - 1).join('_') || 'all';
      // Sanitize filter to prevent unexpected values from callback data
      const revFilter = ['pending', 'approved', 'all'].includes(rawFilter) ? rawFilter : 'pending';
      return showAdminReviewsPanel(chatId, revFilter, revPage);
    }
    if (data.startsWith('rev_view_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_view_', ''));
      try {
        const r = await get(
          `SELECT r.*, m.name as model_name
           FROM reviews r
           LEFT JOIN orders o ON r.order_id = o.id
           LEFT JOIN models m ON o.model_id = m.id
           WHERE r.id=?`,
          [id]
        ).catch(() => null);
        if (!r) return bot.answerCallbackQuery(q.id, { text: 'Отзыв не найден' }).catch(() => {});
        const stars = '⭐'.repeat(Math.max(1, Math.min(5, r.rating || 1)));
        const statusIcon = r.approved ? '✅' : r.status === 'rejected' ? '❌' : '⏳';
        const modelLine = r.model_name ? `\nМодель: ${esc(r.model_name)}` : '';
        const replyLine = r.admin_reply ? `\n\n💬 Ответ: ${esc(r.admin_reply)}` : '';
        return safeSend(
          chatId,
          `📝 *Отзыв \\#${esc(String(r.id))}* ${statusIcon}\n👤 ${esc(r.client_name || 'Клиент')}${modelLine}\n${stars}\n\n${esc(r.text || '')}${replyLine}`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [
                  { text: '✅ Одобрить', callback_data: `rev_approve_${r.id}` },
                  { text: '❌ Отклонить', callback_data: `rev_reject_${r.id}` },
                  { text: '🗑 Удалить', callback_data: `rev_delete_${r.id}` },
                ],
                [{ text: '← К отзывам', callback_data: 'adm_reviews' }],
              ],
            },
          }
        );
      } catch (e) {
        console.error('[Bot] rev_view:', e.message);
      }
    }
    if (data.startsWith('rev_approve_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_approve_', ''));
      await run('UPDATE reviews SET approved=1, status=NULL WHERE id=?', [id]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: '✅ Отзыв одобрен' }).catch(() => {});
      // Notify client if linked to an order
      try {
        const rev = await get('SELECT * FROM reviews WHERE id=?', [id]);
        if (rev && rev.order_id) {
          const ord = await get('SELECT client_chat_id FROM orders WHERE id=?', [rev.order_id]).catch(() => null);
          if (ord?.client_chat_id) {
            await safeSend(ord.client_chat_id, `✅ Ваш отзыв одобрен и опубликован\\. Спасибо\\!`, {
              parse_mode: 'MarkdownV2',
            }).catch(() => {});
          }
        }
      } catch {}
      return showAdminReviewsPanel(chatId, 'pending', 0);
    }
    if (data.startsWith('rev_reject_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_reject_', ''));
      await run("UPDATE reviews SET approved=0, status='rejected' WHERE id=?", [id]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: '❌ Отклонено' }).catch(() => {});
      // Notify client if linked to an order
      try {
        const rev = await get('SELECT * FROM reviews WHERE id=?', [id]);
        if (rev && rev.order_id) {
          const ord = await get('SELECT client_chat_id FROM orders WHERE id=?', [rev.order_id]).catch(() => null);
          if (ord?.client_chat_id) {
            await safeSend(ord.client_chat_id, `ℹ️ Ваш отзыв был отклонён модератором\\.`, {
              parse_mode: 'MarkdownV2',
            }).catch(() => {});
          }
        }
      } catch {}
      return showAdminReviewsPanel(chatId, 'pending', 0);
    }
    if (data.startsWith('rev_delete_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_delete_', ''));
      await run('DELETE FROM reviews WHERE id=?', [id]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: '🗑️ Удалён' }).catch(() => {});
      return showAdminReviewsPanel(chatId, 'all', 0);
    }
    if (data === 'adm_admins') {
      if (!isAdmin(chatId)) {
        await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(() => {});
        return;
      }
      return showAdminManagement(chatId);
    }
    if (data === 'adm_export') {
      if (!isAdmin(chatId)) {
        await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(() => {});
        return;
      }
      return showExportMenu(chatId);
    }
    if (data === 'adm_addmodel') {
      if (!isAdmin(chatId)) return;
      return showAddModelStep(chatId, { _step: 'name' });
    }

    // ── Admin: managers list & stats
    if (data === 'adm_managers') {
      if (!isAdmin(chatId)) return;
      return showManagersList(chatId);
    }
    if (data.startsWith('adm_mgr_stat_')) {
      if (!isAdmin(chatId)) return;
      const managerId = parseInt(data.replace('adm_mgr_stat_', ''));
      if (!isNaN(managerId)) return showManagerStats(chatId, managerId);
    }

    // ── Admin: client management
    if (data === 'adm_clients') {
      if (!isAdmin(chatId)) return;
      return showAdminClients(chatId, 0);
    }
    if (data === 'adm_panel') {
      if (!isAdmin(chatId)) return;
      return showAdminMenu(chatId, q.from.first_name);
    }
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
      await bot.answerCallbackQuery(q.id, { text: '⛔ Клиент заблокирован' }).catch(() => {});
      return showAdminClientCard(chatId, clientId);
    }
    if (data.startsWith('adm_unblock_')) {
      if (!isAdmin(chatId)) return;
      const clientId = parseInt(data.replace('adm_unblock_', ''));
      await run(`DELETE FROM blocked_clients WHERE chat_id=?`, [clientId]);
      await bot.answerCallbackQuery(q.id, { text: '✅ Клиент разблокирован' }).catch(() => {});
      return showAdminClientCard(chatId, clientId);
    }

    // ── Admin: send personal message to client
    if (data.startsWith('adm_msg_client_')) {
      if (!isAdmin(chatId)) return;
      const clientId = parseInt(data.replace('adm_msg_client_', ''));
      await setSession(chatId, `adm_personal_msg_${clientId}`, {});
      return safeSend(
        chatId,
        `📝 Введите сообщение для клиента \\(ID: ${clientId}\\):\n\n_Сообщение будет отправлено от имени бота_`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: `adm_client_${clientId}` }]] },
        }
      );
    }

    // ── Export period filters
    if (data === 'adm_export_today') {
      if (!isAdmin(chatId)) return;
      return doExportOrders(chatId, 'today');
    }
    if (data === 'adm_export_week') {
      if (!isAdmin(chatId)) return;
      return doExportOrders(chatId, 'week');
    }
    if (data === 'adm_export_month') {
      if (!isAdmin(chatId)) return;
      return doExportOrders(chatId, 'month');
    }
    if (data === 'adm_export_all') {
      if (!isAdmin(chatId)) return;
      return doExportOrders(chatId, 'all');
    }

    // ── Export: CSV documents
    if (data === 'adm_export_orders_csv') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '⏳ Формирую CSV...' }).catch(() => {});
      return showExportOrdersMenu(chatId);
    }
    if (data === 'adm_export_models_csv') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '⏳ Формирую CSV...' }).catch(() => {});
      return exportModelsCSV(chatId);
    }
    if (data === 'adm_export_clients_csv') {
      if (!isAdmin(chatId)) return;
      await bot.answerCallbackQuery(q.id, { text: '⏳ Формирую CSV...' }).catch(() => {});
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
        await bot
          .answerCallbackQuery(q.id, {
            text: `📊 Сегодня: ${todayR.n} | Активных: ${activeR.n} | Выручка/мес: ${revenue} руб.`,
            show_alert: true,
          })
          .catch(() => {});
      } catch {
        await bot.answerCallbackQuery(q.id, { text: '❌ Ошибка загрузки статистики' }).catch(() => {});
      }
      return;
    }

    // ── Telegram channel
    if (data === 'tg_channel') {
      const ch = await getSetting('tg_channel').catch(() => null);
      if (ch) {
        return safeSend(chatId, `📣 *Наш Telegram канал:*\n\n${esc(ch)}`, {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [
                {
                  text: '📣 Перейти в канал',
                  url: ch.startsWith('http') ? ch : `https://t.me/${ch.replace(/^@/, '')}`,
                },
              ],
              [{ text: '← Главное меню', callback_data: 'main_menu' }],
            ],
          },
        });
      }
      return;
    }

    // ── Bulk: новые → В работу
    if (data === 'adm_bulk_new_to_review') {
      if (!isAdmin(chatId)) return;
      const result = await run("UPDATE orders SET status='reviewing', updated_at=CURRENT_TIMESTAMP WHERE status='new'");
      return safeSend(chatId, `✅ Переведено ${result.changes} заявок в статус «На рассмотрении»`, {
        reply_markup: { inline_keyboard: [[{ text: '📋 К заявкам', callback_data: 'adm_orders__0' }]] },
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
      const admins = await query('SELECT id, username, role FROM admins ORDER BY id').catch(() => []);
      if (!admins.length) return safeSend(chatId, '❌ Нет администраторов в базе.');
      const btns = admins.map(a => [
        {
          text: `${a.username} (${a.role})`,
          callback_data: `adm_assign_mgr_${orderId}_${a.id}`,
        },
      ]);
      btns.push([{ text: '← Назад', callback_data: `adm_order_${orderId}` }]);
      return safeSend(chatId, `👤 *Выберите менеджера для заявки*:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: btns },
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
        get('SELECT username, telegram_id FROM admins WHERE id=?', [adminId]).catch(() => null),
        get('SELECT order_number, client_name, event_type FROM orders WHERE id=?', [orderId]).catch(() => null),
      ]);
      // Notify assigned manager if they have a telegram_id
      if (admin?.telegram_id && String(admin.telegram_id) !== String(chatId)) {
        safeSend(
          admin.telegram_id,
          `📋 *Вам назначена заявка \\#${esc(order?.order_number || String(orderId))}*\n\nКлиент: ${esc(order?.client_name || '—')}\nТип: ${esc(EVENT_TYPES[order?.event_type] || order?.event_type || '—')}\n\nОткройте панель управления для просмотра деталей\\.`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '📋 Открыть заявку', callback_data: `adm_order_${orderId}` }]] },
          }
        ).catch(() => {});
      }
      await safeSend(chatId, `✅ Менеджер *${esc(admin?.username || String(adminId))}* назначен на заявку\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] },
      });
      return;
    }

    // ── Assign model to order: show list — adm_assign_model_{orderId}
    if (data.startsWith('adm_assign_model_') && !data.match(/adm_assign_model_\d+_\d+/)) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_assign_model_', ''));
      const models = await query(
        'SELECT id, name, city FROM models WHERE available=1 AND archived=0 ORDER BY name'
      ).catch(() => []);
      if (!models.length) return safeSend(chatId, '❌ Нет доступных моделей в базе\\.', { parse_mode: 'MarkdownV2' });
      const btns = models.map(m => [
        {
          text: `${m.name}${m.city ? ' (' + m.city + ')' : ''}`,
          callback_data: `adm_assign_model_${orderId}_${m.id}`,
        },
      ]);
      btns.push([{ text: '← Назад', callback_data: `adm_order_${orderId}` }]);
      return safeSend(chatId, `💃 *Выберите модель для заявки:*`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: btns },
      });
    }

    // ── Assign model to order: confirm selection — adm_assign_model_{orderId}_{modelId}
    if (data.match(/^adm_assign_model_\d+_\d+$/)) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_assign_model_', '').split('_');
      const orderId = parseInt(parts[0]);
      const modelId = parseInt(parts[1]);
      await run('UPDATE orders SET model_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', [modelId, orderId]);
      const [model, order] = await Promise.all([
        get('SELECT name, telegram_chat_id, phone FROM models WHERE id=?', [modelId]).catch(() => null),
        get('SELECT order_number, event_type, event_date FROM orders WHERE id=?', [orderId]).catch(() => null),
      ]);
      // Notify the model via Telegram if their chat_id is known
      if (model?.telegram_chat_id) {
        const eventLabel = esc(EVENT_TYPES[order?.event_type] || order?.event_type || '—');
        const dateLabel = order?.event_date ? esc(order.event_date) : '—';
        safeSend(
          model.telegram_chat_id,
          `📋 *Новая заявка\\!*\n\n\\#${esc(order?.order_number || String(orderId))}\nДата: ${dateLabel}\nТип: ${eventLabel}\n\nПодтвердите участие:`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [
                  { text: '✅ Принять', callback_data: `mdl_confirm_${orderId}` },
                  { text: '❌ Отклонить', callback_data: `mdl_reject_${orderId}` },
                ],
              ],
            },
          }
        ).catch(() => {});
      }
      const notifiedNote = model?.telegram_chat_id ? '' : ' \\(Telegram не привязан\\)';
      await safeSend(
        chatId,
        `✅ Модель *${esc(model?.name || String(modelId))}* назначена на заявку\\.${notifiedNote}`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] },
        }
      );
      return;
    }

    // ── Quick note templates: adm_qnote_{orderId}_{template}
    if (data.startsWith('adm_qnote_')) {
      if (!isAdmin(chatId)) return;
      const rest = data.slice('adm_qnote_'.length);
      const lastUnderscore = rest.lastIndexOf('_');
      const orderId = parseInt(rest.slice(0, lastUnderscore));
      const tplKey = rest.slice(lastUnderscore + 1);
      if (!orderId) return;

      if (tplKey === 'custom') {
        // Enter custom note state
        await setSession(chatId, `adm_note_input_${orderId}`, {});
        return safeSend(chatId, `📝 *Введите заметку к заявке:*`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: `adm_order_${orderId}` }]] },
        });
      }

      // Save template note with timestamp
      const tplText = QUICK_NOTE_TEMPLATES[tplKey];
      if (!tplText) return;
      const now = new Date()
        .toLocaleString('ru', {
          timeZone: 'Europe/Moscow',
          day: 'numeric',
          month: 'long',
          hour: '2-digit',
          minute: '2-digit',
        })
        .replace(' г.', '');
      const noteText = `${tplText} [${now}]`;
      await run('INSERT INTO order_notes (order_id, admin_note) VALUES (?,?)', [orderId, noteText]);
      await bot.answerCallbackQuery(q.id, { text: '✅ Заметка добавлена!' }).catch(() => {});
      return safeSend(chatId, `✅ Заметка добавлена\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] },
      });
    }

    // ── Note delete: adm_note_del_{noteId}_{orderId}_{page}
    if (data.startsWith('adm_note_del_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.slice('adm_note_del_'.length).split('_');
      const noteId = parseInt(parts[0]);
      const orderId = parseInt(parts[1]);
      const page = parseInt(parts[2]) || 0;
      if (!noteId || !orderId) return;
      await run('DELETE FROM order_notes WHERE id=?', [noteId]);
      await bot.answerCallbackQuery(q.id, { text: '🗑 Заметка удалена' }).catch(() => {});
      return showAllOrderNotes(chatId, orderId, page);
    }

    // ── Order note: start input (shows quick templates)
    if (data.startsWith('adm_note_') && !data.startsWith('adm_note_input_') && !data.startsWith('adm_note_del_')) {
      if (!isAdmin(chatId)) return;
      const orderId = parseInt(data.replace('adm_note_', ''));
      return showQuickNoteTemplates(chatId, orderId);
    }

    // ── Settings inputs — set session and ask for text
    const settingPrompts = {
      adm_set_greeting: '📝 Введите новый текст *приветствия* (при /start):',
      adm_set_about: 'ℹ️ Введите новый текст *«О нас»*:',
      adm_set_phone: '📞 Введите новый *номер телефона* агентства:',
      adm_set_email: '📧 Введите новый *email* агентства:',
      adm_set_insta: '📸 Введите новый *Instagram* (без @):',
      adm_set_instagram: '📸 Введите новый *Instagram* (без @):',
      adm_set_addr: '📍 Введите новый *адрес* агентства:',
      adm_set_pricing: '💰 Введите новый *прайс-лист* (можно несколько строк):',
      adm_set_whatsapp: '📱 Введите *WhatsApp* номер (с кодом страны, например +79001234567):',
      adm_set_site_url: '🌐 Введите *URL сайта* (например https://nevesty-models.ru):',
      adm_set_mgr_hours: '🕐 Введите *часы работы менеджера* (например: Пн-Пт 9:00-20:00):',
      adm_set_mgr_reply: '💬 Введите *авто-ответ менеджера* при обращении:',
      adm_set_catalog_per_page: '📄 Введите *кол-во моделей на странице* (рекомендуется 5-10):',
      adm_set_catalog_title: '📌 Введите *заголовок каталога*:',
      adm_set_booking_min_budget: '💰 Введите *минимальный бюджет* для заявки (оставьте пустым — без лимита):',
      adm_set_booking_confirm_msg: '💬 Введите *сообщение после бронирования*:',
      adm_set_booking_thanks: '🎉 Введите *текст после успешного бронирования* (отображается клиенту):',
      adm_set_tg_channel: '📣 Введите *ссылку или @username* Telegram канала агентства:',
      adm_set_reviews_min: '🔢 Введите *минимум завершённых заявок* для написания отзыва:',
      adm_set_reviews_prompt: '📝 Введите *текст приглашения к отзыву*:',
      adm_set_cities_list: '🏙 Введите *список городов* через запятую (например: Москва, Санкт-Петербург, Казань):',
      adm_set_welcome_photo: '🖼 Введите *URL фото* для приветствия (или отправьте ссылку на изображение):',
      adm_set_main_menu_text: '📋 Введите *текст главного меню* бота:',
      adm_set_model_max_photos: '🖼 Введите *максимальное кол-во фото* у модели:',
      adm_set_client_max_orders: '📋 Введите *максимум активных заявок* у одного клиента:',
      adm_set_client_msg_delay: '⏱ Введите *минимальный интервал* между сообщениями клиента (секунды):',
      adm_set_api_rate_limit: '🔒 Введите *rate limit* API (запросов в минуту):',
    };
    if (settingPrompts[data]) {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, data, {});
      return safeSend(chatId, settingPrompts[data], {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_settings' }]] },
      });
    }

    // ── Notifications toggle
    if (data.startsWith('adm_notif_')) {
      if (!isAdmin(chatId)) return;
      // Full-match aliases for longer key names (e.g. adm_notif_order_on, adm_notif_status_on)
      const fullMatchMap = {
        adm_notif_order_on: ['notif_new_order', '1'],
        adm_notif_order_off: ['notif_new_order', '0'],
        adm_notif_status_on: ['notif_status', '1'],
        adm_notif_status_off: ['notif_status', '0'],
        adm_notif_review_on: ['notif_new_review', '1'],
        adm_notif_review_off: ['notif_new_review', '0'],
        adm_notif_msg_on: ['notif_new_message', '1'],
        adm_notif_msg_off: ['notif_new_message', '0'],
        adm_notif_sms_on: ['sms_notifications_enabled', '1'],
        adm_notif_sms_off: ['sms_notifications_enabled', '0'],
      };
      if (fullMatchMap[data]) {
        const [settingKey, val] = fullMatchMap[data];
        await setSetting(settingKey, val);
        return showAdminSettings(chatId, 'notifs');
      }
      // Fallback: short-key pattern (adm_notif_new_on, adm_notif_st_on, etc.)
      const [, , key, onoff] = data.split('_');
      const settingKeyMap = {
        new: 'notif_new_order',
        st: 'notif_status',
        review: 'notif_new_review',
        msg: 'notif_new_message',
        sms: 'sms_notifications_enabled',
      };
      const settingKey = settingKeyMap[key] || 'notif_status';
      await setSetting(settingKey, onoff === 'on' ? '1' : '0');
      return showAdminSettings(chatId, 'notifs');
    }

    // ── Event reminders toggle (admin)
    if (data === 'adm_toggle_event_reminders') {
      if (!isAdmin(chatId)) return;
      const current = await getSetting('event_reminders_enabled');
      await setSetting('event_reminders_enabled', current === '0' ? '1' : '0');
      return showAdminSettings(chatId, 'notifs');
    }

    // ── Add admin Telegram ID
    if (data === 'adm_add_admin_id') {
      if (!isAdmin(chatId)) return;
      await setSession(chatId, 'adm_add_admin_id', {});
      return safeSend(
        chatId,
        '👑 Введите *Telegram ID* нового администратора:\n\n_Получить ID можно через @userinfobot_',
        {
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_admins' }]] },
        }
      );
    }

    // ── Add model wizard — skip buttons
    if (data.startsWith('adm_mdl_skip_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      const skipField = data.replace('adm_mdl_skip_', '');
      const nextSteps = {
        name: 'age',
        age: 'height',
        height: 'params',
        params: 'shoe',
        shoe: 'hair',
        hair: 'eye',
        eye: 'category',
        category: 'instagram',
        instagram: 'bio',
        bio: 'photo',
        photo: 'confirm',
      };
      d2._step = nextSteps[skipField] || 'confirm';
      return showAddModelStep(chatId, d2);
    }

    // ── Add model wizard — select buttons (hair, eye, category)
    if (data.startsWith('adm_mdl_hair_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      d2.hair_color = data.replace('adm_mdl_hair_', '');
      d2._step = 'eye';
      return showAddModelStep(chatId, d2);
    }
    if (data.startsWith('adm_mdl_eye_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      d2.eye_color = data.replace('adm_mdl_eye_', '');
      d2._step = 'category';
      return showAddModelStep(chatId, d2);
    }
    if (data.startsWith('adm_mdl_cat_')) {
      if (!isAdmin(chatId)) return;
      const session2 = await getSession(chatId);
      const d2 = sessionData(session2);
      const newCat = data.replace('adm_mdl_cat_', '');
      if (!Object.keys(MODEL_CATEGORIES).includes(newCat)) return;
      d2.category = newCat;
      d2._step = 'instagram';
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
      const id = parseInt(data.replace('adm_editmodel_', ''));
      return showModelEditMenu(chatId, id);
    }
    if (data.startsWith('adm_ef_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_ef_', '').split('_');
      const modelId = parseInt(parts[0]);
      const field = parts.slice(1).join('_');
      if (field === 'category') {
        // Show category selector
        const btns = Object.entries(MODEL_CATEGORIES).map(([k, v]) => [
          { text: v, callback_data: `adm_efc_${modelId}_${k}` },
        ]);
        btns.push([{ text: '← Назад', callback_data: `adm_editmodel_${modelId}` }]);
        return safeSend(chatId, '🏷 Выберите новую категорию:', { reply_markup: { inline_keyboard: btns } });
      }
      if (field === 'photo') {
        return showPhotoGalleryManager(chatId, modelId);
      }
      const fieldLabels = {
        name: 'имя',
        age: 'возраст',
        height: 'рост (см)',
        weight: 'вес (кг)',
        shoe_size: 'размер обуви',
        instagram: 'Instagram',
        bio: 'описание',
        hair_color: 'цвет волос',
        eye_color: 'цвет глаз',
        params: 'параметры (ОГ/ОТ/ОБ)',
        phone: 'телефон модели',
        city: 'город',
        video_url: 'ссылка на видео (URL)',
      };
      await setSession(chatId, `adm_ef_${modelId}_${field}`, {});
      return safeSend(chatId, `✏️ Введите новое *${fieldLabels[field] || field}*:`, {
        reply_markup: { inline_keyboard: [[{ text: '← Отмена', callback_data: `adm_editmodel_${modelId}` }]] },
      });
    }
    if (data.startsWith('adm_efc_')) {
      // edit field category
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_efc_', '').split('_');
      const modelId = parseInt(parts[0]);
      const cat = parts[1];
      if (!Object.keys(MODEL_CATEGORIES).includes(cat)) return;
      await run('UPDATE models SET category=? WHERE id=?', [cat, modelId]).catch(() => {});
      return safeSend(chatId, '✅ Категория обновлена!', {
        reply_markup: {
          inline_keyboard: [
            [
              { text: '✏️ Редактировать', callback_data: `adm_editmodel_${modelId}` },
              { text: '← Карточка', callback_data: `adm_model_${modelId}` },
            ],
          ],
        },
      });
    }

    // ── Delete model
    if (data.startsWith('adm_del_model_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_del_model_', ''));
      const m = await get('SELECT name FROM models WHERE id=?', [id]).catch(() => null);
      return safeSend(chatId, `🗑 *Удалить модель «${m?.name || id}»?*\n\nЭто действие необратимо!`, {
        reply_markup: {
          inline_keyboard: [
            [{ text: '⚠️ Да, удалить', callback_data: `adm_del_confirm_${id}` }],
            [{ text: '← Отмена', callback_data: `adm_model_${id}` }],
          ],
        },
      });
    }
    if (data.startsWith('adm_del_confirm_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_del_confirm_', ''));
      const m = await get('SELECT name FROM models WHERE id=?', [id]).catch(() => null);
      await run('DELETE FROM models WHERE id=?', [id]).catch(() => {});
      await logAdminAction(chatId, 'delete_model', 'model', id, { name: m?.name });
      return safeSend(chatId, `✅ Модель «${m?.name || id}» удалена.`, {
        reply_markup: { inline_keyboard: [[{ text: '← К моделям', callback_data: 'adm_models_0' }]] },
      });
    }
    if (data.startsWith('adm_gallery_clear_confirm_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_gallery_clear_confirm_', ''));
      await run("UPDATE models SET photo_main=NULL, photos='[]' WHERE id=?", [modelId]).catch(() => {});
      return showPhotoGalleryManager(chatId, modelId);
    }
    if (data.startsWith('adm_gallery_clear_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_gallery_clear_', ''));
      const m = await get('SELECT name FROM models WHERE id=?', [modelId]).catch(() => null);
      return safeSend(
        chatId,
        `🗑 *Очистить все фото «${esc(m?.name || String(modelId))}»?*\n\nВсе загруженные фото будут удалены\\. Действие необратимо\\!`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '⚠️ Да, удалить все фото', callback_data: `adm_gallery_clear_confirm_${modelId}` }],
              [{ text: '← Отмена', callback_data: `adm_gallery_${modelId}` }],
            ],
          },
        }
      );
    }
    if (data.startsWith('adm_gallery_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_gallery_', ''));
      return showPhotoGalleryManager(chatId, modelId);
    }

    // ── Прямой ответ клиенту (direct_reply_chatId — из вопроса менеджеру)
    if (data.startsWith('direct_reply_')) {
      if (!isAdmin(chatId)) return;
      const targetId = data.replace('direct_reply_', '');
      await setSession(chatId, 'direct_reply', { target_chat_id: targetId });
      return safeSend(chatId, `✍️ Введите ответ клиенту (ID: ${targetId}):`, {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_main' }]] },
      });
    }

    // ── Написать менеджеру
    if (data === 'msg_manager_start') {
      await setSession(chatId, 'msg_to_manager', {});
      const autoReply = await getSetting('manager_reply').catch(() => '');
      if (autoReply && autoReply.trim()) {
        await safeSend(chatId, esc(autoReply), { parse_mode: 'MarkdownV2' });
      }
      return safeSend(chatId, '✍️ *Напишите ваш вопрос*\n\nОтправьте сообщение — менеджер ответит в течение часа\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] },
      });
    }

    // ── AI Factory
    if (data === 'adm_factory') {
      if (!isAdmin(chatId)) return;
      return showFactoryPanel(chatId);
    }
    if (data === 'adm_factory_growth') {
      if (!isAdmin(chatId)) return;
      return showFactoryGrowth(chatId, 0);
    }
    if (data.startsWith('adm_factory_growth_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('adm_factory_growth_', '')) || 0;
      return showFactoryGrowth(chatId, page);
    }
    if (data === 'adm_factory_actions') {
      if (!isAdmin(chatId)) return;
      return showFactoryGrowthActions(chatId);
    }
    if (data === 'adm_factory_exp') {
      if (!isAdmin(chatId)) return;
      return showFactoryExperiments(chatId);
    }
    if (data === 'adm_factory_decisions') {
      if (!isAdmin(chatId)) return;
      return showFactoryDecisions(chatId);
    }
    if (data === 'adm_factory_tasks') {
      if (!isAdmin(chatId)) return;
      return showFactoryTasks(chatId, 0);
    }
    if (data === 'adm_experiments') {
      if (!isAdmin(chatId)) return;
      return showAdminExperiments(chatId);
    }
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
        reply_markup: { inline_keyboard: [[{ text: 'AI Задачи', callback_data: 'adm_factory_tasks' }]] },
      });
    }
    if (data.startsWith('factory_task_skip_')) {
      if (!isAdmin(chatId)) return;
      const taskId = parseInt(data.replace('factory_task_skip_', ''));
      await run("UPDATE factory_tasks SET status='skipped' WHERE id=?", [taskId]).catch(() => {});
      await bot.answerCallbackQuery(q.id, { text: 'Задача пропущена' }).catch(() => {});
      return safeSend(chatId, 'Задача пропущена.', {
        reply_markup: { inline_keyboard: [[{ text: 'AI Задачи', callback_data: 'adm_factory_tasks' }]] },
      });
    }
    if (data.startsWith('adm_factory_done_')) {
      if (!isAdmin(chatId)) return;
      const actionId = parseInt(data.replace('adm_factory_done_', ''));
      await new Promise(resolve => {
        const sqlite3 = require('sqlite3').verbose();
        const fdb = new sqlite3.Database(FACTORY_DB_PATH, sqlite3.OPEN_READWRITE, err => {
          if (err) return resolve();
          fdb.run("UPDATE growth_actions SET status='done', updated_at=datetime('now') WHERE id=?", [actionId], () => {
            fdb.close();
            resolve();
          });
        });
      });
      return safeSend(chatId, '✅ Отмечено как выполнено.', {
        reply_markup: { inline_keyboard: [[{ text: '← Growth Actions', callback_data: 'adm_factory_growth' }]] },
      });
    }
    if (data === 'adm_factory_run') {
      if (!isAdmin(chatId)) return;
      await safeSend(chatId, '🔄 Запускаю цикл AI Factory...\n\nРезультат придёт через 1-2 минуты.', {
        reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] },
      });
      // Notify all admins that a factory cycle was started manually
      notifyAdmin('🏭 *AI Factory* — цикл запущен вручную из бота\nРезультат придёт через 1\\-2 минуты\\.', {
        parse_mode: 'MarkdownV2',
      }).catch(() => {});
      const { spawn } = require('child_process');
      const proc = spawn(
        'python3',
        ['-c', 'import sys; sys.path.insert(0,"/home/user/Pablo"); from factory.cycle import run_cycle; run_cycle()'],
        { cwd: '/home/user/Pablo', detached: true, stdio: ['ignore', 'ignore', 'pipe'] }
      );
      proc.stderr.on('data', d => console.error('[Factory]', d.toString().trim()));
      proc.unref();
      return;
    }

    // ── Factory content (AI posts)
    if (data === 'adm_factory_content') {
      if (!isAdmin(chatId)) return;
      return showFactoryContent(chatId);
    }
    if (data.startsWith('adm_fc_pub_')) {
      if (!isAdmin(chatId)) return;
      const postId = parseInt(data.replace('adm_fc_pub_', ''));
      return publishFactoryPost(chatId, postId);
    }
    if (data.startsWith('adm_fc_preview_')) {
      if (!isAdmin(chatId)) return;
      const postId = parseInt(data.replace('adm_fc_preview_', ''));
      return previewFactoryPost(chatId, postId);
    }

    // ── Agent feed
    if (data.startsWith('agent_feed_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('agent_feed_', '')) || 0;
      return showAgentFeed(chatId, page);
    }

    // ── Agent discussions feed
    if (data === 'adm_discussions') {
      if (!isAdmin(chatId)) return;
      return showAgentDiscussions(chatId, '24h', 0);
    }
    if (data.startsWith('adm_disc_')) {
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_disc_', '').split('_');
      const rawPeriod = parts.slice(0, -1).join('_');
      const validPeriods = ['1h', '24h', '7d', '30d'];
      const period = validPeriods.includes(rawPeriod) ? rawPeriod : '24h';
      const page = parseInt(parts[parts.length - 1]) || 0;
      return showAgentDiscussions(chatId, period, page);
    }

    // ── Категории каталога (быстрые фильтры)
    if (data === 'cat_filter_fashion') return showCatalog(chatId, 'fashion', 0, { category: 'fashion' });
    if (data === 'cat_filter_commercial') return showCatalog(chatId, 'commercial', 0, { category: 'commercial' });
    if (data === 'cat_filter_events') return showCatalog(chatId, 'events', 0, { category: 'events' });

    // ── Сортировка каталога
    if (data === 'cat_sort_featured') {
      catalogSortPrefs.set(String(chatId), 'featured');
      return showCatalog(chatId, '', 0);
    }
    if (data === 'cat_sort_newest') {
      catalogSortPrefs.set(String(chatId), 'newest');
      return showCatalog(chatId, '', 0);
    }
    if (data === 'cat_sort_alpha') {
      catalogSortPrefs.set(String(chatId), 'alpha');
      return showCatalog(chatId, '', 0);
    }

    // ── Поиск модели по параметрам (мульти-фильтр, БЛОК 2.4)
    if (data === 'cat_search') return showSearchMenu(chatId);

    // ── AI подбор моделей
    if (data === 'ai_match') return startAiMatch(chatId);

    // Height filter: srch_h_{min}_{max}
    if (data.startsWith('srch_h_')) {
      const parts = data.replace('srch_h_', '').split('_');
      const min = parseInt(parts[0]) || 0;
      const max = parseInt(parts[1]) || 999;
      const f = getSearchFilters(chatId);
      if (f.height_min === min && f.height_max === max) {
        // toggle off
        delete f.height_min;
        delete f.height_max;
      } else {
        f.height_min = min;
        f.height_max = max;
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
        delete f.age_min;
        delete f.age_max;
      } else {
        f.age_min = min;
        f.age_max = max;
      }
      return showSearchMenu(chatId);
    }

    // Category filter: srch_c_{category}
    if (data.startsWith('srch_c_')) {
      const cat = data.replace('srch_c_', '');
      const f = getSearchFilters(chatId);
      f.category = f.category === cat ? null : cat;
      return showSearchMenu(chatId);
    }

    // City filter: srch_city_{city}
    if (data.startsWith('srch_city_')) {
      const city = data.replace('srch_city_', '');
      const f = getSearchFilters(chatId);
      f.city = f.city === city ? null : city;
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

    // No-op button (page indicator x/N) — already answered above, just return
    if (data === 'srch_noop') return;

    // View model from search results
    if (data.startsWith('srch_view_')) {
      const modelId = parseInt(data.replace('srch_view_', ''));
      return showModel(chatId, modelId, { text: '← Назад к поиску', callback_data: 'search_go' });
    }

    // Legacy cat_search_* callbacks (keep for backward compatibility)
    if (data.startsWith('cat_search_height_')) {
      const range = data.replace('cat_search_height_', '');
      const [min, max] = range.split('-').map(Number);
      const f = getSearchFilters(chatId);
      f.height_min = min || 0;
      f.height_max = max || 999;
      return showSearchResults(chatId, f, 0);
    }
    if (data.startsWith('cat_search_age_')) {
      const range = data.replace('cat_search_age_', '');
      const [min, max] = range.split('-').map(Number);
      const f = getSearchFilters(chatId);
      f.age_min = min || 0;
      f.age_max = max || 99;
      return showSearchResults(chatId, f, 0);
    }
    if (data.startsWith('cat_search_res_')) {
      // legacy pagination — just re-run current filters
      const rest = data.replace('cat_search_res_', '');
      const parts = rest.split('_');
      const page2 = parseInt(parts.pop()) || 0;
      const f = getSearchFilters(chatId);
      return showSearchResults(chatId, f, page2);
    }

    // ── Advanced search v2 — new-format callbacks (search_h_*, search_a_*, search_cat_*, search_city_*, etc.)

    // Height filter: search_h_160 → 160-165, search_h_166 → 166-170, etc.
    if (data.startsWith('search_h_')) {
      const key = data.replace('search_h_', '');
      const heightMap = { 160: [160, 165], 166: [166, 170], 171: [171, 175], 176: [176, 180], 181: [181, 220] };
      const range = heightMap[key];
      if (range) {
        const f = getSearchFilters(chatId);
        if (f.height_min === range[0] && f.height_max === range[1]) {
          delete f.height_min;
          delete f.height_max;
        } else {
          f.height_min = range[0];
          f.height_max = range[1];
        }
      }
      return showSearchMenu(chatId);
    }

    // Age filter: search_a_18 → 18-22, search_a_23 → 23-27, etc.
    if (data.startsWith('search_a_')) {
      const key = data.replace('search_a_', '');
      const ageMap = { 18: [18, 22], 23: [23, 27], 28: [28, 32], 33: [33, 99] };
      const range = ageMap[key];
      if (range) {
        const f = getSearchFilters(chatId);
        if (f.age_min === range[0] && f.age_max === range[1]) {
          delete f.age_min;
          delete f.age_max;
        } else {
          f.age_min = range[0];
          f.age_max = range[1];
        }
      }
      return showSearchMenu(chatId);
    }

    // Category filter: search_cat_fashion, search_cat_commercial, search_cat_events
    if (data.startsWith('search_cat_')) {
      const cat = data.replace('search_cat_', '');
      const f = getSearchFilters(chatId);
      f.category = f.category === cat ? null : cat;
      return showSearchMenu(chatId);
    }

    // City filter: search_city_CITY (URL-decoded)
    if (data.startsWith('search_city_')) {
      const rawCity = data.replace('search_city_', '');
      const city = decodeURIComponent(rawCity);
      const f = getSearchFilters(chatId);
      f.city = f.city === city ? null : city;
      return showSearchMenu(chatId);
    }

    // City text input: prompt user to type a city name
    if (data === 'search_city_input') {
      await setSession(chatId, 'search_city_input', {});
      const f = getSearchFilters(chatId);
      const currentCity = f.city ? `\n\n_Текущий город: ${esc(f.city)}_` : '';
      return safeSend(chatId, `🏙 *Поиск по городу*${currentCity}\n\nВведите название города:`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            ...(f.city ? [[{ text: '✖️ Сбросить город', callback_data: 'search_city_clear' }]] : []),
            [{ text: '← Назад к поиску', callback_data: 'cat_search' }],
          ],
        },
      });
    }

    // Clear city filter
    if (data === 'search_city_clear') {
      const f = getSearchFilters(chatId);
      delete f.city;
      return showSearchMenu(chatId);
    }

    // Reset all filters
    if (data === 'search_reset') {
      searchFilters.set(String(chatId), {});
      return showSearchMenu(chatId);
    }

    // Run search with current filters
    if (data === 'search_go') {
      return showSearchResultsV2(chatId, 0);
    }

    // Pagination: search_page_N
    if (data.startsWith('search_page_')) {
      const pageNum = parseInt(data.replace('search_page_', '')) || 0;
      return showSearchResultsV2(chatId, pageNum);
    }

    // ── Отзывы (публичные)
    if (data === 'show_reviews' || data === 'cat_rev' || data === 'cat_reviews') return showPublicReviews(chatId, 0);
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
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
      });
    }
    if (data === 'leave_review') {
      return startLeaveReview(chatId, 0);
    }
    if (data.startsWith('leave_review_')) {
      const orderId = parseInt(data.replace('leave_review_', ''));
      return startLeaveReview(chatId, orderId);
    }
    // ── rev_start_{orderId} — альтернативний формат запуску відгуку
    if (data.startsWith('rev_start_')) {
      const orderId = parseInt(data.replace('rev_start_', ''));
      // Check if review already exists for this order
      if (orderId) {
        const existing = await get('SELECT id FROM reviews WHERE chat_id=? AND order_id=?', [
          String(chatId),
          orderId,
        ]).catch(() => null);
        if (existing) {
          return safeSend(chatId, STRINGS.reviewAlreadyLeft, {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
          });
        }
      }
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
      d.review_rating = rating;
      await setSession(chatId, 'leave_review_text', d);
      return safeSend(chatId, `⭐ Оценка: ${'⭐'.repeat(rating)}\n\nТеперь напишите текст отзыва:`, {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] },
      });
    }

    // ── rev_rate_{rating}_{orderId} — альтернативний формат кнопок оцінки
    if (data.startsWith('rev_rate_')) {
      // rev_rate_5_123
      const parts = data.split('_'); // ['rev', 'rate', '5', '123']
      const rating = parseInt(parts[2]);
      const orderId = parseInt(parts[3]);
      if (!rating || rating < 1 || rating > 5) return;
      // Verify order belongs to this user (if orderId provided)
      if (orderId) {
        const order = await get('SELECT id, status FROM orders WHERE id=? AND client_chat_id=?', [
          orderId,
          String(chatId),
        ]).catch(() => null);
        if (!order) {
          return safeSend(chatId, '❌ Заявка не найдена\\.', { parse_mode: 'MarkdownV2' });
        }
        // Check for duplicate review
        const existing = await get('SELECT id FROM reviews WHERE chat_id=? AND order_id=?', [
          String(chatId),
          orderId,
        ]).catch(() => null);
        if (existing) {
          return safeSend(chatId, '✅ Ви вже залишали відгук на цю заявку\\.', {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
          });
        }
      }
      const session = await getSession(chatId);
      const d = sessionData(session);
      d.review_order_id = orderId || null;
      d.review_rating = rating;
      await setSession(chatId, 'leave_review_text', d);
      const starLabel = rating === 5 ? '🌟' : '⭐'.repeat(rating);
      return safeSend(
        chatId,
        `${starLabel} *Оцінка: ${rating}/5*\n\nТепер напишіть короткий відгук \\(або надішліть «\\.» щоб пропустити\\):`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Скасувати', callback_data: 'main_menu' }]] },
        }
      );
    }

    // ── Повторить заявку
    if (data.startsWith('repeat_order_')) {
      const orderId = parseInt(data.replace('repeat_order_', ''));
      return repeatOrder(chatId, orderId);
    }

    // ── Підтвердити повторну заявку
    if (data === 'bk_repeat_confirm') {
      const session = await getSession(chatId);
      const d = sessionData(session);
      if (session?.state !== 'bk_repeat_confirm' || !d.client_name || !d.client_phone || !d.event_type) {
        await clearSession(chatId);
        return safeSend(chatId, '❌ Сесія застаріла\\. Спробуйте ще раз\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] },
        });
      }
      return bkRepeatSubmit(chatId, d, q.from.username);
    }

    // ── Скасувати повторну заявку
    if (data === 'bk_repeat_cancel') {
      await clearSession(chatId);
      return isAdmin(chatId) ? showAdminMenu(chatId, q.from.first_name) : showMainMenu(chatId, q.from.first_name);
    }

    // ── Профиль: изменить контакты
    if (data === 'profile_edit_contacts') return startEditProfile(chatId);
    if (data === 'profile_edit_name') {
      await setSession(chatId, 'profile_edit_name', {});
      return safeSend(chatId, STRINGS.profileEditName, {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'profile' }]] },
      });
    }
    if (data === 'profile_edit_phone') {
      await setSession(chatId, 'profile_edit_phone', {});
      return safeSend(chatId, STRINGS.profileEditPhone, {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'profile' }]] },
      });
    }
    if (data === 'profile_edit_email') {
      await setSession(chatId, 'profile_edit_email', {});
      return safeSend(chatId, STRINGS.profileEditEmail, {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'profile' }]] },
      });
    }

    // ── Настройки уведомлений клиента
    if (data === 'client_notif_settings') return showClientNotificationSettings(chatId);

    if (data === 'client_notif_status') {
      const prefs = (await get('SELECT * FROM client_prefs WHERE chat_id=?', [chatId]).catch(() => null)) || {
        notify_status: 1,
      };
      await run(
        `INSERT INTO client_prefs (chat_id, notify_status) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET notify_status=excluded.notify_status, updated_at=CURRENT_TIMESTAMP`,
        [chatId, prefs.notify_status ? 0 : 1]
      ).catch(() => {});
      return showClientNotificationSettings(chatId);
    }

    if (data === 'client_notif_marketing') {
      const prefs = (await get('SELECT notify_marketing FROM client_prefs WHERE chat_id=?', [chatId]).catch(
        () => null
      )) || { notify_marketing: 1 };
      const newVal = prefs.notify_marketing === 0 || prefs.notify_marketing === false ? 1 : 0;
      await run(
        `INSERT INTO client_prefs (chat_id, notify_marketing) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET notify_marketing=excluded.notify_marketing, updated_at=CURRENT_TIMESTAMP`,
        [chatId, newVal]
      ).catch(() => {});
      return showClientNotificationSettings(chatId);
    }

    if (data === 'client_notif_review_invites') {
      const prefs = (await get('SELECT notify_review_invites FROM client_prefs WHERE chat_id=?', [chatId]).catch(
        () => null
      )) || { notify_review_invites: 1 };
      const newVal = prefs.notify_review_invites === 0 || prefs.notify_review_invites === false ? 1 : 0;
      await run(
        `INSERT INTO client_prefs (chat_id, notify_review_invites) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET notify_review_invites=excluded.notify_review_invites, updated_at=CURRENT_TIMESTAMP`,
        [chatId, newVal]
      ).catch(() => {});
      return showClientNotificationSettings(chatId);
    }

    if (data === 'client_notif_reminders') {
      const prefs = (await get('SELECT notify_reminders FROM client_prefs WHERE chat_id=?', [chatId]).catch(
        () => null
      )) || { notify_reminders: 1 };
      const newVal = prefs.notify_reminders === 0 || prefs.notify_reminders === false ? 1 : 0;
      await run(
        `INSERT INTO client_prefs (chat_id, notify_reminders) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET notify_reminders=excluded.notify_reminders, updated_at=CURRENT_TIMESTAMP`,
        [chatId, newVal]
      ).catch(() => {});
      return showClientNotificationSettings(chatId);
    }

    // ── Настройки клиента
    if (data === 'client_settings') return showClientSettings(chatId);

    if (data === 'client_settings_privacy') {
      const prefs = (await get('SELECT profile_hidden FROM client_prefs WHERE chat_id=?', [chatId]).catch(
        () => null
      )) || { profile_hidden: 0 };
      const newVal = prefs.profile_hidden ? 0 : 1;
      await run(
        `INSERT INTO client_prefs (chat_id, profile_hidden) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET profile_hidden=excluded.profile_hidden, updated_at=CURRENT_TIMESTAMP`,
        [chatId, newVal]
      ).catch(() => {});
      return showClientSettings(chatId);
    }

    if (data === 'client_settings_lang') {
      const langEnabled = await getSetting('bot_language').catch(() => null);
      if (langEnabled !== 'multi') {
        return safeSend(chatId, '🌐 Мультиязычность пока недоступна\\.\n\nСледите за обновлениями\\!', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'client_settings' }]] },
        });
      }
      return safeSend(chatId, '🌐 *Язык интерфейса*\n\nВыберите язык:', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '🇷🇺 Русский ✓', callback_data: 'noop' }],
            [{ text: '🇬🇧 English — Coming soon', callback_data: 'noop' }],
            [{ text: '← Назад', callback_data: 'client_settings' }],
          ],
        },
      });
    }

    if (data === 'client_settings_delete') {
      return safeSend(
        chatId,
        '⚠️ *Удаление аккаунта*\n\n' +
          'Все ваши данные \\(профиль, история заявок, баллы\\) будут удалены\\.\n' +
          'Это действие *необратимо*\\.\n\n' +
          'Подтвердите удаление:',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '🗑 Да, удалить аккаунт', callback_data: 'client_settings_delete_confirm' }],
              [{ text: '❌ Отмена', callback_data: 'client_settings' }],
            ],
          },
        }
      );
    }

    if (data === 'client_settings_delete_confirm') {
      try {
        await run('DELETE FROM client_prefs WHERE chat_id=?', [chatId]).catch(() => {});
        await run('DELETE FROM sessions WHERE chat_id=?', [chatId]).catch(() => {});
        await run('UPDATE orders SET client_chat_id=NULL WHERE client_chat_id=?', [chatId]).catch(() => {});
        await run('DELETE FROM wishlists WHERE chat_id=?', [chatId]).catch(() => {});
        return safeSend(
          chatId,
          '🗑 Ваш аккаунт удалён\\.\n\nСпасибо, что пользовались нашим сервисом\\!\n' +
            'Для новой регистрации просто напишите /start\\.',
          { parse_mode: 'MarkdownV2' }
        );
      } catch (e) {
        return safeSend(chatId, '⚠️ Не удалось удалить аккаунт\\. Попробуйте позже\\.', { parse_mode: 'MarkdownV2' });
      }
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
        reply_markup: {
          inline_keyboard: [
            [{ text: '📋 Оформить заявку', callback_data: `bk_model_${modelId}` }],
            [{ text: '📞 Менеджер', callback_data: 'msg_manager_start' }],
            [{ text: '← К модели', callback_data: `cat_model_${modelId}` }],
          ],
        },
      });
    }

    // ── FAQ: отдельный вопрос
    if (data.startsWith('faq_')) {
      const faqId = parseInt(data.replace('faq_', ''));
      const faq = await get('SELECT * FROM faq WHERE id=? AND active=1', [faqId]).catch(() => null);
      if (!faq) return;
      return safeSend(chatId, `*${esc(faq.question)}*\n\n${esc(faq.answer)}`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '← Все вопросы', callback_data: 'faq' }],
            [{ text: '📋 Оформить заявку', callback_data: 'bk_start' }],
          ],
        },
      });
    }

    // ── Отмена незавершённой заявки (из напоминания)
    if (data === 'cancel_booking') {
      await clearSession(chatId);
      return showMainMenu(chatId, q.from.first_name);
    }
  });

  // ── Photo handler (для загрузки фото модели через бот) ──────────────────
  bot.on('photo', async msg => {
    const chatId = msg.chat.id;
    if (!isAdmin(chatId)) return;
    const session = await getSession(chatId);
    const state = session?.state || 'idle';
    const d = sessionData(session);
    const fileId = msg.photo[msg.photo.length - 1].file_id;

    if (state === 'adm_mdl_photo') {
      d.photo_file_id = fileId;
      d._step = 'confirm';
      return showAddModelStep(chatId, d);
    }
    if (state.startsWith('adm_gallery_')) {
      const modelId = parseInt(state.replace('adm_gallery_', ''));
      const m = await get('SELECT photo_main, photos FROM models WHERE id=?', [modelId]).catch(() => null);
      if (!m) return safeSend(chatId, '❌ Модель не найдена.');
      let gallery = [];
      try {
        gallery = JSON.parse(m.photos || '[]');
      } catch {}
      const all = m.photo_main ? [m.photo_main, ...gallery] : gallery;
      const maxPhotos = Math.min(20, Math.max(1, parseInt(await getSetting('model_max_photos').catch(() => '8')) || 8));
      if (all.length >= maxPhotos) {
        return safeSend(chatId, `⚠️ Максимум ${maxPhotos} фото. Сначала нажмите «Очистить».`);
      }
      if (!m.photo_main) {
        await run('UPDATE models SET photo_main=? WHERE id=?', [fileId, modelId]).catch(() => {});
      } else {
        gallery.push(fileId);
        await run('UPDATE models SET photos=? WHERE id=?', [JSON.stringify(gallery), modelId]).catch(() => {});
      }
      const newCount = all.length + 1;
      const remaining = maxPhotos - newCount;
      const doneText =
        remaining > 0
          ? `✅ Фото ${newCount}/${maxPhotos} сохранено!\n\nМожно добавить ещё ${remaining} фото.`
          : `✅ Фото ${newCount}/${maxPhotos} — галерея заполнена!`;
      const buttons = [];
      if (remaining > 0) {
        buttons.push([
          { text: `➕ Добавить ещё фото (${newCount}/${maxPhotos})`, callback_data: `adm_gallery_${modelId}` },
        ]);
      }
      buttons.push([{ text: '✅ Готово — показать карточку', callback_data: `adm_model_${modelId}` }]);
      buttons.push([{ text: '🗑 Очистить все фото', callback_data: `adm_gallery_clear_${modelId}` }]);
      return safeSend(chatId, doneText, { reply_markup: { inline_keyboard: buttons } });
    }
    if (state.startsWith('adm_ef_') && state.endsWith('_photo')) {
      const modelId = parseInt(state.replace('adm_ef_', '').split('_')[0]);
      await clearSession(chatId);
      return showPhotoGalleryManager(chatId, modelId);
    }
    // ── Broadcast: admin sends photo directly while in text-input state
    // This lets admin skip the text step and jump straight to photo+caption flow
    if (state === 'adm_broadcast_msg' || state === 'adm_broadcast_edit_text') {
      const caption = (msg.caption || '').trim();
      const segment = d.broadcastSegment || 'all';
      const clients = await _getBroadcastClients(segment);
      if (!clients.length) {
        return safeSend(chatId, '❌ Нет клиентов для рассылки\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] },
        });
      }
      const newSd = {
        ...d,
        broadcastRecipients: clients.map(c => c.client_chat_id),
        broadcastPhotoId: fileId,
        broadcastText: caption,
        broadcastSegment: segment,
      };
      await setSession(chatId, 'adm_broadcast_preview', newSd);
      return previewBroadcast(chatId);
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
      return safeSend(
        chatId,
        `✅ Фото получено\\!\n\nТеперь введите подпись к рассылке \\(или нажмите «Пропустить»\\):`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '⏭ Пропустить подпись', callback_data: 'adm_broadcast_photo_nosend' }],
              [{ text: '❌ Отмена', callback_data: 'admin_menu' }],
            ],
          },
        }
      );
    }
  });

  // ── Message handler ────────────────────────────────────────────────────────
  bot.on('message', async msg => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId = msg.chat.id;
    const text = msg.text.trim();

    // ── Block check: silently ignore messages from blocked users ─────────────
    const isBlockedUser =
      !isAdmin(chatId) &&
      !!(await get(`SELECT chat_id FROM blocked_clients WHERE chat_id=?`, [chatId]).catch(() => null));
    if (isBlockedUser) return;

    const session = await getSession(chatId);
    const state = session?.state || 'idle';
    const d = sessionData(session);

    // ── Session timeout: предупреждение если сессия протухла ──────────────────
    if (state !== 'idle' && session?.updated_at) {
      const updatedAt = new Date(session.updated_at).getTime();
      if (!isNaN(updatedAt) && Date.now() - updatedAt > SESSION_TIMEOUT_MS) {
        if (state.startsWith('bk_')) {
          // Booking session > timeout — warn and ask to continue or restart
          return safeSend(
            chatId,
            '⏰ *Сессия бронирования неактивна более 30 минут\\.*\n\nПродолжить с того же места или начать заново?',
            {
              parse_mode: 'MarkdownV2',
              reply_markup: {
                inline_keyboard: [
                  [{ text: '▶ Продолжить', callback_data: 'session_continue' }],
                  [{ text: '🔄 Начать заново', callback_data: 'session_restart' }],
                  [{ text: '❌ Отменить /cancel', callback_data: 'cancel_booking' }],
                ],
              },
            }
          );
        }
        // Non-booking session expired — reset and show appropriate menu
        clearTimeout(sessionTimers.get(chatId));
        sessionTimers.delete(chatId);
        clearSessionWarning(chatId);
        clearSessionReminder(chatId);
        await clearSession(chatId);
        await safeSend(
          chatId,
          '⏰ *Сессия истекла\\. Действие отменено\\.*\n\nНапишите /start или нажмите кнопку ниже, чтобы продолжить\\.',
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]],
            },
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
        if (text === '⭐ Топ-модели') return showTopModels(chatId, 0);
        if (text === '💃 Каталог') return showCatalog(chatId, null, 0);
        if (text === '📝 Подать заявку') return bkStep1(chatId);
        if (text === '⚡ Быстрая заявка') return bkQuickStart(chatId);
        if (text === '❤️ Избранное') return showWishlist(chatId, 0);
        if (text === '💬 Менеджер') return showContactManager(chatId);
        if (text === '📋 Мои заявки') return showMyOrders(chatId);
        if (text === '🔍 Статус заявки') {
          await setSession(chatId, 'check_status', {});
          return safeSend(chatId, '🔍 Введите номер заявки (например, НМ-001):');
        }
        if (text === '💰 Прайс') return showPricing(chatId);
        if (text === '❓ FAQ') return showFaq(chatId);
        if (text === '👤 Профиль') return showUserProfile(chatId, msg.from.first_name);
        if (text === '📞 Контакты') return showContacts(chatId);
        if (text === '📋 Тех. задание') return startTechSpec(chatId);
      }
      // Кнопки администратора
      if (isAdmin(chatId)) {
        if (text === '📋 Заявки') return showAdminOrders(chatId, '', 0);
        if (text === '💃 Модели') return showAdminModels(chatId, 0);
        if (text === '📊 Статистика') return showAdminStats(chatId);
        if (text === '🤖 Организм') return showOrganismStatus(chatId);
        if (text === '📡 Фид агентов') return showAgentFeed(chatId, 0);
        if (text === '💬 Обсуждения') return showAgentDiscussions(chatId);
        if (text === '⚙️ Настройки') return showAdminSettings(chatId);
        if (text === '📢 Рассылка') return showBroadcast(chatId);
        if (text === '📤 Экспорт') return exportOrders(chatId);
        if (text === '👥 Клиенты') return showAdminClients(chatId, 0);
      }
    }

    // ── Admin: settings text inputs
    if (isAdmin(chatId)) {
      const settingStates = {
        adm_set_greeting: ['greeting', '📝 Приветствие обновлено!'],
        adm_set_about: ['about', 'ℹ️ Текст «О нас» обновлён!'],
        adm_set_phone: ['contacts_phone', '📞 Телефон обновлён!'],
        adm_set_email: ['contacts_email', '📧 Email обновлён!'],
        adm_set_insta: ['contacts_insta', '📸 Instagram обновлён!'],
        adm_set_instagram: ['contacts_instagram', '📸 Instagram обновлён!'],
        adm_set_addr: ['contacts_addr', '📍 Адрес обновлён!'],
        adm_set_pricing: ['pricing', '💰 Прайс-лист обновлён!'],
        adm_set_whatsapp: ['contacts_whatsapp', '📱 WhatsApp обновлён!'],
        adm_set_site_url: ['site_url', '🌐 URL сайта обновлён!'],
        adm_set_mgr_hours: ['manager_hours', '🕐 Часы работы обновлены!'],
        adm_set_mgr_reply: ['manager_reply', '💬 Авто-ответ обновлён!'],
        adm_set_catalog_per_page: ['catalog_per_page', '📄 Кол-во на странице обновлено!'],
        adm_set_catalog_title: ['catalog_title', '📌 Заголовок каталога обновлён!'],
        adm_set_booking_min_budget: ['booking_min_budget', '💰 Мин. бюджет обновлён!'],
        adm_set_booking_confirm_msg: ['booking_confirm_msg', '💬 Сообщение брони обновлено!'],
        adm_set_booking_thanks: ['booking_thanks_text', '🎉 Текст после бронирования обновлён!'],
        adm_set_tg_channel: ['tg_channel', '📣 Telegram канал обновлён!'],
        adm_set_reviews_min: ['reviews_min_completed', '🔢 Мин. заявок обновлено!'],
        adm_set_reviews_prompt: ['reviews_prompt_text', '📝 Приглашение к отзыву обновлено!'],
        adm_set_cities_list: ['cities_list', '🏙 Список городов обновлён!'],
        adm_set_welcome_photo: ['welcome_photo_url', '🖼 Фото приветствия обновлено!'],
        adm_set_main_menu_text: ['main_menu_text', '📋 Текст меню обновлён!'],
        adm_set_model_max_photos: ['model_max_photos', '🖼 Лимит фото обновлён!'],
        adm_set_client_max_orders: ['client_max_active_orders', '📋 Лимит заявок обновлён!'],
        adm_set_client_msg_delay: ['client_msg_delay_sec', '⏱ Интервал сообщений обновлён!'],
        adm_set_api_rate_limit: ['api_rate_limit', '🔒 Rate limit обновлён!'],
      };
      if (settingStates[state]) {
        const [key, okMsg] = settingStates[state];
        await setSetting(key, text);
        await logAdminAction(chatId, 'update_setting', 'setting', null, { key });
        await clearSession(chatId);
        return safeSend(chatId, `✅ ${okMsg}`, {
          reply_markup: { inline_keyboard: [[{ text: '⚙️ К настройкам', callback_data: 'adm_settings' }]] },
        });
      }

      // ── Add admin Telegram ID
      if (state === 'adm_add_admin_id') {
        const newId = text.replace(/[^0-9]/g, '');
        if (!newId) return safeSend(chatId, '❌ Некорректный ID. Введите числовой Telegram ID:');
        await run(
          'UPDATE admins SET telegram_id=? WHERE id=(SELECT MIN(id) FROM admins WHERE telegram_id IS NULL OR telegram_id="")',
          [newId]
        ).catch(() => {});
        await clearSession(chatId);
        return safeSend(
          chatId,
          `✅ Telegram ID \`${newId}\` добавлен!\n\n⚠️ Для постоянного добавления — также добавьте его в ADMIN_TELEGRAM_IDS в .env файле.`,
          {
            reply_markup: { inline_keyboard: [[{ text: '← Администраторы', callback_data: 'adm_admins' }]] },
          }
        );
      }

      // ── Scheduled broadcast: step 1 — text input
      if (state === 'adm_sched_bcast_text') {
        if (!text || text.length < 2) return safeSend(chatId, '❌ Текст слишком короткий. Введите текст рассылки:');
        await setSession(chatId, 'adm_sched_bcast_time', { sched_text: text });
        return safeSend(
          chatId,
          `📅 Текст принят\\!\n\nВведите дату и время рассылки в формате:\n\`2026\\-05\\-20 14:00\``,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'adm_sched_bcast' }]] },
          }
        );
      }

      // ── Scheduled broadcast: step 2 — time input
      if (state === 'adm_sched_bcast_time') {
        const timeMatch = text.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})$/);
        if (!timeMatch) {
          return safeSend(chatId, '❌ Неверный формат\\. Введите дату в формате `2026-05-20 14:00`:', {
            parse_mode: 'MarkdownV2',
          });
        }
        const scheduledAt = `${timeMatch[1]} ${timeMatch[2]}:00`;
        const sessData = { ...d, sched_time: scheduledAt };
        await setSession(chatId, 'adm_sched_bcast_segment', sessData);
        return safeSend(chatId, `⏰ Время: *${esc(scheduledAt)}*\n\nВыберите сегмент получателей:`, {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '👥 Все клиенты', callback_data: 'adm_sched_bcast_seg_all' }],
              [{ text: '✅ Завершившие заявку', callback_data: 'adm_sched_bcast_seg_completed' }],
              [{ text: '▶️ Активные клиенты', callback_data: 'adm_sched_bcast_seg_active' }],
              [{ text: '❌ Отмена', callback_data: 'adm_sched_bcast' }],
            ],
          },
        });
      }

      // ── Broadcast: schedule time input (from preview → 🕐 Запланировать)
      if (state === 'broadcast_schedule_time') {
        // Accept DD.MM.YYYY HH:MM format
        const m2 = text.match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})$/);
        if (!m2) {
          return safeSend(
            chatId,
            '❌ Неверный формат\\. Введите дату как `ДД\\.ММ\\.ГГГГ ЧЧ:ММ` \\(например `20\\.05\\.2026 14:00`\\):',
            { parse_mode: 'MarkdownV2' }
          );
        }
        const scheduledAt = new Date(`${m2[3]}-${m2[2]}-${m2[1]}T${m2[4]}:${m2[5]}:00`);
        if (isNaN(scheduledAt.getTime()) || scheduledAt <= new Date()) {
          return safeSend(chatId, '❌ Дата должна быть в будущем\\. Введите корректную дату и время:', {
            parse_mode: 'MarkdownV2',
          });
        }
        const scheduledAtStr = `${m2[3]}-${m2[2]}-${m2[1]} ${m2[4]}:${m2[5]}:00`;
        const segment = d.broadcastSegment || 'all';
        const bcText = d.broadcastText || '';
        const photoUrl = d.broadcastPhotoId || null;
        await run(
          `INSERT INTO scheduled_broadcasts (text, scheduled_at, photo_url, segment, status) VALUES (?,?,?,?,'pending')`,
          [bcText, scheduledAtStr, photoUrl, segment]
        ).catch(() => {});
        await clearSession(chatId);
        const displayDt = `${m2[1]}.${m2[2]}.${m2[3]} ${m2[4]}:${m2[5]}`;
        return safeSend(
          chatId,
          `✅ *Рассылка запланирована на ${esc(displayDt)}*\n\nСегмент: *${esc(_bcSegmentLabel(segment))}*`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [{ text: '📅 Запланированные рассылки', callback_data: 'adm_bc_scheduled' }],
                [{ text: '← Меню', callback_data: 'admin_menu' }],
              ],
            },
          }
        );
      }

      // ── Broadcast text (initial entry — no photo yet)
      if (state === 'adm_broadcast_msg') {
        return sendBroadcast(chatId, text, false);
      }

      // ── Broadcast text edit from preview (preserve existing photo)
      if (state === 'adm_broadcast_edit_text') {
        return sendBroadcast(chatId, text, true);
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

      // ── Admin order search by number input
      if (state === 'adm_order_search_input') {
        return handleAdminOrderSearchInput(chatId, text);
      }

      // ── Admin search notes input
      if (state === 'adm_search_notes_input') {
        return searchAdminNotes(chatId, text);
      }

      // ── Admin search model input
      if (state === 'adm_search_model_input') {
        const q2 = text.trim();
        await clearSession(chatId);
        const results = await query(`SELECT * FROM models WHERE name LIKE ? AND archived=0 LIMIT 10`, [`%${q2}%`]);
        if (!results.length)
          return safeSend(chatId, '❌ Модели не найдены\\.', {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '← Список моделей', callback_data: 'adm_models_p_0_name_0' }]] },
          });
        const keyboard2 = results.map(m => [
          {
            text: `${m.featured ? '⭐' : ''}${m.name} (${m.city || 'город не указан'})`,
            callback_data: `adm_model_${m.id}`,
          },
        ]);
        keyboard2.push([{ text: '← Список моделей', callback_data: 'adm_models_p_0_name_0' }]);
        return safeSend(chatId, `🔍 Найдено ${results.length} моделей по запросу "*${esc(q2)}*":`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: keyboard2 },
        });
      }

      // ── Order note input
      if (state.startsWith('adm_note_input_')) {
        const orderId = parseInt(state.replace('adm_note_input_', ''));
        if (!orderId) {
          await clearSession(chatId);
          return;
        }
        await run('INSERT INTO order_notes (order_id, admin_note) VALUES (?,?)', [orderId, text]);
        await clearSession(chatId);
        return safeSend(chatId, `✅ Заметка добавлена\\.`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] },
        });
      }

      // ── Internal note for order (adm_order_note_ flow)
      if (state === 'adm_note_order_id') {
        const orderId = d?.orderId;
        if (!orderId) {
          await clearSession(chatId);
          return;
        }
        const trimmed = text.slice(0, 1000);
        await run('UPDATE orders SET internal_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=?', [trimmed, orderId]);
        await clearSession(chatId);
        return safeSend(chatId, `✅ *Заметка сохранена\\!*\n\n${esc(trimmed)}`, {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${orderId}` }]] },
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
          await safeSend(chatId, `❌ Не удалось отправить \\(клиент мог заблокировать бота\\)\\.`, {
            parse_mode: 'MarkdownV2',
          });
        }
        return;
      }
    }

    // ── Admin: add busy period input
    if (isAdmin(chatId) && state.startsWith('adm_add_busy_')) {
      const modelId = parseInt(state.replace('adm_add_busy_', ''));
      await clearSession(chatId);
      if (!modelId) return;

      // Parse input: "dd.mm.yyyy[-dd.mm.yyyy] [reason]"
      // e.g. "15.05.2026-20.05.2026 Съёмка Nike" or "15.05.2026 Съёмка"
      const match = text.trim().match(/^(\d{2}\.\d{2}\.\d{4})(?:-(\d{2}\.\d{2}\.\d{4}))?\s*(.*)?$/);
      if (!match) {
        return safeSend(
          chatId,
          '❌ Неверный формат\\. Введите дату в виде: `15\\.05\\.2026` или `15\\.05\\.2026\\-20\\.05\\.2026 Причина`',
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '← Отмена', callback_data: `adm_model_cal_${modelId}` }]] },
          }
        );
      }

      function parseDMY(str) {
        const [dd, mm, yyyy] = str.split('.');
        return `${yyyy}-${mm.padStart(2, '0')}-${dd.padStart(2, '0')}`;
      }

      const dateFrom = parseDMY(match[1]);
      const dateTo = match[2] ? parseDMY(match[2]) : dateFrom;
      const reason = (match[3] || '').trim() || null;

      // Validate dates
      const fromD = new Date(dateFrom);
      const toD = new Date(dateTo);
      if (isNaN(fromD.getTime()) || isNaN(toD.getTime()) || fromD > toD) {
        return safeSend(chatId, '❌ Неверные даты\\. Проверьте формат и порядок дат\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: `adm_model_cal_${modelId}` }]] },
        });
      }

      // Insert all dates in range
      let inserted = 0;
      const cur = new Date(fromD);
      while (cur <= toD) {
        const dateStr = cur.toISOString().slice(0, 10);
        await run('INSERT OR IGNORE INTO model_busy_dates (model_id, busy_date, reason) VALUES (?,?,?)', [
          modelId,
          dateStr,
          reason,
        ]).catch(() => {});
        inserted++;
        cur.setDate(cur.getDate() + 1);
      }

      const rangeStr =
        dateFrom === dateTo ? formatDateShort(dateFrom) : `${formatDateShort(dateFrom)}–${formatDateShort(dateTo)}`;
      await safeSend(
        chatId,
        `✅ Добавлено *${inserted}* ${inserted === 1 ? 'дата' : 'дней'}: *${rangeStr}*${reason ? `\nПричина: ${esc(reason)}` : ''}`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '📅 К расписанию', callback_data: `adm_model_cal_${modelId}` }]] },
        }
      );
      return;
    }

    // ── Admin: add model text inputs
    if (isAdmin(chatId) && state.startsWith('adm_mdl_')) {
      const step = state.replace('adm_mdl_', '');
      if (step === 'name') {
        if (text.length < 2) return safeSend(chatId, '❌ Имя слишком короткое. Введите имя модели:');
        d.name = text;
        d._step = 'age';
        return showAddModelStep(chatId, d);
      }
      if (step === 'age') {
        d.age = parseInt(text) || null;
        d._step = 'height';
        return showAddModelStep(chatId, d);
      }
      if (step === 'height') {
        d.height = parseInt(text) || null;
        d._step = 'params';
        return showAddModelStep(chatId, d);
      }
      if (step === 'params') {
        const parts = text.split('/').map(x => parseInt(x.trim()));
        if (parts.length === 3 && parts.every(Boolean)) {
          [d.bust, d.waist, d.hips] = parts;
        }
        d._step = 'shoe';
        return showAddModelStep(chatId, d);
      }
      if (step === 'shoe') {
        d.shoe_size = text;
        d._step = 'hair';
        return showAddModelStep(chatId, d);
      }
      if (step === 'instagram') {
        d.instagram = text.replace('@', '');
        d._step = 'bio';
        return showAddModelStep(chatId, d);
      }
      if (step === 'bio') {
        d.bio = text;
        d._step = 'photo';
        return showAddModelStep(chatId, d);
      }
    }

    // ── Admin: edit model field input
    if (isAdmin(chatId) && state.startsWith('adm_ef_')) {
      // state: adm_ef_{id}_{field}
      const parts = state.replace('adm_ef_', '').split('_');
      const modelId = parseInt(parts[0]);
      const field = parts.slice(1).join('_');
      const fieldMap = {
        name: 'name',
        age: 'age',
        height: 'height',
        weight: 'weight',
        shoe_size: 'shoe_size',
        instagram: 'instagram',
        bio: 'bio',
        eye_color: 'eye_color',
        hair_color: 'hair_color',
        phone: 'phone',
        city: 'city',
        video_url: 'video_url',
      };
      if (field === 'params') {
        const ps = text.split('/').map(x => parseInt(x.trim()));
        if (ps.length === 3 && ps.every(Boolean)) {
          await run('UPDATE models SET bust=?,waist=?,hips=?,updated_at=CURRENT_TIMESTAMP WHERE id=?', [
            ps[0],
            ps[1],
            ps[2],
            modelId,
          ]).catch(() => {});
        }
      } else if (fieldMap[field] && /^[a-z_]+$/.test(fieldMap[field])) {
        const col = fieldMap[field];
        const val = ['age', 'height', 'weight'].includes(field) ? parseInt(text) || null : text;
        await run(`UPDATE models SET ${col}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, [val, modelId]).catch(() => {});
      }
      await clearSession(chatId);
      return safeSend(chatId, '✅ Поле обновлено!', {
        reply_markup: {
          inline_keyboard: [
            [
              { text: '✏️ Редактировать ещё', callback_data: `adm_editmodel_${modelId}` },
              { text: '← Карточка', callback_data: `adm_model_${modelId}` },
            ],
          ],
        },
      });
    }

    // ── Admin reply to client
    if (isAdmin(chatId) && state === 'replying' && d.order_id) {
      const order = await get('SELECT * FROM orders WHERE id=?', [d.order_id]).catch(() => null);
      if (!order) {
        await clearSession(chatId);
        return safeSend(chatId, RU.ORDER_NOT_FOUND);
      }
      const adm = await get('SELECT username FROM admins WHERE telegram_id=?', [String(chatId)]).catch(() => null);
      await run('INSERT INTO messages (order_id,sender_type,sender_name,content) VALUES (?,?,?,?)', [
        d.order_id,
        'admin',
        adm?.username || 'Менеджер',
        text,
      ]);
      if (order.client_chat_id) await sendMessageToClient(order.client_chat_id, order.order_number, text);
      await clearSession(chatId);
      return safeSend(chatId, `✅ Сообщение отправлено клиенту ${order.client_name}.`, {
        reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${d.order_id}` }]] },
      });
    }

    // ── Leave review: text input
    if (state === 'leave_review_text') {
      if (!text) {
        return safeSend(chatId, '❌ Введите текст отзыва или отправьте «.» чтобы пропустить:');
      }
      // Allow "." as a shortcut to skip writing text
      const reviewText = text.trim() === '.' ? '' : text.trim();
      if (reviewText && reviewText.length < 20) {
        return safeSend(
          chatId,
          '❌ Отзыв слишком короткий. Напишите не менее 20 символов или отправьте «.» чтобы пропустить:'
        );
      }
      const orderId = d.review_order_id;
      const rating = d.review_rating || 5;
      let clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      let modelId = null;
      try {
        const ord = await get('SELECT client_name, model_id FROM orders WHERE id=?', [orderId]);
        if (ord?.client_name) clientName = ord.client_name;
        if (ord?.model_id) modelId = ord.model_id;
      } catch {}
      const autoApprove = await getSetting('reviews_auto_approve').catch(() => '0');
      const approvedVal = autoApprove === '1' ? 1 : 0;
      const insertResult = await run(
        'INSERT OR IGNORE INTO reviews (chat_id, order_id, client_name, rating, text, model_id, approved) VALUES (?,?,?,?,?,?,?)',
        [String(chatId), orderId || null, clientName, rating, reviewText, modelId, approvedVal]
      ).catch(e => {
        console.error('[Bot] insert review:', e.message);
        return null;
      });
      const newReviewId = insertResult?.id || null;
      await clearSession(chatId);

      // Bonus points for good review (rating 4-5)
      let reviewBonusMsg = '';
      if (rating >= 4) {
        await addLoyaltyPoints(chatId, 100, 'review', 'Бонус за отзыв').catch(() => {});
        reviewBonusMsg = '\n\n🎁 *\\+100 баллов* начислено за отзыв\\!';
      }
      // Grant "first_review" achievement
      await grantAchievement(chatId, 'first_review').catch(() => {});

      const notifReview = await getSetting('notif_new_review').catch(() => '1');
      const adminIds2 = await getAdminChatIds();
      if (notifReview !== '0') {
        const reviewPreview = reviewText ? `\n\n_${esc(reviewText.substring(0, 200))}_` : ' _(текст не указан)_';
        const adminReviewBtns = newReviewId
          ? [
              [
                { text: '✅ Одобрить', callback_data: `rev_approve_${newReviewId}` },
                { text: '❌ Отклонить', callback_data: `rev_reject_${newReviewId}` },
              ],
            ]
          : [[{ text: '✅ Модерация отзывов', callback_data: 'adm_reviews' }]];
        const adminNotifyText = `⭐ *Новый отзыв от ${esc(clientName)}\\!*\nОценка: ${rating}⭐${reviewPreview}\n\n_Перейдите в раздел отзывов для модерации\\._`;
        await Promise.allSettled(
          adminIds2.map(id =>
            safeSend(id, adminNotifyText, {
              parse_mode: 'MarkdownV2',
              reply_markup: { inline_keyboard: adminReviewBtns },
            })
          )
        );
      }
      return safeSend(chatId, `✅ Спасибо за отзыв\\! Он появится на сайте после проверки\\.${reviewBonusMsg}`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
      });
    }

    // ── AI підбір моделей: опис замовлення
    if (state === 'ai_match_desc') {
      const desc = text ? text.trim() : '';
      if (desc.length < 10)
        return safeSend(chatId, 'Опишіть детальніше \\(мінімум 10 символів\\):', { parse_mode: 'MarkdownV2' });
      if (desc.length > 500)
        return safeSend(chatId, '❌ Опис занадто довгий \\(максимум 500 символів\\)\\.', { parse_mode: 'MarkdownV2' });

      await clearSession(chatId);
      await safeSend(chatId, '🤖 Аналізую ваш запит та підбираю моделей\\.\\.\\.', { parse_mode: 'MarkdownV2' });
      return runAiMatch(chatId, desc);
    }

    // ── Edit profile name
    if (state === 'profile_edit_name') {
      if (!text || text.trim().length < 2) {
        return safeSend(chatId, STRINGS.errorInvalidName);
      }
      if (text.trim().length > 50) {
        return safeSend(chatId, STRINGS.errorNameTooLong);
      }
      const newName = text.trim().slice(0, 50);
      await run(
        `INSERT INTO client_prefs (chat_id, name) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, updated_at=CURRENT_TIMESTAMP`,
        [chatId, newName]
      ).catch(() => {});
      await clearSession(chatId);
      await safeSend(chatId, `✅ Имя обновлено: *${esc(newName)}*`, { parse_mode: 'MarkdownV2' });
      return showUserProfile(chatId, newName);
    }

    // ── Edit profile phone
    if (state === 'profile_edit_phone') {
      if (!text || !/^[\d\s+\-()]{7,20}$/.test(text.trim())) {
        return safeSend(chatId, STRINGS.errorInvalidPhone);
      }
      const newPhone = text.trim().slice(0, 20);
      await run(
        `INSERT INTO client_prefs (chat_id, phone) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET phone=excluded.phone, updated_at=CURRENT_TIMESTAMP`,
        [chatId, newPhone]
      ).catch(() => {});
      await clearSession(chatId);
      await safeSend(chatId, `✅ Телефон обновлён: *${esc(newPhone)}*`, { parse_mode: 'MarkdownV2' });
      return showUserProfile(chatId);
    }

    // ── Edit profile email
    if (state === 'profile_edit_email') {
      if (!text || !text.trim().includes('@')) {
        return safeSend(chatId, STRINGS.errorInvalidEmail);
      }
      const newEmail = text.trim().slice(0, 200);
      await run(
        `INSERT INTO client_prefs (chat_id, email) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET email=excluded.email, updated_at=CURRENT_TIMESTAMP`,
        [chatId, newEmail]
      ).catch(() => {});
      await clearSession(chatId);
      await safeSend(chatId, `✅ Email обновлён: *${esc(newEmail)}*`, { parse_mode: 'MarkdownV2' });
      return showUserProfile(chatId);
    }

    // ── Status check
    if (state === 'check_status') {
      return showOrderStatus(chatId, text);
    }

    // ── Booking text inputs
    switch (state) {
      case 'bk_s2_date': {
        if (!text || text.length < 3) return safeSend(chatId, '❌ Введите дату мероприятия:');
        {
          const dmyFmt = text.trim().match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/);
          if (!dmyFmt) {
            return safeSend(chatId, '❌ Неверный формат\\. Введите дату в виде *ДД\\.ММ\\.ГГГГ*', {
              parse_mode: 'MarkdownV2',
              reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] },
            });
          }
          const dv = parseInt(dmyFmt[1]);
          const mv = parseInt(dmyFmt[2]);
          if (mv < 1 || mv > 12 || dv < 1 || dv > 31) {
            return safeSend(chatId, '❌ Неверная дата\\. Проверьте день и месяц\\.', {
              parse_mode: 'MarkdownV2',
              reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] },
            });
          }
        }
        // Check if selected model is busy on this date
        if (d.model_id) {
          // Try to parse date entered as dd.mm.yyyy → YYYY-MM-DD for DB lookup
          const dmyMatch = text.trim().match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/);
          if (dmyMatch) {
            const isoDate = `${dmyMatch[3]}-${dmyMatch[2].padStart(2, '0')}-${dmyMatch[1].padStart(2, '0')}`;
            const busyRow = await get('SELECT id FROM model_busy_dates WHERE model_id=? AND busy_date=?', [
              d.model_id,
              isoDate,
            ]).catch(() => null);
            if (busyRow) {
              return safeSend(chatId, '⚠️ Модель занята в этот день\\. Выберите другую дату или другую модель\\.', {
                parse_mode: 'MarkdownV2',
                reply_markup: {
                  inline_keyboard: [
                    [{ text: '← Выбрать другую модель', callback_data: 'bk_start' }],
                    [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
                  ],
                },
              });
            }
          }
        }
        d.event_date = text;
        return bkStep2Duration(chatId, d);
      }

      case 'bk_s2_loc':
        if (!text || !text.trim()) return safeSend(chatId, '❌ Введите место проведения:');
        d.location = text.trim();
        return bkStep2Budget(chatId, d);

      case 'bk_s2_budget': {
        d.budget = text;
        // Check against booking_min_budget setting
        const minBudgetRaw = await getSetting('booking_min_budget').catch(() => null);
        const minBudget = minBudgetRaw ? parseInt(String(minBudgetRaw).replace(/\D/g, '')) : 0;
        if (minBudget > 0) {
          // Extract numeric value from entered budget string
          const enteredNum = parseInt(String(text).replace(/\D/g, '')) || 0;
          if (enteredNum > 0 && enteredNum < minBudget) {
            await setSession(chatId, 'bk_s2_budget', d);
            return safeSend(
              chatId,
              `⚠️ Рекомендуемый бюджет от *${esc(minBudget.toLocaleString('ru-RU'))} ₽*\\.\n\nХотите продолжить с указанным бюджетом?`,
              {
                parse_mode: 'MarkdownV2',
                reply_markup: {
                  inline_keyboard: [
                    [
                      { text: '✅ Да, продолжить', callback_data: 'bk_budget_continue' },
                      { text: '🔄 Изменить бюджет', callback_data: 'bk_budget_change' },
                    ],
                    [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
                  ],
                },
              }
            );
          }
        }
        return bkStep2Comments(chatId, d);
      }

      case 'bk_s2_comments':
        d.comments = text;
        return bkStep3Name(chatId, d);

      case 'bk_s3_name':
        if (text.length < 2) return safeSend(chatId, '❌ Введите имя и фамилию:');
        d.client_name = text;
        return bkStep3Phone(chatId, d);

      case 'bk_s3_phone':
        if (!/^[\d\s+\-()]{7,20}$/.test(text))
          return safeSend(
            chatId,
            '❌ Формат номера неверный\\. Введите номер в формате: \\+7 999 123\\-45\\-67 или 89991234567',
            {
              parse_mode: 'MarkdownV2',
              reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] },
            }
          );
        d.client_phone = text;
        return bkStep3Email(chatId, d);

      case 'bk_s3_email': {
        const requireEmailVal = await getSetting('booking_require_email').catch(() => '0');
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text)) {
          const errKb =
            requireEmailVal === '1'
              ? [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]]
              : [
                  [{ text: '⏭ Пропустить', callback_data: 'bk_skip_email' }],
                  [{ text: '❌ Отменить', callback_data: 'bk_cancel' }],
                ];
          return safeSend(chatId, STRINGS.bookingErrorEmail, {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: errKb },
          });
        }
        d.client_email = text;
        return bkStep3Telegram(chatId, d, msg.from.username);
      }

      case 'bk_s3_tg':
        d.client_telegram = text.replace('@', '');
        return bkStep4Confirm(chatId, d);

      default:
        // unknown booking state — handled by fallthrough logic below
        break;
    }

    // ── Вопрос менеджеру (через кнопку "Написать менеджеру")
    if (state === 'msg_to_manager') {
      const clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      const username = msg.from.username ? `@${msg.from.username}` : '';
      const adminIds = await getAdminChatIds();
      await Promise.allSettled(
        adminIds.map(id =>
          safeSend(
            id,
            `💬 *Вопрос менеджеру*\nОт: ${esc(clientName)} ${esc(username)}\nTelegram ID: ${chatId}\n\n${esc(text)}`,
            {
              parse_mode: 'MarkdownV2',
              reply_markup: { inline_keyboard: [[{ text: '💬 Ответить', callback_data: `direct_reply_${chatId}` }]] },
            }
          )
        )
      );
      await clearSession(chatId);
      // Check "talkative" achievement after sending
      await checkAndGrantAchievements(chatId).catch(() => {});
      return safeSend(chatId, '✅ Вопрос отправлен менеджеру\\. Мы ответим в ближайшее время\\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
      });
    }

    // ── Ответ администратора напрямую клиенту (direct_reply)
    if (isAdmin(chatId) && state === 'direct_reply' && d.target_chat_id) {
      await safeSend(d.target_chat_id, `💬 *Сообщение от менеджера:*\n\n${esc(text)}`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '✍️ Ответить', callback_data: 'msg_manager_start' }]] },
      });
      await clearSession(chatId);
      return safeSend(chatId, '✅ Ответ отправлен клиенту.');
    }

    // ── Client free message → forward to admin
    if (!isAdmin(chatId)) {
      const clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      const username = msg.from.username ? `@${msg.from.username}` : '';
      const order = await get('SELECT * FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 1', [
        String(chatId),
      ]).catch(() => null);
      if (order) {
        await run('INSERT INTO messages (order_id,sender_type,sender_name,content) VALUES (?,?,?,?)', [
          order.id,
          'client',
          clientName,
          text,
        ]).catch(() => {});
      }
      const notifMsg = await getSetting('notif_new_message').catch(() => '1');
      if (notifMsg !== '0') {
        const adminIds = await getAdminChatIds();
        const header = order
          ? `📩 *Сообщение от клиента*\nЗаявка: *${esc(order.order_number)}*\nКлиент: ${esc(clientName)} ${esc(username)}\n\n`
          : `📩 *Новое сообщение*\n${esc(clientName)} ${esc(username)}\n\n`;
        await Promise.allSettled(
          adminIds.map(id =>
            safeSend(id, header + esc(text), {
              parse_mode: 'MarkdownV2',
              reply_markup: order
                ? {
                    inline_keyboard: [
                      [
                        { text: '💬 Ответить', callback_data: `adm_contact_${order.id}` },
                        { text: '📋 Заявка', callback_data: `adm_order_${order.id}` },
                      ],
                    ],
                  }
                : undefined,
            })
          )
        );
      }
      return safeSend(chatId, '✅ Сообщение передано менеджеру\\. Ответим в ближайшее время\\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
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
  const notifEnabled = await getSetting('notif_new_order').catch(() => '1');
  if (notifEnabled === '0') return;
  let modelName = null;
  if (order.model_id) {
    const m = await get('SELECT name FROM models WHERE id=?', [order.model_id]).catch(() => null);
    if (m) modelName = m.name;
  }
  const text =
    `🆕 *Новая заявка\\!*\n\n` +
    `📋 *${esc(order.order_number)}*\n` +
    `👤 ${esc(order.client_name)}\n📞 ${esc(order.client_phone)}\n` +
    (order.client_email ? `📧 ${esc(order.client_email)}\n` : '') +
    (order.client_telegram ? `💬 @${esc(String(order.client_telegram).replace('@', ''))}\n` : '') +
    `\n🎭 ${esc(EVENT_TYPES[order.event_type] || order.event_type)}\n` +
    (order.event_date ? `📅 ${esc(order.event_date)}\n` : '') +
    (order.location ? `📍 ${esc(order.location)}\n` : '') +
    (order.budget ? `💰 ${esc(order.budget)}\n` : '') +
    (modelName ? `💃 ${esc(modelName)}\n` : '') +
    (order.comments ? `\n💬 ${esc(order.comments)}` : '');

  const ids = await getAdminChatIds();
  await Promise.allSettled(
    ids.map(id =>
      safeSend(id, text, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [
              { text: '✅ Подтвердить', callback_data: `adm_confirm_${order.id}` },
              { text: '🔍 В работу', callback_data: `adm_review_${order.id}` },
              { text: '❌ Отклонить', callback_data: `adm_reject_${order.id}` },
            ],
            [{ text: '💬 Написать клиенту', callback_data: `adm_contact_${order.id}` }],
            [{ text: '📋 Открыть заявку', callback_data: `adm_order_${order.id}` }],
          ],
        },
      })
    )
  );
}

async function notifyStatusChange(clientChatId, orderNumber, newStatus, clientPhone = null) {
  if (!bot || !clientChatId) return;

  // notif_status setting is intentionally not checked here — client always receives status updates

  // Check client notification preferences
  const clientPrefs = await get('SELECT notify_status, notify_review_invites FROM client_prefs WHERE chat_id=?', [
    clientChatId,
  ]).catch(() => null);
  if (clientPrefs && clientPrefs.notify_status === 0) {
    // Client opted out of status notifications
    return;
  }
  const msgs = {
    confirmed: `✅ *Заявка ${esc(orderNumber)} подтверждена\\!*\n\nМенеджер свяжется с вами для уточнения деталей\\.`,
    reviewing: `🔍 *Заявка ${esc(orderNumber)} принята в работу\\.*\n\nМы изучаем ваш запрос\\.`,
    in_progress: `▶️ *Заявка ${esc(orderNumber)} выполняется\\.*`,
    completed: `🏁 *Заявка ${esc(orderNumber)} завершена\\!*\n\nСпасибо, что выбрали Nevesty Models\\! 💎`,
    cancelled: `❌ *Заявка ${esc(orderNumber)} отклонена\\.*\n\nЕсли есть вопросы — свяжитесь с нами\\.`,
  };
  const text = msgs[newStatus];
  if (!text) return;

  // Кнопки действий для клиента при смене статуса
  const keyboard = {
    inline_keyboard: [
      [
        { text: '💬 Написать менеджеру', callback_data: 'contact_mgr' },
        { text: '📋 Мои заявки', callback_data: 'my_orders' },
      ],
      [{ text: '📝 Повторить заявку', callback_data: 'bk_start' }],
    ],
  };

  // После завершения — предлагаем оставить отзыв (если отзывы включены)
  let reviewsEnabledForCompleted = false;
  let reviewOrderId = null;
  if (newStatus === 'completed') {
    try {
      const [reviewsEnabled, order] = await Promise.all([
        getSetting('reviews_enabled').catch(() => null),
        get('SELECT id FROM orders WHERE order_number=?', [orderNumber]).catch(() => null),
      ]);
      reviewOrderId = order?.id || null;
      const reviewInvitesAllowed = !clientPrefs || clientPrefs.notify_review_invites !== 0;
      if (reviewsEnabled === '1' && order && reviewInvitesAllowed) {
        reviewsEnabledForCompleted = true;
        keyboard.inline_keyboard.unshift([{ text: '⭐ Оставить отзыв', callback_data: `leave_review_${order.id}` }]);
      }
    } catch {}
  }

  // WhatsApp кнопка — если есть телефон и настроен WhatsApp контакт агентства
  try {
    const [orderRow, waContact] = await Promise.all([
      get('SELECT client_phone FROM orders WHERE order_number=?', [orderNumber]).catch(() => null),
      getSetting('contacts_whatsapp').catch(() => null),
    ]);
    if (orderRow?.client_phone && waContact) {
      const statusLabels = {
        confirmed: 'подтверждена',
        reviewing: 'принята в работу',
        in_progress: 'выполняется',
        completed: 'завершена',
        cancelled: 'отклонена',
      };
      const waMsg = `Здравствуйте! Статус вашей заявки №${orderNumber} изменён: ${statusLabels[newStatus] || newStatus}. Агентство Nevesty Models.`;
      const phone = orderRow.client_phone.replace(/[^0-9+]/g, '');
      const waUrl = `https://wa.me/${phone.replace(/^\+/, '')}?text=${encodeURIComponent(waMsg)}`;
      keyboard.inline_keyboard.push([{ text: '💬 Написать в WhatsApp', url: waUrl }]);
    }
  } catch {}

  await safeSend(clientChatId, text, { parse_mode: 'MarkdownV2', reply_markup: keyboard });

  // SMS уведомление при подтверждении или завершении заявки
  if (smsService && clientPhone) {
    const smsEnabled = await getSetting('sms_notifications_enabled').catch(() => null);
    if (smsEnabled === '1') {
      if (newStatus === 'confirmed') {
        const smsText = `Nevesty Models: ваша заявка ${orderNumber} подтверждена! Менеджер свяжется с вами.`;
        smsService.sendSMS(clientPhone, smsText).catch(e => console.error('[SMS] notify confirm:', e.message));
      } else if (newStatus === 'completed') {
        const smsText = `Nevesty Models: заявка ${orderNumber} завершена! Спасибо, что выбрали нас.`;
        smsService.sendSMS(clientPhone, smsText).catch(e => console.error('[SMS] notify complete:', e.message));
      }
    }
  }

  // Отправляем отдельное приглашение к отзыву с задержкой (если отзывы включены и клиент прошёл порог)
  if (reviewsEnabledForCompleted && reviewOrderId) {
    try {
      const [reviewsMinCompleted, reviewsPromptText] = await Promise.all([
        getSetting('reviews_min_completed').catch(() => null),
        getSetting('reviews_prompt_text').catch(() => null),
      ]);
      const minCompleted = parseInt(reviewsMinCompleted) || 0;
      const completedCount = await get(
        "SELECT COUNT(*) as n FROM orders WHERE client_chat_id=? AND status='completed'",
        [String(clientChatId)]
      ).catch(() => ({ n: 0 }));
      if ((completedCount?.n || 0) >= minCompleted) {
        const promptText =
          reviewsPromptText ||
          '⭐ Как прошло мероприятие? Оставьте отзыв о работе с нами!\nЭто займёт 1 минуту и поможет другим клиентам.';
        setTimeout(async () => {
          await safeSend(clientChatId, promptText, {
            reply_markup: {
              inline_keyboard: [
                [{ text: '⭐ Оставить отзыв', callback_data: `rev_start_${reviewOrderId}` }],
                [{ text: '⏩ Позже', callback_data: 'review_skip' }],
              ],
            },
          }).catch(() => {});
        }, 1000);
      }
    } catch {}
  }
}

async function sendMessageToClient(clientChatId, orderNumber, text) {
  if (!bot || !clientChatId) return;
  await safeSend(clientChatId, `💬 *Сообщение от менеджера* \\(${esc(orderNumber)}\\):\n\n${esc(text)}`, {
    parse_mode: 'MarkdownV2',
  });
}

async function notifyPaymentSuccess(clientChatId, orderNumber) {
  if (!bot || !clientChatId) return;
  await safeSend(
    clientChatId,
    `✅ *Оплата получена\\!* Ваша заявка *${esc(orderNumber)}* подтверждена\\.\n\nСпасибо\\! Менеджер свяжется с вами для уточнения деталей\\.`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
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
    reply_markup: { inline_keyboard: keyboard },
  });
}

// ─── User Profile ──────────────────────────────────────────────────────────────

async function showUserProfile(chatId, firstName) {
  try {
    const [orders, lastOrderFull, prefs] = await Promise.all([
      query(
        `SELECT o.id, o.status, o.created_at, o.order_number, m.name AS model_name FROM orders o
         LEFT JOIN models m ON m.id = o.model_id
         WHERE o.client_chat_id = ?
         ORDER BY o.created_at DESC LIMIT 50`,
        [String(chatId)]
      ),
      get(
        `SELECT client_name, client_phone, client_email FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 1`,
        [String(chatId)]
      ).catch(() => null),
      get(`SELECT * FROM client_prefs WHERE chat_id=?`, [chatId]).catch(() => null),
    ]);

    // Resolve name/phone/email: client_prefs takes priority, fallback to last order
    const displayName = prefs?.name || lastOrderFull?.client_name || firstName || 'Гость';
    const displayPhone = prefs?.phone || lastOrderFull?.client_phone || null;
    const displayEmail = prefs?.email || lastOrderFull?.client_email || null;

    const profileEditButtons = [
      [
        { text: '✏️ Изменить имя', callback_data: 'profile_edit_name' },
        { text: '📱 Изменить телефон', callback_data: 'profile_edit_phone' },
      ],
      [{ text: '📧 Изменить email', callback_data: 'profile_edit_email' }],
      [{ text: '🔔 Уведомления', callback_data: 'client_notif_settings' }],
      [{ text: '⚙️ Настройки', callback_data: 'client_settings' }],
    ];

    if (!orders.length) {
      let emptyText = `_🏠 Главная › 👤 Профиль_\n\n`;
      emptyText += `👤 *Мой профиль*\n\n`;
      emptyText += `Имя: *${esc(displayName)}*\n`;
      emptyText += `📱 Телефон: ${displayPhone ? esc(displayPhone) : '_\\(не указан\\)_'}\n`;
      emptyText += `📧 Email: ${displayEmail ? esc(displayEmail) : '_\\(не указан\\)_'}\n`;
      emptyText += `\nУ вас пока нет заявок\\. Оформите первую прямо сейчас\\!`;
      return safeSend(chatId, emptyText, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            ...profileEditButtons,
            [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }],
            [{ text: '← Назад', callback_data: 'main_menu' }],
          ],
        },
      });
    }

    // Count by status
    const counts = {};
    for (const o of orders) {
      counts[o.status] = (counts[o.status] || 0) + 1;
    }

    // Stats
    const activeStatuses = new Set(['new', 'reviewing', 'confirmed', 'in_progress']);
    const activeOrders = orders.filter(o => activeStatuses.has(o.status)).length;
    const completedOrders = counts['completed'] || 0;
    const cancelledOrders = counts['cancelled'] || 0;
    const totalOrders = orders.length;

    const firstDate = orders[orders.length - 1]?.created_at
      ? new Date(orders[orders.length - 1].created_at).toLocaleDateString('ru')
      : 'неизвестно';
    const lastDate = orders[0]?.created_at ? new Date(orders[0].created_at).toLocaleDateString('ru') : 'неизвестно';

    const [loyalty, earnedAchs] = await Promise.all([
      get(`SELECT * FROM loyalty_points WHERE chat_id=?`, [chatId]).catch(() => null),
      query(`SELECT achievement_key FROM achievements WHERE chat_id=? ORDER BY achieved_at ASC`, [chatId]).catch(
        () => []
      ),
    ]);

    const level = !loyalty
      ? '🥉 Бронзовый'
      : loyalty.total_earned >= 5000
        ? '💎 Платиновый'
        : loyalty.total_earned >= 2000
          ? '🥇 Золотой'
          : loyalty.total_earned >= 500
            ? '🥈 Серебряный'
            : '🥉 Бронзовый';

    // Compute next loyalty level threshold
    const currentPoints = loyalty?.total_earned || 0;
    const nextLevelThreshold =
      currentPoints < 500 ? 500 : currentPoints < 2000 ? 2000 : currentPoints < 5000 ? 5000 : null;
    const pointsBalance = loyalty?.points || 0;

    let text = `_🏠 Главная › 👤 Профиль_\n\n`;
    text += `👤 *Мой профиль*\n\n`;
    text += `Имя: *${esc(displayName)}*\n`;
    text += `📱 Телефон: ${displayPhone ? esc(displayPhone) : '_\\(не указан\\)_'}\n`;
    text += `📧 Email: ${displayEmail ? esc(displayEmail) : '_\\(не указан\\)_'}\n`;
    text += `💫 Уровень: *${esc(level)}*\n`;
    if (loyalty) {
      if (nextLevelThreshold) {
        const toNext = nextLevelThreshold - currentPoints;
        text += `💎 Баллы: *${pointsBalance}* \\(до следующей награды: ${toNext}\\)\n`;
      } else {
        text += `💎 Баллы: *${pointsBalance}* \\(максимальный уровень\\)\n`;
      }
    }
    text += `\n📊 *Статистика заявок:*\n`;
    text += `Активных: ${activeOrders}\n`;
    text += `Завершённых: ${completedOrders}\n`;
    if (cancelledOrders) text += `Отменённых: ${cancelledOrders}\n`;
    text += `Всего: ${totalOrders}\n`;
    text += `Первая: ${esc(firstDate)}\n`;
    text += `Последняя: ${esc(lastDate)}\n\n`;

    const statusOrder = ['new', 'reviewing', 'confirmed', 'in_progress', 'completed', 'cancelled'];
    for (const st of statusOrder) {
      if (counts[st]) {
        const label = STATUS_LABELS[st] || st;
        text += `  ${label}: ${counts[st]}\n`;
      }
    }

    // Last 3 orders summary in text
    const recent3 = orders.slice(0, 3);
    if (recent3.length) {
      text += `\n📋 *Последние заявки:*\n`;
      for (const o of recent3) {
        const modelPart = o.model_name ? ` — ${esc(o.model_name)}` : '';
        const stLabel = STATUS_LABELS[o.status] || esc(o.status);
        text += `• ${esc(o.order_number)}${modelPart} \\[${stLabel}\\]\n`;
      }
    }

    // Achievements section
    text += `\n🏆 *Достижения* \\(${earnedAchs.length}/${ACHIEVEMENTS_LIST.length}\\):\n`;
    if (earnedAchs.length === 0) {
      text += `_Выполняйте заявки, чтобы получить достижения\\!_\n`;
    } else {
      const earnedKeys = new Set(earnedAchs.map(a => a.achievement_key));
      const earned = ACHIEVEMENTS_LIST.filter(a => earnedKeys.has(a.key));
      text += earned.map(a => `${esc(a.icon)} ${esc(a.title)}`).join('  ') + '\n';
    }

    // Last 3 orders for quick access (buttons)
    const recentBtns = orders.slice(0, 3).map(o => [
      {
        text: o.model_name
          ? `${o.order_number} · ${o.model_name}  ${STATUS_LABELS[o.status] || o.status}`
          : `${o.order_number}  ${STATUS_LABELS[o.status] || o.status}`,
        callback_data: `client_order_${o.id}`,
      },
    ]);

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...recentBtns,
          [{ text: '📋 Все заявки', callback_data: 'my_orders' }],
          [
            { text: '🏆 Достижения', callback_data: 'my_achievements' },
            { text: '💫 Баллы', callback_data: 'loyalty' },
          ],
          [{ text: '📤 Пригласить друга', callback_data: 'referral' }],
          ...profileEditButtons,
          [{ text: '📝 Новая заявка', callback_data: 'bk_start' }],
          [{ text: '← Назад', callback_data: 'main_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showUserProfile:', e.message);
  }
}

// ─── Client notification preferences ─────────────────────────────────────────

async function showClientNotificationSettings(chatId) {
  const prefs = (await get('SELECT * FROM client_prefs WHERE chat_id=?', [chatId]).catch(() => null)) || {
    notify_marketing: 1,
    notify_status: 1,
    notify_reminders: 1,
    notify_review_invites: 1,
  };

  // 1 (or null/undefined) = on, 0 = off
  const onOff = v => (v === undefined || v === null || v === 1 || v === true ? '🔔 Вкл' : '🔕 Выкл');

  return safeSend(chatId, '🔔 *Настройки уведомлений*\n\nВыберите что вы хотите получать:', {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [
          {
            text: `📢 ${onOff(prefs.notify_marketing)} Рассылки от агентства`,
            callback_data: 'client_notif_marketing',
          },
        ],
        [{ text: `🔔 ${onOff(prefs.notify_status)} Изменение статуса заявки`, callback_data: 'client_notif_status' }],
        [
          {
            text: `⏰ ${onOff(prefs.notify_reminders)} Напоминания о мероприятиях`,
            callback_data: 'client_notif_reminders',
          },
        ],
        [
          {
            text: `📝 ${onOff(prefs.notify_review_invites)} Приглашения оставить отзыв`,
            callback_data: 'client_notif_review_invites',
          },
        ],
        [{ text: '← Назад', callback_data: 'client_settings' }],
      ],
    },
  });
}

// ─── Client settings menu ─────────────────────────────────────────────────────

async function showClientSettings(chatId) {
  const prefs = (await get('SELECT profile_hidden FROM client_prefs WHERE chat_id=?', [chatId]).catch(() => null)) || {
    profile_hidden: 0,
  };

  const langEnabled = await getSetting('bot_language').catch(() => null);
  const privacyLabel = prefs.profile_hidden ? '🔒 Профиль скрыт' : '👁 Профиль виден';

  const keyboard = [
    [{ text: `${privacyLabel} — переключить`, callback_data: 'client_settings_privacy' }],
    [{ text: '🔔 Уведомления', callback_data: 'client_notif_settings' }],
    [
      {
        text: langEnabled === 'multi' ? '🌐 Язык / Language' : '🌐 Язык (скоро)',
        callback_data: 'client_settings_lang',
      },
    ],
    [{ text: '🗑 Удалить аккаунт', callback_data: 'client_settings_delete' }],
    [{ text: '← Назад', callback_data: 'profile' }],
  ];

  return safeSend(
    chatId,
    '⚙️ *Настройки аккаунта*\n\n' + 'Управляйте приватностью, уведомлениями и языком интерфейса\\.',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: keyboard },
    }
  );
}

// ─── AI Factory Panel ─────────────────────────────────────────────────────────

const FACTORY_DB_PATH = require('path').join(__dirname, '..', 'factory', 'factory.db');

function factoryDbGet(sql, params = []) {
  return new Promise((resolve, _reject) => {
    const sqlite3 = require('sqlite3').verbose();
    const fdb = new sqlite3.Database(FACTORY_DB_PATH, sqlite3.OPEN_READONLY, err => {
      if (err) return resolve(null);
      fdb.get(sql, params, (e, row) => {
        fdb.close();
        e ? resolve(null) : resolve(row || null);
      });
    });
  });
}

function factoryDbAll(sql, params = []) {
  return new Promise((resolve, _reject) => {
    const sqlite3 = require('sqlite3').verbose();
    const fdb = new sqlite3.Database(FACTORY_DB_PATH, sqlite3.OPEN_READONLY, err => {
      if (err) return resolve([]);
      fdb.all(sql, params, (e, rows) => {
        fdb.close();
        e ? resolve([]) : resolve(rows || []);
      });
    });
  });
}

// ─── Factory Content: view & send AI-generated posts to Telegram channel ────────

async function getFactoryTelegramPosts(limit) {
  const fs = require('fs');
  if (!fs.existsSync(FACTORY_DB_PATH)) return null;
  return factoryDbAll(
    "SELECT id, action_type, channel, action as content, status, created_at FROM growth_actions WHERE channel = 'telegram' AND action IS NOT NULL ORDER BY created_at DESC LIMIT ?",
    [limit || 5]
  );
}

function formatFactoryPostForChannel(content) {
  const phone = process.env.CONTACT_PHONE || '+7 (800) 123-45-67';
  const domain = (process.env.SITE_URL || 'nevesty-models.ru').replace(/^https?:\/\//, '');
  return '💎 Nevesty Models\n\n' + content + '\n\n📞 ' + phone + '\n🌐 ' + domain + '\n#невесты #модели #агентство';
}

async function showFactoryContent(chatId) {
  if (!isAdmin(chatId)) return;
  const posts = await getFactoryTelegramPosts(5);
  if (posts === null) {
    return safeSend(chatId, '🏭 Factory не запущен.\n\nЗапустите: pm2 start nevesty-factory', {
      reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] },
    });
  }
  if (!posts.length) {
    return safeSend(chatId, '📢 Нет Telegram-постов от Factory.\n\nЗапустите цикл — Factory сгенерирует контент.', {
      reply_markup: {
        inline_keyboard: [
          [{ text: '🔄 Запустить цикл', callback_data: 'adm_factory_run' }],
          [{ text: '← Factory', callback_data: 'adm_factory' }],
        ],
      },
    });
  }
  const tgChannel = await getSetting('tg_channel').catch(() => null);
  const channelLabel = tgChannel || '(канал не задан)';
  for (const p of posts) {
    const preview = (p.content || '').slice(0, 300);
    const dt = p.created_at
      ? new Date(p.created_at).toLocaleString('ru-RU', {
          timeZone: 'Europe/Moscow',
          day: '2-digit',
          month: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
      : '';
    const label =
      '📝 [' +
      (p.action_type || 'post') +
      '] ' +
      dt +
      '\n\n' +
      preview +
      (preview.length < (p.content || '').length ? '…' : '');
    await safeSend(chatId, label, {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '📢 Отправить в канал', callback_data: 'adm_fc_pub_' + p.id },
            { text: '👁 Превью', callback_data: 'adm_fc_preview_' + p.id },
          ],
        ],
      },
    });
  }
  return safeSend(chatId, '📣 Канал: ' + channelLabel + '\n\nНастройте в: Настройки → Бот → Telegram канал', {
    reply_markup: {
      inline_keyboard: [
        [{ text: '⚙️ Задать канал', callback_data: 'adm_set_tg_channel' }],
        [{ text: '← Factory', callback_data: 'adm_factory' }],
      ],
    },
  });
}

async function previewFactoryPost(chatId, postId) {
  if (!isAdmin(chatId)) return;
  const post = await factoryDbGet('SELECT action as content FROM growth_actions WHERE id=?', [postId]);
  if (!post || !post.content) {
    return safeSend(chatId, '❌ Пост не найден.', {
      reply_markup: { inline_keyboard: [[{ text: '← Контент', callback_data: 'adm_factory_content' }]] },
    });
  }
  const formatted = formatFactoryPostForChannel(post.content);
  return safeSend(chatId, '👁 Предпросмотр поста:\n\n' + formatted, {
    reply_markup: {
      inline_keyboard: [
        [{ text: '📢 Отправить в канал', callback_data: 'adm_fc_pub_' + postId }],
        [{ text: '← Контент', callback_data: 'adm_factory_content' }],
      ],
    },
  });
}

async function publishFactoryPost(chatId, postId) {
  if (!isAdmin(chatId)) return;
  const post = await factoryDbGet('SELECT action as content FROM growth_actions WHERE id=?', [postId]);
  if (!post || !post.content) {
    return safeSend(chatId, '❌ Пост не найден.', {
      reply_markup: { inline_keyboard: [[{ text: '← Контент', callback_data: 'adm_factory_content' }]] },
    });
  }
  let channelId = await getSetting('tg_channel').catch(() => null);
  const formatted = formatFactoryPostForChannel(post.content);
  if (!channelId) {
    await safeSend(chatId, '⚠️ Telegram-канал не задан. Отправляю превью вам как тест:\n\n' + formatted);
    return safeSend(chatId, 'Чтобы отправлять в канал, задайте его в настройках:', {
      reply_markup: {
        inline_keyboard: [
          [{ text: '⚙️ Задать канал', callback_data: 'adm_set_tg_channel' }],
          [{ text: '← Контент', callback_data: 'adm_factory_content' }],
        ],
      },
    });
  }
  if (!channelId.startsWith('@') && !channelId.startsWith('-')) {
    channelId = '@' + channelId;
  }
  try {
    await bot.sendMessage(channelId, formatted);
    return safeSend(chatId, '✅ Пост отправлен в канал ' + channelId + '!', {
      reply_markup: { inline_keyboard: [[{ text: '← Контент', callback_data: 'adm_factory_content' }]] },
    });
  } catch (e) {
    console.error('[Bot] publishFactoryPost:', e.message);
    return safeSend(
      chatId,
      '❌ Ошибка отправки: ' + e.message + '\n\nПроверьте, что бот является администратором канала ' + channelId + '.',
      {
        reply_markup: { inline_keyboard: [[{ text: '← Контент', callback_data: 'adm_factory_content' }]] },
      }
    );
  }
}

async function showFactoryPanel(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [lastCycle, lastDecision, pendingCount, runningExp, topActions, weeklyReport, proposedExp] =
      await Promise.all([
        factoryDbGet('SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1'),
        factoryDbGet('SELECT * FROM decisions ORDER BY created_at DESC LIMIT 1'),
        factoryDbGet("SELECT COUNT(*) as n FROM growth_actions WHERE status='pending'"),
        factoryDbGet("SELECT COUNT(*) as n FROM experiments WHERE status='running'"),
        factoryDbAll(
          "SELECT action_type, channel, priority, action FROM growth_actions WHERE status='pending' ORDER BY priority DESC, created_at DESC LIMIT 5"
        ),
        factoryDbGet(
          "SELECT report_json FROM factory_reports WHERE report_type='weekly' ORDER BY created_at DESC LIMIT 1"
        ),
        factoryDbGet(
          "SELECT hypothesis, effort, expected_lift FROM experiments WHERE status='proposed' ORDER BY created_at DESC LIMIT 1"
        ),
      ]);

    const score = lastCycle?.health_score ?? '—';
    const icon = score >= 70 ? '💚' : score >= 50 ? '🟡' : '🔴';
    const elapsed = lastCycle ? `${lastCycle.duration_s || '?'}с` : 'нет данных';
    const cycleTime = lastCycle?.finished_at
      ? new Date(lastCycle.finished_at).toLocaleString('ru-RU', {
          timeZone: 'Europe/Moscow',
          hour: '2-digit',
          minute: '2-digit',
          day: '2-digit',
          month: '2-digit',
        })
      : '—';

    // Key insights: extract top 3 lines from cycle summary
    let insightsSection = '';
    if (lastCycle?.summary) {
      const lines = lastCycle.summary
        .split('\n')
        .map(l => l.trim())
        .filter(l => l.length > 3);
      const top3 = lines.slice(0, 3);
      if (top3.length) {
        insightsSection = '\n\n📌 Инсайты последнего цикла:\n' + top3.map(l => `  • ${l.slice(0, 90)}`).join('\n');
      }
    }

    // Recent growth actions (top 5)
    let actionsSection = '';
    if (topActions && topActions.length) {
      const channelIcon = { telegram: '📱', instagram: '📸', tiktok: '🎵', seo: '🔍', email: '📧', direct: '📞' };
      actionsSection =
        '\n\n💡 Топ Growth Actions:\n' +
        topActions
          .map(
            (a, i) =>
              `  ${i + 1}. ${channelIcon[a.channel] || '•'} [${a.channel}/${a.action_type}] p${a.priority} — ${(a.action || '').slice(0, 60)}`
          )
          .join('\n');
    }

    // CEO focus department from weekly report
    let ceoFocusSection = '';
    try {
      const reportData = weeklyReport?.report_json ? JSON.parse(weeklyReport.report_json) : null;
      if (reportData?.last_ceo_focus || reportData?.top_department) {
        const focus = reportData.last_ceo_focus || reportData.top_department;
        ceoFocusSection = `\n\n🎯 Фокус CEO (следующий цикл): ${focus}`;
      }
    } catch (_) {}

    // Current A/B experiment proposal
    let abSection = '';
    if (proposedExp) {
      abSection =
        `\n\n🧪 A/B предложение: ${(proposedExp.hypothesis || '').slice(0, 80)}` +
        (proposedExp.effort ? ` [усилие: ${proposedExp.effort}]` : '');
    }

    const decisionLine = lastDecision
      ? `\n🧠 Решение CEO: ${lastDecision.decision_type} — ${(lastDecision.rationale || '').slice(0, 80)}`
      : '';

    const adminUrl = (SITE_URL || 'http://localhost:3000').replace(/\/$/, '') + '/admin/factory.html';

    const text =
      `🏭 AI Startup Factory\n\n` +
      `${icon} Health Score: ${score}%\n` +
      `🕐 Последний цикл: ${cycleTime} (${elapsed})\n` +
      `💡 Действий в очереди: ${pendingCount?.n ?? 0}\n` +
      `🧪 Экспериментов активных: ${runningExp?.n ?? 0}` +
      decisionLine +
      insightsSection +
      actionsSection +
      ceoFocusSection +
      abSection;

    const keyboard = [
      [
        { text: '🔄 Запустить цикл', callback_data: 'adm_factory_run' },
        { text: '💡 Growth Actions', callback_data: 'adm_factory_growth' },
      ],
      [
        { text: '🧪 Эксперименты', callback_data: 'adm_factory_exp' },
        { text: '📋 Решения CEO', callback_data: 'adm_factory_decisions' },
      ],
      [
        { text: '🎯 AI Задачи', callback_data: 'adm_factory_tasks' },
        { text: '🧪 A/B Тесты', callback_data: 'adm_experiments' },
      ],
      [
        { text: '📋 Growth Actions', callback_data: 'adm_factory_actions' },
        { text: '📢 Контент в канал', callback_data: 'adm_factory_content' },
      ],
      [{ text: '← Меню', callback_data: 'admin_menu' }],
    ];
    // Add detail link only for HTTPS sites (web_app requires HTTPS)
    if (SITE_URL && SITE_URL.startsWith('https://')) {
      keyboard.splice(keyboard.length - 1, 0, [{ text: '📊 Детали', web_app: { url: adminUrl } }]);
    }

    return safeSend(chatId, text, { reply_markup: { inline_keyboard: keyboard } });
  } catch (e) {
    console.error('[Factory] showFactoryPanel:', e.message);
    return safeSend(chatId, '🏭 AI Factory ещё не запущен.\n\nЗапустите: `pm2 start nevesty-factory`', {
      reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] },
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
      reply_markup: {
        inline_keyboard: [
          [{ text: '🔄 Запустить цикл', callback_data: 'adm_factory_run' }],
          [{ text: '← Factory', callback_data: 'adm_factory' }],
        ],
      },
    });
  }

  for (const a of actions) {
    const channelIcon =
      { telegram: '📱', instagram: '📸', tiktok: '🎵', seo: '🔍', email: '📧', direct: '📞' }[a.channel] || '💡';
    const text = `${channelIcon} [${a.channel}/${a.action_type}] приоритет ${a.priority}\n\n${(a.content || '').slice(0, 600)}`;
    await safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [[{ text: '✅ Выполнено', callback_data: `adm_factory_done_${a.id}` }]] },
    });
  }

  const nav = [];
  if (page > 0) nav.push({ text: '◀ Назад', callback_data: `adm_factory_growth_${page - 1}` });
  if (offset + LIMIT < total) nav.push({ text: 'Ещё ▶', callback_data: `adm_factory_growth_${page + 1}` });

  return safeSend(chatId, `Показано ${offset + 1}–${Math.min(offset + LIMIT, total)} из ${total}`, {
    reply_markup: {
      inline_keyboard: [nav.length ? nav : [], [{ text: '← Factory', callback_data: 'adm_factory' }]].filter(
        r => r.length
      ),
    },
  });
}

async function showFactoryGrowthActions(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const { existsSync } = require('fs');
    if (!existsSync(FACTORY_DB_PATH)) {
      return safeSend(chatId, '🏭 Factory не запущена\\.', { parse_mode: 'MarkdownV2' });
    }
    const rows = await factoryDbAll(
      `SELECT action_type, description, status, priority, created_at
       FROM growth_actions
       WHERE status='pending'
       ORDER BY priority DESC, created_at DESC
       LIMIT 5`,
      []
    );
    if (!rows || !rows.length) {
      return safeSend(chatId, '🏭 *Нет активных Growth Actions*\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_factory' }]] },
      });
    }
    let text = '🏭 *Growth Actions \\(pending\\)*\n\n';
    rows.forEach((r, i) => {
      text += `${i + 1}\\. ${esc(r.action_type)} — ${esc((r.description || '').slice(0, 80))}\n`;
    });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'adm_factory' }]] },
    });
  } catch (e) {
    console.error('[Bot] showFactoryGrowthActions:', e.message);
  }
}

async function showFactoryDecisions(chatId) {
  if (!isAdmin(chatId)) return;
  const decisions = await factoryDbAll('SELECT * FROM decisions ORDER BY created_at DESC LIMIT 10');
  if (!decisions.length) {
    return safeSend(chatId, 'Нет решений CEO.', {
      reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] },
    });
  }
  const icons = {
    create_mvp: '📦',
    scale: '🚀',
    kill: '💀',
    iterate: '🔧',
    grow: '📣',
    experiment: '🧪',
    optimize: '⚙️',
    monitor: '👁',
  };
  const lines = decisions.map(
    d => `${icons[d.decision_type] || '•'} ${d.decision_type} — ${(d.rationale || '').slice(0, 80)}`
  );
  return safeSend(chatId, `📋 Решения CEO (последние 10)\n\n${lines.join('\n')}`, {
    reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] },
  });
}

async function showFactoryExperiments(chatId) {
  if (!isAdmin(chatId)) return;
  const exps = await factoryDbAll('SELECT * FROM experiments ORDER BY started_at DESC LIMIT 8');
  if (!exps.length) {
    return safeSend(chatId, 'Нет экспериментов.', {
      reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] },
    });
  }
  const statusIcon = { running: '🔵', concluded: '✅' };
  const resultIcon = { scale: '🚀', kill: '💀', iterate: '🔧' };
  const lines = exps.map(
    e =>
      `${statusIcon[e.status] || '•'} ${e.name}\n` +
      `   A=${e.conversion_a ?? '—'}% / B=${e.conversion_b ?? '—'}%` +
      (e.result ? ` → ${resultIcon[e.result] || ''} ${e.result}` : '')
  );
  return safeSend(chatId, `🧪 Эксперименты\n\n${lines.join('\n\n')}`, {
    reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] },
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
      query(
        "SELECT * FROM factory_tasks WHERE status='pending' ORDER BY priority DESC, created_at DESC LIMIT ? OFFSET ?",
        [LIMIT, offset]
      ),
      get("SELECT COUNT(*) as n FROM factory_tasks WHERE status='pending'"),
    ]);
    const total = totalRow ? totalRow.n : 0;
    if (!tasks || !tasks.length) {
      return safeSend(chatId, '🎯 Нет активных AI-задач.\n\nЗапустите цикл Factory чтобы сгенерировать новые задачи.', {
        reply_markup: {
          inline_keyboard: [
            [{ text: '🔄 Запустить цикл', callback_data: 'adm_factory_run' }],
            [{ text: '← Factory', callback_data: 'adm_factory' }],
          ],
        },
      });
    }
    const priIcon = function (p) {
      return p >= 8 ? '🔴' : p >= 5 ? '🟡' : '🟢';
    };
    const dIcons = {
      marketing: '📣',
      sales: '💼',
      product: '📦',
      tech: '🛠',
      hr: '👥',
      operations: '⚙',
      creative: '🎨',
      finance: '💰',
      research: '🔬',
      analytics: '📊',
    };
    for (const t of tasks) {
      const dept = t.department || '';
      const dicon = dIcons[dept] || '🎯';
      const parts = [
        dicon + ' AI-задача #' + t.id,
        '',
        priIcon(t.priority || 5) + ' Приоритет: ' + (t.priority || 5) + '/10',
      ];
      if (dept) parts.push('🏢 Отдел: ' + dept);
      if (t.expected_impact) parts.push('📈 Эффект: ' + t.expected_impact);
      parts.push('', (t.action || '').slice(0, 400));
      await safeSend(chatId, parts.join('\n'), {
        reply_markup: {
          inline_keyboard: [
            [
              { text: '✅ Выполнено', callback_data: 'factory_task_done_' + t.id },
              { text: '🗑 Пропустить', callback_data: 'factory_task_skip_' + t.id },
            ],
          ],
        },
      });
    }
    const nav = [];
    if (page > 0) nav.push({ text: '◀ Назад', callback_data: 'adm_factory_tasks_' + (page - 1) });
    if (offset + LIMIT < total) nav.push({ text: 'Ещё ▶', callback_data: 'adm_factory_tasks_' + (page + 1) });
    return safeSend(chatId, 'Показано ' + (offset + 1) + '–' + Math.min(offset + LIMIT, total) + ' из ' + total, {
      reply_markup: {
        inline_keyboard: [...(nav.length ? [nav] : []), [{ text: '← Factory', callback_data: 'adm_factory' }]],
      },
    });
  } catch (e) {
    console.error('[Bot] showFactoryTasks:', e.message);
    return safeSend(chatId, 'Ошибка загрузки AI-задач.', {
      reply_markup: { inline_keyboard: [[{ text: '← Factory', callback_data: 'adm_factory' }]] },
    });
  }
}

// ─── A/B Experiments (synced from AI Factory) ────────────────────────────────
// Note (БЛОК 5.5): showFactoryTasks IS the factory queue viewer — it shows pending
// tasks with [✅ Выполнено] and [🗑 Пропустить] buttons. Accessible via adm_factory_tasks.

async function showAdminExperiments(chatId) {
  if (!isAdmin(chatId)) return;
  const experiments = await query(`SELECT * FROM ab_experiments ORDER BY created_at DESC LIMIT 10`).catch(() => []);

  if (!experiments.length) {
    return safeSend(
      chatId,
      `🧪 *A/B Эксперименты*\n\nЭкспериментов пока нет\\. Factory сгенерирует их при следующем цикле\\.`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Factory панель', callback_data: 'adm_factory' }]] },
      }
    );
  }

  const statusIcon = { proposed: '💡', running: '▶️', applied: '✅', skipped: '❌' };
  const lines = experiments
    .map(
      (e, i) =>
        `${i + 1}\\. ${statusIcon[e.status] || '💡'} ${esc(e.hypothesis?.slice(0, 80) || '')}\\.\\.\\.\n   _Усилие: ${esc(e.effort || '?')} | Ожидание: ${esc(e.expected_lift || '?')}_`
    )
    .join('\n\n');

  return safeSend(chatId, `🧪 *A/B Эксперименты* \\(${experiments.length}\\)\n\n${lines}`, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [[{ text: '← Factory панель', callback_data: 'adm_factory' }]] },
  });
}

// ─── Admin Reviews ────────────────────────────────────────────────────────────

async function _showAdminReviews(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [pendingCount, approvedCount, totalCount] = await Promise.all([
      get("SELECT COUNT(*) as n FROM reviews WHERE approved=0 AND (status IS NULL OR status != 'rejected')"),
      get('SELECT COUNT(*) as n FROM reviews WHERE approved=1'),
      get('SELECT COUNT(*) as n FROM reviews'),
    ]);
    const text = `*⭐ Управление отзывами*\n\nОжидают одобрения: *${esc(String(pendingCount.n))}*\nОдобрено: *${esc(String(approvedCount.n))}*\nВсего: *${esc(String(totalCount.n))}*`;
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [
            { text: `⏳ Ожидают (${pendingCount.n})`, callback_data: 'adm_rev_pending' },
            { text: `✅ Одобрены (${approvedCount.n})`, callback_data: 'adm_rev_approved' },
            { text: `📋 Все (${totalCount.n})`, callback_data: 'adm_rev_all' },
          ],
          [{ text: '← Меню', callback_data: 'admin_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showAdminReviews:', e.message);
  }
}

async function _showAdminReviewsList(chatId, filter) {
  if (!isAdmin(chatId)) return;
  try {
    let reviews;
    if (filter === 'pending') {
      reviews = await query(
        "SELECT * FROM reviews WHERE approved=0 AND (status IS NULL OR status != 'rejected') ORDER BY created_at DESC LIMIT 15"
      ).catch(() => []);
    } else if (filter === 'approved') {
      reviews = await query('SELECT * FROM reviews WHERE approved=1 ORDER BY created_at DESC LIMIT 15').catch(() => []);
    } else {
      reviews = await query('SELECT * FROM reviews ORDER BY created_at DESC LIMIT 15').catch(() => []);
    }

    const filterLabels = { pending: 'ожидающих одобрения', approved: 'одобренных', all: 'отзывов' };
    const filterBtns = [
      { text: '⏳ Ожидают', callback_data: 'adm_rev_pending' },
      { text: '✅ Одобрены', callback_data: 'adm_rev_approved' },
      { text: '📋 Все', callback_data: 'adm_rev_all' },
    ];

    if (!reviews.length) {
      return safeSend(chatId, `Нет ${filterLabels[filter] || 'отзывов'}.`, {
        reply_markup: { inline_keyboard: [filterBtns, [{ text: '← К отзывам', callback_data: 'adm_reviews' }]] },
      });
    }

    for (const r of reviews) {
      const stars = '⭐'.repeat(Math.max(1, Math.min(5, r.rating || 1)));
      const preview = r.text ? r.text.slice(0, 120) + (r.text.length > 120 ? '…' : '') : '';
      const statusIcon = r.approved ? '✅' : r.status === 'rejected' ? '❌' : '⏳';
      const msgText = `${statusIcon} *Отзыв \\#${esc(String(r.id))}*\n👤 ${esc(r.client_name || 'Клиент')}\n${stars}\n\n${esc(preview)}`;
      const btns = [];
      if (!r.approved || r.status === 'rejected') {
        btns.push({ text: '✅ Одобрить', callback_data: `rev_approve_${r.id}` });
      }
      if (r.approved || r.status !== 'rejected') {
        btns.push({ text: '❌ Отклонить', callback_data: `rev_reject_${r.id}` });
      }
      btns.push({ text: '🔍 Подробнее', callback_data: `rev_view_${r.id}` });
      btns.push({ text: '🗑 Удалить', callback_data: `rev_delete_${r.id}` });
      await safeSend(chatId, msgText, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [btns] },
      });
    }

    const label = filter === 'pending' ? 'ожидают одобрения' : filter === 'approved' ? 'одобрено' : 'всего';
    return safeSend(chatId, `${label}: ${reviews.length}`, {
      reply_markup: { inline_keyboard: [filterBtns, [{ text: '← К отзывам', callback_data: 'adm_reviews' }]] },
    });
  } catch (e) {
    console.error('[Bot] showAdminReviewsList:', e.message);
  }
}

// ─── Топ-модели ───────────────────────────────────────────────────────────────

async function showTopModels(chatId, page = 0) {
  try {
    const perPage = 5;
    const models = await query(
      `SELECT m.*,
        (SELECT COUNT(*) FROM orders o WHERE o.model_id=m.id AND o.status NOT IN ('cancelled','new')) as book_count
       FROM models m WHERE m.available=1 AND COALESCE(m.archived,0)=0
       ORDER BY m.featured DESC, book_count DESC, m.id ASC`
    ).catch(() => []);

    if (!models.length) {
      return safeSend(chatId, '📭 Нет доступных моделей\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'main_menu' }]] },
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
    if (page > 0) nav.push({ text: '◀️', callback_data: `cat_top_${page - 1}` });
    if ((page + 1) * perPage < total) nav.push({ text: '▶️', callback_data: `cat_top_${page + 1}` });

    return safeSend(
      chatId,
      `⭐ *Топ\\-модели Nevesty Models*\n\n_Рейтинг по популярности и востребованности_\n\nВсего: ${total} ${ru_plural(total, 'модель', 'модели', 'моделей')}`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            ...modelBtns,
            ...(nav.length ? [nav] : []),
            [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }],
            [{ text: '🏠 Меню', callback_data: 'main_menu' }],
          ],
        },
      }
    );
  } catch (e) {
    console.error('[Bot] showTopModels:', e.message);
  }
}

// ─── Написать менеджеру ───────────────────────────────────────────────────────

async function showContactManager(chatId) {
  const [phone, insta, waPhone, mgrHours] = await Promise.all([
    getSetting('contacts_phone').catch(() => '+7 (900) 000-00-00'),
    getSetting('contacts_insta').catch(() => '@nevesty_models'),
    getSetting('contacts_whatsapp')
      .catch(() => null)
      .then(v => v || getSetting('agency_phone').catch(() => '')),
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
  const inlineRows = [[{ text: '✍️ Написать вопрос сейчас', callback_data: 'msg_manager_start' }]];
  if (waDigits) {
    inlineRows.push([{ text: '📱 WhatsApp', url: `https://wa.me/${waDigits}` }]);
  }
  inlineRows.push([{ text: '🏠 Главное меню', callback_data: 'main_menu' }]);
  return safeSend(chatId, msgText, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: inlineRows },
  });
}

// ─── Получить контакт модели ──────────────────────────────────────────────────

async function showModelContact(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена.');
    const parts = [];
    if (m.phone) parts.push(`📞 Телефон: ${esc(m.phone)}`);
    if (m.instagram) parts.push(`📸 Instagram: @${esc(m.instagram)}`);
    if (!parts.length) {
      return safeSend(
        chatId,
        `📱 *Контакт модели ${esc(m.name)}*\n\nДля получения контакта обратитесь к менеджеру\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '💬 Написать менеджеру', callback_data: 'contact_mgr' }],
              [{ text: '← Назад', callback_data: `cat_model_${modelId}` }],
            ],
          },
        }
      );
    }
    return safeSend(chatId, `📱 *Контакт: ${esc(m.name)}*\n\n${parts.join('\n')}`, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '📝 Заказать модель', callback_data: `bk_model_${m.id}` }],
          [{ text: '← Назад', callback_data: `cat_model_${modelId}` }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showModelContact:', e.message);
  }
}

// ─── О нас ────────────────────────────────────────────────────────────────────

async function showAboutUs(chatId) {
  const about = await getSetting('about').catch(() => 'Мы работаем с 2018 года. Более 200 моделей в базе.');
  const phone = await getSetting('contacts_phone').catch(() => '');
  return safeSend(
    chatId,
    `ℹ️ *О нас — Nevesty Models*\n\n${esc(about)}\n\n` +
      `💎 *Почему мы:*\n` +
      `• Более 200 профессиональных моделей\n` +
      `• Работаем по всей России\n` +
      `• Договор и полная юридическая чистота\n` +
      `• Fashion, Commercial, Events, Runway\n\n` +
      (phone ? `📞 ${esc(phone)}` : ''),
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '💃 Смотреть каталог', callback_data: 'cat_cat__0' }],
          [{ text: '📞 Контакты', callback_data: 'contacts' }],
          [{ text: '🏠 Меню', callback_data: 'main_menu' }],
        ],
      },
    }
  );
}

// ─── Прайс-лист ───────────────────────────────────────────────────────────────

async function showPricing(chatId) {
  const pricing = await getSetting('pricing').catch(() => '');
  const pricingText =
    pricing ||
    `💰 *Наши пакеты услуг*

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
    reply_markup: {
      inline_keyboard: [
        [{ text: '📋 Оформить заявку', callback_data: 'bk_start' }],
        [{ text: '📞 Связаться с менеджером', callback_data: 'msg_manager_start' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ],
    },
  });
}

// ─── Каталог по городу ────────────────────────────────────────────────────────

async function showCatalogByCity(chatId, city, page = 0) {
  try {
    const _rawPerPageCity = parseInt(await getSetting('catalog_per_page').catch(() => '5')) || 5;
    const perPage = Math.min(20, Math.max(1, _rawPerPageCity));
    const models = city
      ? await query('SELECT * FROM models WHERE available=1 AND COALESCE(archived,0)=0 AND city=? ORDER BY id', [city])
      : await query('SELECT * FROM models WHERE available=1 AND COALESCE(archived,0)=0 ORDER BY id');

    if (!models.length) {
      return safeSend(chatId, `📭 Моделей в городе «${city}» нет\\.`, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '💃 Все модели', callback_data: 'cat_cat__0' }]] },
      });
    }

    const total = models.length;
    const slice = models.slice(page * perPage, page * perPage + perPage);
    const catShortLabelsByCity = { fashion: 'Fashion', commercial: 'Commercial', events: 'Events' };
    const modelBtns = slice.map((m, i) => {
      const num = page * perPage + i + 1;
      const featStar = m.featured ? '⭐' : '·';
      const catShort = catShortLabelsByCity[m.category] || m.category || '';
      const agePart = m.age ? ` | ${m.age} л` : '';
      const heightPart = m.height ? ` | ${m.height} см` : '';
      return [
        {
          text: `[${num}] ${featStar} ${m.name}${heightPart}${agePart}${catShort ? ` | ${catShort}` : ''}`,
          callback_data: `cat_model_${m.id}`,
        },
      ];
    });
    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `cat_city_${city}_${page - 1}` });
    if ((page + 1) * perPage < total) nav.push({ text: '▶️', callback_data: `cat_city_${city}_${page + 1}` });

    return safeSend(
      chatId,
      `_🏠 Главная › 💃 Каталог › 🏙️ ${esc(city)}_\n\n🏙 *Модели — ${esc(city)}*\n\nНайдено: ${total}`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            ...modelBtns,
            ...(nav.length ? [nav] : []),
            [{ text: '← Каталог', callback_data: 'cat_cat__0' }],
            [{ text: '🏠 Меню', callback_data: 'main_menu' }],
          ],
        },
      }
    );
  } catch (e) {
    console.error('[Bot] showCatalogByCity:', e.message);
  }
}

// ─── Поиск модели по параметрам (БЛОК 2.4) ───────────────────────────────────
// In-memory фильтры для каждого пользователя
const searchFilters = new Map(); // chatId → { height_min, height_max, age_min, age_max, category, city }
// Cleanup stale search filters every 6 hours to prevent unbounded growth
setInterval(
  () => {
    searchFilters.clear();
  },
  6 * 60 * 60 * 1000
).unref();

function getSearchFilters(chatId) {
  if (!searchFilters.has(String(chatId))) searchFilters.set(String(chatId), {});
  return searchFilters.get(String(chatId));
}

async function showSearchMenu(chatId) {
  try {
    const f = getSearchFilters(chatId);

    // Height ranges definition (task spec: 160-165, 166-170, 171-175, 176-180, 181+)
    const heightRanges = [
      { key: '160', label: '🔹 160–165', min: 160, max: 165, cb: 'search_h_160' },
      { key: '166', label: '🔹 166–170', min: 166, max: 170, cb: 'search_h_166' },
      { key: '171', label: '🔹 171–175', min: 171, max: 175, cb: 'search_h_171' },
      { key: '176', label: '🔹 176–180', min: 176, max: 180, cb: 'search_h_176' },
      { key: '181', label: '🔹 181+', min: 181, max: 220, cb: 'search_h_181' },
    ];
    const ageRanges = [
      { key: '18', label: '🔸 18–22', min: 18, max: 22, cb: 'search_a_18' },
      { key: '23', label: '🔸 23–27', min: 23, max: 27, cb: 'search_a_23' },
      { key: '28', label: '🔸 28–32', min: 28, max: 32, cb: 'search_a_28' },
      { key: '33', label: '🔸 33+', min: 33, max: 99, cb: 'search_a_33' },
    ];

    // Height buttons (2 per row)
    const heightBtns = [];
    for (let i = 0; i < heightRanges.length; i += 2) {
      const row = heightRanges.slice(i, i + 2).map(r => {
        const active = f.height_min === r.min && f.height_max === r.max;
        return { text: (active ? '✅ ' : '') + r.label, callback_data: r.cb };
      });
      heightBtns.push(row);
    }

    // Age buttons (2 per row)
    const ageBtns = [];
    for (let i = 0; i < ageRanges.length; i += 2) {
      const row = ageRanges.slice(i, i + 2).map(r => {
        const active = f.age_min === r.min && f.age_max === r.max;
        return { text: (active ? '✅ ' : '') + r.label, callback_data: r.cb };
      });
      ageBtns.push(row);
    }

    // Category buttons
    const catDefs = [
      { key: 'fashion', label: '👗 Fashion', cb: 'search_cat_fashion' },
      { key: 'commercial', label: '📸 Commercial', cb: 'search_cat_commercial' },
      { key: 'events', label: '🎭 Events', cb: 'search_cat_events' },
    ];
    const catBtns = catDefs.map(c => {
      const active = f.category === c.key;
      return { text: (active ? '✅ ' : '') + c.label, callback_data: c.cb };
    });

    // City buttons — query DISTINCT cities from available models, fallback to getSetting('cities_list')
    let cities = [];
    try {
      const cityRows = await query(
        "SELECT DISTINCT city FROM models WHERE available=1 AND city IS NOT NULL AND city != '' ORDER BY city"
      );
      cities = cityRows
        .map(r => r.city)
        .filter(Boolean)
        .slice(0, 8);
      if (!cities.length) {
        const citiesSetting = await getSetting('cities_list').catch(() => '');
        const fallback = (citiesSetting || 'Москва,Санкт-Петербург,Екатеринбург')
          .split(',')
          .map(c => c.trim())
          .filter(Boolean);
        cities = fallback.slice(0, 8);
      }
    } catch (e) {
      console.error('[Bot] showSearchMenu cities:', e.message);
    }

    const cityBtns = cities.map(city => {
      const active = f.city === city;
      return { text: (active ? '✅ ' : '🏙 ') + city, callback_data: 'search_city_' + encodeURIComponent(city) };
    });

    // Count matching models for the current filters
    let matchCount = 0;
    try {
      const conditions = ['available=1'];
      const params = [];
      if (f.height_min != null) {
        conditions.push('height >= ?');
        params.push(f.height_min);
      }
      if (f.height_max != null && f.height_max < 999) {
        conditions.push('height <= ?');
        params.push(f.height_max);
      }
      if (f.age_min != null) {
        conditions.push('age >= ?');
        params.push(f.age_min);
      }
      if (f.age_max != null && f.age_max < 99) {
        conditions.push('age <= ?');
        params.push(f.age_max);
      }
      if (f.category) {
        conditions.push('category = ?');
        params.push(f.category);
      }
      if (f.city) {
        conditions.push('city = ?');
        params.push(f.city);
      }
      const countRow = await get(`SELECT COUNT(*) as cnt FROM models WHERE ${conditions.join(' AND ')}`, params);
      matchCount = countRow?.cnt || 0;
    } catch (e) {
      console.error('[Bot] showSearchMenu count:', e.message);
    }

    // Build active filter summary for heading
    const activeParts = [];
    if (f.height_min != null) {
      const r = heightRanges.find(r => r.min === f.height_min);
      if (r) activeParts.push(`📏 ${r.label}`);
      else activeParts.push(`📏 ${f.height_min}–${f.height_max} см`);
    }
    if (f.age_min != null) {
      const r = ageRanges.find(r => r.min === f.age_min);
      if (r) activeParts.push(`🎂 ${r.label}`);
      else activeParts.push(`🎂 ${f.age_min}–${f.age_max} лет`);
    }
    if (f.category) activeParts.push(`🏷 ${f.category}`);
    if (f.city) activeParts.push(`🏙 ${f.city}`);

    const hasFilters = activeParts.length > 0;
    const summaryLine = hasFilters
      ? `\n\n_Выбрано: ${esc(activeParts.join(', '))}_`
      : `\n\n_Выберите фильтры для поиска_`;

    // Find button label: show count only when filters selected
    const findLabel = hasFilters ? `🔍 Найти (${matchCount})` : `🔍 Найти всех (${matchCount})`;

    // City input button — shows active city or prompt to type
    const cityInputLabel = f.city ? `✅ 🏙 ${f.city}` : '✏️ Ввести город';
    const cityInputBtn = { text: cityInputLabel, callback_data: 'search_city_input' };

    const keyboard = [
      ...heightBtns,
      ...ageBtns,
      [catBtns[0], catBtns[1], catBtns[2]],
      ...(cityBtns.length ? [cityBtns.slice(0, 4)] : []),
      ...(cityBtns.length > 4 ? [cityBtns.slice(4)] : []),
      [cityInputBtn],
      [
        ...(hasFilters ? [{ text: '✖️ Сбросить фильтры', callback_data: 'search_reset' }] : []),
        { text: findLabel, callback_data: 'search_go' },
      ],
      [{ text: '← Назад', callback_data: 'cat_cat__0' }],
    ];

    return safeSend(chatId, `🔍 *Поиск моделей*${summaryLine}`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: keyboard },
    });
  } catch (e) {
    console.error('[Bot] showSearchMenu:', e.message);
    return safeSend(chatId, '⚠️ Ошибка загрузки меню поиска. Попробуйте ещё раз.');
  }
}

async function showSearchResults(chatId, filters, page = 0) {
  try {
    page = parseInt(page) || 0;
    const perPage = 6;

    const conditions = ['available=1'];
    const params = [];
    if (filters.height_min != null) {
      conditions.push('height >= ?');
      params.push(filters.height_min);
    }
    if (filters.height_max != null && filters.height_max < 999) {
      conditions.push('height <= ?');
      params.push(filters.height_max);
    }
    if (filters.age_min != null) {
      conditions.push('age >= ?');
      params.push(filters.age_min);
    }
    if (filters.age_max != null && filters.age_max < 99) {
      conditions.push('age <= ?');
      params.push(filters.age_max);
    }
    if (filters.category) {
      conditions.push('category = ?');
      params.push(filters.category);
    }
    if (filters.city) {
      conditions.push('city = ?');
      params.push(filters.city);
    }

    const where = conditions.join(' AND ');
    const models = await query(
      `SELECT id, name, age, height, city, category, photo_main, photos FROM models WHERE ${where} ORDER BY featured DESC, name`,
      params
    );
    const total = models.length;
    const totalPages = Math.ceil(total / perPage) || 1;

    if (!total) {
      return safeSend(chatId, STRINGS.searchNoResults, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '🔍 Изменить поиск', callback_data: 'cat_search' }],
            [{ text: '✖️ Сбросить фильтры', callback_data: 'srch_reset' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
          ],
        },
      });
    }

    const slice = models.slice(page * perPage, page * perPage + perPage);

    // Build result text
    let text = `🔍 *Результаты поиска*\n\nНайдено: *${total}* ${ru_plural(total, 'модель', 'модели', 'моделей')}`;
    if (totalPages > 1) text += ` \\(стр\\. ${esc(String(page + 1))}/${esc(String(totalPages))}\\)`;
    text += '\n\n';

    slice.forEach((m, i) => {
      text += `${page * perPage + i + 1}\\. *${esc(m.name)}*`;
      const parts = [];
      if (m.city) parts.push(esc(m.city));
      if (m.height) parts.push(`${m.height} см`);
      if (m.age) parts.push(`${m.age} лет`);
      if (m.category) parts.push(esc(m.category));
      if (parts.length) text += ` — ${parts.join(' · ')}`;
      text += '\n';
    });

    // Model buttons (one per row)
    const modelBtns = slice.map(m => {
      const label = `👁 ${m.name}` + (m.city ? ` · ${m.city}` : '') + (m.height ? ` · ${m.height}см` : '');
      return [{ text: label, callback_data: `srch_view_${m.id}` }];
    });

    // Navigation row
    const nav = [];
    if (page > 0) nav.push({ text: '◀️ Пред', callback_data: `srch_page_${page - 1}` });
    nav.push({ text: `${page + 1}/${totalPages}`, callback_data: 'srch_noop' });
    if ((page + 1) * perPage < total) nav.push({ text: 'След ▶️', callback_data: `srch_page_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...modelBtns,
          nav,
          [
            { text: '← Изменить поиск', callback_data: 'cat_search' },
            { text: '✖️ Сбросить', callback_data: 'srch_reset' },
          ],
          [{ text: '🏠 Меню', callback_data: 'main_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showSearchResults:', e.message);
  }
}

// Advanced search v2 reads filters from searchFilters Map

async function showSearchResultsV2(chatId, page) {
  try {
    page = parseInt(page) || 0;
    const perPage = 5;
    const filters = getSearchFilters(chatId);

    const conditions = ['available=1'];
    const params = [];
    if (filters.height_min != null && filters.height_max != null) {
      conditions.push('height BETWEEN ? AND ?');
      params.push(filters.height_min, filters.height_max);
    }
    if (filters.age_min != null && filters.age_max != null) {
      conditions.push('age BETWEEN ? AND ?');
      params.push(filters.age_min, filters.age_max);
    }
    if (filters.category) {
      conditions.push('category = ?');
      params.push(filters.category);
    }
    if (filters.city) {
      conditions.push('LOWER(city) LIKE LOWER(?)');
      params.push(filters.city);
    }

    const where = conditions.join(' AND ');
    const countRow = await get(`SELECT COUNT(*) as cnt FROM models WHERE ${where}`, params);
    const total = countRow?.cnt || 0;
    const totalPages = Math.ceil(total / perPage) || 1;

    if (!total) {
      return safeSend(
        chatId,
        '🔍 *Поиск моделей*\n\nПо вашим критериям моделей не найдено\\. Попробуйте расширить фильтры\\.',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '✖️ Сбросить фильтры', callback_data: 'search_reset' }],
              [{ text: '← Изменить поиск', callback_data: 'cat_search' }],
              [{ text: '🏠 Меню', callback_data: 'main_menu' }],
            ],
          },
        }
      );
    }

    const models = await query(
      `SELECT id, name, age, height, city, category FROM models WHERE ${where} ORDER BY featured DESC, name ASC LIMIT ? OFFSET ?`,
      [...params, perPage, page * perPage]
    );

    let text = `🔍 *Результаты поиска*\n\nНайдено: *${total}* ${ru_plural(total, 'модель', 'модели', 'моделей')}`;
    if (totalPages > 1) text += ` \\(стр\\. ${esc(String(page + 1))}/${esc(String(totalPages))}\\)`;
    text += '\n\n';

    models.forEach((m, i) => {
      text += `${page * perPage + i + 1}\\. *${esc(m.name)}*`;
      const parts = [];
      if (m.city) parts.push(esc(m.city));
      if (m.height) parts.push(`${m.height} см`);
      if (m.age) parts.push(`${m.age} лет`);
      if (m.category) parts.push(esc(m.category));
      if (parts.length) text += ` — ${parts.join(' · ')}`;
      text += '\n';
    });

    const modelBtns = models.map(m => {
      const label = '👁 ' + m.name + (m.city ? ' · ' + m.city : '') + (m.height ? ' · ' + m.height + 'см' : '');
      return [{ text: label, callback_data: 'srch_view_' + m.id }];
    });

    const nav = [];
    if (page > 0) nav.push({ text: '◀️ Пред', callback_data: 'search_page_' + (page - 1) });
    nav.push({ text: page + 1 + '/' + totalPages, callback_data: 'srch_noop' });
    if ((page + 1) * perPage < total) nav.push({ text: 'След ▶️', callback_data: 'search_page_' + (page + 1) });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...modelBtns,
          ...(totalPages > 1 ? [nav] : []),
          [
            { text: '← Изменить поиск', callback_data: 'cat_search' },
            { text: '✖️ Сбросить', callback_data: 'search_reset' },
          ],
          [{ text: '🏠 Меню', callback_data: 'main_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showSearchResultsV2:', e.message);
  }
}

// ─── AI підбір моделей ────────────────────────────────────────────────────────

async function startAiMatch(chatId) {
  await setSession(chatId, 'state', 'ai_match_desc');
  return safeSend(
    chatId,
    '🤖 *AI подбор модели*\n\nОпишите ваше мероприятие — и AI подберёт лучших моделей из каталога\\.\n\n_Например: "Корпоратив на 50 человек в Москве, нужны 2 модели для встречи гостей, бюджет 30000₽"_\n\n💡 Чем подробнее описание — тем точнее подбор\\.\n\n_Или /cancel для отмены_',
    { parse_mode: 'MarkdownV2' }
  );
}

async function runAiMatch(chatId, userDesc) {
  try {
    // Get available models
    const models = await query(
      'SELECT id, name, age, height, city, category, bio, featured FROM models WHERE available=1 AND (archived=0 OR archived IS NULL) ORDER BY featured DESC, id ASC LIMIT 20'
    );
    if (!models.length) return safeSend(chatId, '😔 Каталог пуст\\. Попробуйте позже\\.', { parse_mode: 'MarkdownV2' });

    const catalog = models
      .map(
        m =>
          `ID:${m.id} ${m.name}, ${m.age}р, ${m.height}см, ${m.city || '—'}, ${m.category}${m.bio ? ', ' + m.bio.slice(0, 80) : ''}`
      )
      .join('\n');

    const prompt = `Ти — AI-асистент модельного агентства. Клієнт описав своє замовлення: "${userDesc}"\n\nДоступні моделі:\n${catalog}\n\nПідбери 3 найкращих моделей для цього замовлення. Поясни чому кожна підходить (1-2 речення). Відповідь лише у форматі:\n1. ID:XX - Ім'я - Причина\n2. ID:XX - Ім'я - Причина\n3. ID:XX - Ім'я - Причина\n\nТільки ці 3 рядки, без зайвого тексту.`;

    const { spawn } = require('child_process');
    let output = '';
    let errOut = '';
    const proc = spawn('claude', ['-p', prompt, '--output-format', 'text'], {
      cwd: '/home/user/Pablo/nevesty-models',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    proc.stdout.on('data', d => {
      output += d.toString();
    });
    proc.stderr.on('data', d => {
      errOut += d.toString();
    });

    proc.on('close', async code => {
      if (!output.trim() || code !== 0) {
        console.error('[AI Match] error:', errOut);
        return safeSend(
          chatId,
          '⚠️ AI тимчасово недоступний\\. Спробуйте пізніше або скористайтесь ручним пошуком\\.',
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '🔍 Ручний пошук', callback_data: 'cat_search' }]] },
          }
        );
      }

      // Parse model IDs from response
      const lines = output
        .trim()
        .split('\n')
        .filter(l => l.trim());
      const modelIds = lines
        .map(l => {
          const m = l.match(/ID:(\d+)/i);
          return m ? parseInt(m[1]) : null;
        })
        .filter(Boolean);

      // Build inline keyboard with matched models
      const keyboard = [];
      for (const id of modelIds.slice(0, 3)) {
        const m = models.find(x => x.id === id);
        if (m)
          keyboard.push([
            { text: `💃 ${m.name} (${m.age}р, ${m.height}см, ${m.city || '—'})`, callback_data: `model_${id}` },
          ]);
      }
      keyboard.push([{ text: '🔍 Розширений пошук', callback_data: 'cat_search' }]);
      keyboard.push([{ text: '🏠 Головне меню', callback_data: 'main_menu' }]);

      const aiResult = esc(output.trim().slice(0, 800));
      await safeSend(
        chatId,
        `🤖 *AI підбір завершено:*\n\n${aiResult}\n\n_Натисніть на модель щоб переглянути профіль:_`,
        { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } }
      );
    });

    proc.on('error', async err => {
      console.error('[AI Match] spawn error:', err.message);
      return safeSend(chatId, '⚠️ AI недоступний\\. Скористайтесь ручним пошуком\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🔍 Пошук', callback_data: 'cat_search' }]] },
      });
    });
  } catch (e) {
    console.error('[AI Match] error:', e.message);
    return safeSend(chatId, '⚠️ Помилка\\. Спробуйте пізніше\\.', { parse_mode: 'MarkdownV2' });
  }
}

// ─── Публичные отзывы ─────────────────────────────────────────────────────────

async function showPublicReviews(chatId, page) {
  page = parseInt(page) || 0;
  try {
    const perPage = 3;
    const totalRow = await get('SELECT COUNT(*) as n FROM reviews WHERE approved=1').catch(() => ({ n: 0 }));
    const total = totalRow.n;

    if (!total) {
      return safeSend(chatId, '📭 *Пока нет отзывов*\n\nБудьте первым\\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '⭐ Оставить отзыв', callback_data: 'leave_review_0' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
          ],
        },
      });
    }

    const reviews = await query(
      `SELECT r.*, m.name as model_name
       FROM reviews r
       LEFT JOIN models m ON r.model_id = m.id
       WHERE r.approved=1 ORDER BY r.created_at DESC LIMIT ? OFFSET ?`,
      [perPage, page * perPage]
    ).catch(() => []);

    const totalPages = Math.ceil(total / perPage);
    let text = `_🏠 Главная › ⭐ Отзывы \\(стр\\. ${page + 1}/${totalPages}\\)_\n\n`;
    text += `⭐ *Отзывы клиентов \\(${total}\\)*\n\n`;
    reviews.forEach(r => {
      const stars = '⭐'.repeat(Math.max(1, Math.min(5, r.rating || 5)));
      const date = r.created_at ? new Date(r.created_at).toLocaleDateString('ru') : '';
      const snippet = r.text ? (r.text.length > 200 ? r.text.slice(0, 200) + '…' : r.text) : '';
      text += `${stars}`;
      if (r.model_name) text += ` Модель: _${esc(r.model_name)}_`;
      text += `\n_"${esc(snippet)}"_`;
      if (date) text += `\n📅 ${esc(date)}`;
      if (r.admin_reply) text += `\n💬 _${esc(r.admin_reply)}_`;
      text += '\n\n';
    });

    const nav = [];
    if (page > 0) nav.push({ text: '← Пред', callback_data: `cat_rev_${page - 1}` });
    nav.push({ text: `${page + 1}/${totalPages}`, callback_data: 'noop' });
    if ((page + 1) * perPage < total) nav.push({ text: 'След →', callback_data: `cat_rev_${page + 1}` });

    const reviewsEnabled = await getSetting('reviews_enabled').catch(() => '1');

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          nav,
          ...(reviewsEnabled !== '0' ? [[{ text: '✍️ Оставить отзыв', callback_data: 'leave_review_0' }]] : []),
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showPublicReviews:', e.message);
  }
}

// ─── Оставить отзыв ───────────────────────────────────────────────────────────

async function startLeaveReview(chatId, orderId) {
  orderId = parseInt(orderId) || 0;

  // Check reviews_min_completed setting
  const minCompletedRaw = await getSetting('reviews_min_completed').catch(() => null);
  const minCompleted = parseInt(minCompletedRaw) || 0;
  if (minCompleted > 0) {
    const completedRow = await get("SELECT COUNT(*) as cnt FROM orders WHERE client_chat_id=? AND status='completed'", [
      String(chatId),
    ]).catch(() => ({ cnt: 0 }));
    if ((completedRow?.cnt || 0) < minCompleted) {
      return safeSend(
        chatId,
        `⚠️ Для написания отзыва нужно завершить минимум *${minCompleted}* заявок\\.\nУ вас завершено: *${completedRow?.cnt || 0}*\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
        }
      );
    }
  }

  // Validate order if orderId provided
  if (orderId) {
    const order = await get('SELECT id, order_number, status FROM orders WHERE id=? AND client_chat_id=?', [
      orderId,
      String(chatId),
    ]).catch(() => null);
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
    const existing = await get('SELECT id FROM reviews WHERE chat_id=? AND order_id=?', [
      String(chatId),
      orderId,
    ]).catch(() => null);
    if (existing) {
      return safeSend(chatId, STRINGS.reviewAlreadyLeftForOrder, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
      });
    }
  }

  const text = orderId
    ? `⭐ *Оставить отзыв о заявке*\n\nОцените работу агентства по 5\\-балльной шкале:`
    : `⭐ *Оставить отзыв*\n\nОцените работу агентства Nevesty Models\\!`;

  const ratingRow = [1, 2, 3, 4, 5].map(n => ({
    text: '⭐'.repeat(n),
    callback_data: `rev_rate_${n}_${orderId}`,
  }));

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: { inline_keyboard: [ratingRow, [{ text: '❌ Отмена', callback_data: 'main_menu' }]] },
  });
}

// ─── Повторити заявку ─────────────────────────────────────────────────────────

async function repeatOrder(chatId, orderId) {
  try {
    const o = await get('SELECT * FROM orders WHERE id=? AND client_chat_id=?', [orderId, String(chatId)]);
    if (!o) {
      return safeSend(chatId, RU.ORDER_NOT_FOUND, {
        reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] },
      });
    }

    // Check model availability
    let modelId = null;
    let modelName = null;
    if (o.model_id) {
      const m = await get('SELECT id,name,available FROM models WHERE id=?', [o.model_id]).catch(() => null);
      if (m?.available) {
        modelId = m.id;
        modelName = m.name;
      }
    }

    // Pre-fill session with all data from original order
    const prefill = {
      repeat_from: orderId,
      client_name: o.client_name,
      client_phone: o.client_phone,
      client_email: o.client_email || null,
      client_telegram: o.client_telegram || null,
      event_type: o.event_type || null,
      event_duration: o.event_duration || null,
      location: o.location || null,
      budget: o.budget || null,
      comments: o.comments || null,
      model_id: modelId,
      model_name: modelName,
    };

    await setSession(chatId, 'bk_repeat_confirm', prefill);

    // Build summary message
    const eventLabel = prefill.event_type ? EVENT_TYPES[prefill.event_type] || prefill.event_type : '—';

    let text = `🔁 *Повторить заявку?*\n\n`;
    text += `👤 Ім'я: ${esc(prefill.client_name || '—')}\n`;
    text += `📱 Телефон: ${esc(prefill.client_phone || '—')}\n`;
    if (prefill.client_email) text += `📧 Email: ${esc(prefill.client_email)}\n`;
    text += `\n`;
    text += `🎭 Тип події: ${esc(eventLabel)}\n`;
    if (prefill.event_duration) text += `⏱ Тривалість: ${esc(String(prefill.event_duration))} год\\.\n`;
    if (prefill.location) text += `📍 Місце: ${esc(prefill.location)}\n`;
    if (prefill.budget) text += `💰 Бюджет: ${esc(prefill.budget)}\n`;
    if (prefill.model_name) text += `💃 Модель: ${esc(prefill.model_name)}\n`;
    text += `\n_Нову заявку буде створено зі статусом «нова»\\._`;

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '✅ Підтвердити', callback_data: 'bk_repeat_confirm' }],
          [{ text: '❌ Скасувати', callback_data: 'bk_repeat_cancel' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] repeatOrder:', e.message);
  }
}

async function bkRepeatSubmit(chatId, d, tgUsername) {
  try {
    // Check active orders limit
    const maxActive = parseInt(await getSetting('client_max_active_orders').catch(() => '10')) || 10;
    const activeCountRow = await get(
      "SELECT COUNT(*) as cnt FROM orders WHERE client_chat_id=? AND status NOT IN ('completed','cancelled')",
      [String(chatId)]
    ).catch(() => ({ cnt: 0 }));
    if ((activeCountRow?.cnt || 0) >= maxActive) {
      await clearSession(chatId);
      return safeSend(chatId, '⚠️ *Перевищено ліміт активних заявок*\\.\nЗачекайте завершення поточних заявок\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '📋 Мої заявки', callback_data: 'my_orders' }],
            [{ text: '🏠 Головне меню', callback_data: 'main_menu' }],
          ],
        },
      });
    }

    if (tgUsername && !d.client_telegram) d.client_telegram = tgUsername;

    const orderNum = generateOrderNumber();
    await run(
      `INSERT INTO orders
        (order_number,client_name,client_phone,client_email,client_telegram,
         client_chat_id,model_id,event_type,event_date,event_duration,
         location,budget,comments,status)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'new')`,
      [
        orderNum,
        d.client_name,
        d.client_phone,
        d.client_email || null,
        d.client_telegram || null,
        String(chatId),
        d.model_id || null,
        d.event_type || null,
        null, // no date — user needs to clarify with manager
        parseInt(d.event_duration) || 4,
        d.location || null,
        d.budget || null,
        d.comments || null,
      ]
    );
    const order = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]);

    // Post-insert race condition check: verify active order count wasn't exceeded
    const activeAfterInsert = await get(
      "SELECT COUNT(*) as n FROM orders WHERE client_chat_id=? AND status NOT IN ('completed','cancelled')",
      [String(chatId)]
    ).catch(() => ({ n: 0 }));
    if (maxActive > 0 && (activeAfterInsert?.n || 0) > maxActive) {
      await run('DELETE FROM orders WHERE order_number=?', [orderNum]).catch(() => {});
      await clearSession(chatId);
      return safeSend(
        chatId,
        '❌ У вас уже слишком много активных заявок\\. Пожалуйста, дождитесь завершения текущих\\.',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '📋 Мої заявки', callback_data: 'my_orders' }]] },
        }
      );
    }

    await clearSession(chatId);

    await safeSend(
      chatId,
      `🎉 *Заявку прийнято\\!*\n\nНомер: *${esc(orderNum)}*\n\nМенеджер зв'яжеться з вами протягом 1 години для підтвердження\\.`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '📋 Мої заявки', callback_data: 'my_orders' }],
            [{ text: '🏠 Головне меню', callback_data: 'main_menu' }],
          ],
        },
      }
    );

    if (order) {
      notifyNewOrder(order);
      if (mailer) {
        if (order.client_email) {
          mailer
            .sendOrderConfirmation(order.client_email, order)
            .catch(e => console.error('[mailer] repeat order confirm:', e.message));
        }
        mailer.getAdminEmails().forEach(adminEmail => {
          mailer.sendManagerNotification(adminEmail, order).catch(() => {});
        });
      }
      try {
        const { notifyCRM } = require('./services/crm');
        notifyCRM('order.created', order, getSetting).catch(e => console.error('[CRM] repeat:', e.message));
      } catch {}
    }
  } catch (e) {
    console.error('[Bot] bkRepeatSubmit:', e.message);
    await clearSession(chatId);
    return safeSend(chatId, '❌ Помилка при створенні заявки\\. Спробуйте ще раз\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '🏠 Меню', callback_data: 'main_menu' }]] },
    });
  }
}

// ─── Редактировать профиль ────────────────────────────────────────────────────

async function startEditProfile(chatId) {
  try {
    const lastOrder = await get(
      'SELECT client_name, client_phone, client_email FROM orders WHERE client_chat_id=? ORDER BY created_at DESC LIMIT 1',
      [String(chatId)]
    ).catch(() => null);

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
      reply_markup: {
        inline_keyboard: [
          [{ text: "👤 Змінити ім'я", callback_data: 'profile_edit_name' }],
          [{ text: '📞 Изменить телефон', callback_data: 'profile_edit_phone' }],
          [{ text: '← Профиль', callback_data: 'profile' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] startEditProfile:', e.message);
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE A: Избранные модели (Wishlist) ───────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function addToWishlist(chatId, modelId) {
  await run('INSERT OR IGNORE INTO wishlists (chat_id, model_id) VALUES (?,?)', [String(chatId), modelId]);
  // Also sync to favorites table for compatibility
  await run('INSERT OR IGNORE INTO favorites (chat_id, model_id) VALUES (?,?)', [String(chatId), modelId]).catch(
    () => {}
  );
}

async function removeFromWishlist(chatId, modelId) {
  await run('DELETE FROM wishlists WHERE chat_id=? AND model_id=?', [String(chatId), modelId]);
  await run('DELETE FROM favorites WHERE chat_id=? AND model_id=?', [String(chatId), modelId]).catch(() => {});
}

async function isInWishlist(chatId, modelId) {
  const row = await get('SELECT id FROM wishlists WHERE chat_id=? AND model_id=?', [String(chatId), modelId]).catch(
    () => null
  );
  return !!row;
}

// ─── Wishlist (wishlists table) ───────────────────────────────────────────────

async function showWishlist(chatId, page = 0) {
  try {
    const enabled = await getSetting('wishlist_enabled').catch(() => '1');
    if (enabled === '0') {
      return safeSend(chatId, STRINGS.wishlistUnavailable, {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
      });
    }

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
      return safeSend(chatId, STRINGS.wishlistEmpty, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '💃 Перейти в каталог', callback_data: 'cat_cat__0' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
          ],
        },
      });
    }

    const totalRow = await get('SELECT COUNT(*) as c FROM wishlists WHERE chat_id=?', [String(chatId)]).catch(() => ({
      c: items.length,
    }));

    let text = `_🏠 Главная › ❤️ Избранное_\n\n`;
    text += `❤️ *Избранные модели* \\(${totalRow.c}\\)\n\n`;
    const keyboard = [];
    for (const m of items) {
      const star = m.featured ? '⭐ ' : '';
      const cat = MODEL_CATEGORIES[m.category] || m.category || '';
      const city = m.city ? ` · ${esc(m.city)}` : '';
      text += `${star}*${esc(m.name)}* · ${esc(cat)}${city}\n`;
      keyboard.push([
        { text: `👁 ${m.name}`, callback_data: `fav_view_${m.id}` },
        { text: '❌ Убрать', callback_data: `fav_remove_${m.id}` },
      ]);
    }

    const navRow = [];
    if (page > 0) navRow.push({ text: '◀️ Назад', callback_data: `fav_list_${page - 1}` });
    if (hasMore) navRow.push({ text: 'Вперёд ▶️', callback_data: `fav_list_${page + 1}` });
    if (navRow.length) keyboard.push(navRow);
    keyboard.push([{ text: '🗑 Очистить список', callback_data: 'fav_clear' }]);
    keyboard.push([
      { text: '💃 Каталог', callback_data: 'cat_cat__0' },
      { text: '🏠 Меню', callback_data: 'main_menu' },
    ]);

    return safeSend(chatId, text, { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } });
  } catch (e) {
    console.error('[Bot] showWishlist:', e.message);
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE B: Быстрая заявка (Quick Booking) ────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function bkQuickStart(chatId) {
  await setSession(chatId, 'bk_quick_name', {});
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    `${STRINGS.quickBookingTitle}\n\n${STRINGS.quickBookingIntro}\n\n${STRINGS.quickBookingStep1}`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '📋 Полная форма', callback_data: 'bk_start' }],
          [{ text: '❌ Отменить', callback_data: 'main_menu' }],
        ],
      },
    }
  );
}

async function bkQuickPhone(chatId, data) {
  await setSession(chatId, 'bk_quick_phone', data);
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    `${STRINGS.quickBookingTitle}\n\n✅ Имя: *${esc(data.quick_name)}*\n\n${STRINGS.quickBookingStep2}`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'main_menu' }]] },
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
    await safeSend(
      chatId,
      `⚡ *Заявка принята\\!*\n\nНомер: *${esc(orderNum)}*\n\nМенеджер позвонит на *${esc(data.quick_phone)}* в ближайшее время\\.`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
          ],
        },
      }
    );
    if (order) notifyNewOrder(order);
  } catch (e) {
    console.error('[Bot] bkQuickSubmit:', e.message);
    await clearSession(chatId);
    return safeSend(chatId, STRINGS.errorSend, { parse_mode: 'MarkdownV2' });
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE D: Поиск по росту — ввод диапазона вручную ──────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function showHeightSearchInput(chatId) {
  await setSession(chatId, 'search_height', {});
  return safeSend(
    chatId,
    `📏 *Поиск моделей по росту*\n\nВведите диапазон роста в формате:\n*170\\-180* или одно значение *175*\n\n_Или выберите быстрый диапазон:_`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [
            { text: '160–165 см', callback_data: 'cat_search_height_160-165' },
            { text: '165–170 см', callback_data: 'cat_search_height_165-170' },
          ],
          [
            { text: '170–175 см', callback_data: 'cat_search_height_170-175' },
            { text: '175–185 см', callback_data: 'cat_search_height_175-185' },
          ],
          [{ text: '← Поиск', callback_data: 'cat_search' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
        ],
      },
    }
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE E: Расширенный дашборд администратора ───────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function showAdminDashboard(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const now = new Date();
    const todayStr = now.toISOString().slice(0, 10);
    const weekAgo = new Date(now - 7 * 86400000).toISOString().slice(0, 10);
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
      reply_markup: {
        inline_keyboard: [
          [
            { text: '📋 Заявки', callback_data: 'adm_orders__0' },
            { text: '📊 Статистика', callback_data: 'adm_stats' },
          ],
          [{ text: '← Меню', callback_data: 'admin_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showAdminDashboard:', e.message);
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE: AI Tech Spec Generator ─────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

// Returns a plain-text spec (for DB storage) and a MarkdownV2-safe display version
function generateTechSpec(description) {
  const d = description.toLowerCase();
  // Extract or guess event type
  let eventType = 'Другое';
  if (/корпоратив|корп\./.test(d)) eventType = 'Корпоратив';
  else if (/фотосесс|фотограф/.test(d)) eventType = 'Фотосессия';
  else if (/показ|fashion|дефиле/.test(d)) eventType = 'Показ мод';
  else if (/выставк|конференц/.test(d)) eventType = 'Выставка';
  else if (/реклам|видео/.test(d)) eventType = 'Реклама';

  // Guess model count
  let modelCount = '1';
  const numMatch = d.match(/(\d+)\s*(модел|хостес|деву)/);
  if (numMatch) modelCount = numMatch[1];

  // Look for budget mentions
  let budget = 'По запросу';
  const budgetMatch = description.match(/(\d[\d\s]*(?:тыс|руб|₽|k|000))/i);
  if (budgetMatch) budget = budgetMatch[1].trim();

  // Look for date
  let date = 'Уточняется';
  const dateMatch = description.match(/\d{1,2}[\.\/\-]\d{1,2}(?:[\.\/\-]\d{2,4})?/);
  if (dateMatch) date = dateMatch[0];

  // Plain-text spec (for DB storage and admin notifications)
  const plainSpec =
    `📋 ТЕХНИЧЕСКОЕ ЗАДАНИЕ\n\n` +
    `Тип мероприятия: ${eventType}\n` +
    `Количество моделей: ${modelCount}\n` +
    `Бюджет: ${budget}\n` +
    `Дата: ${date}\n\n` +
    `Описание от клиента:\n${description}\n\n` +
    `Сгенерировано автоматически на основе вашего описания`;

  // MarkdownV2 display (escape all user-provided values)
  const mdSpec =
    `📋 *ТЕХНИЧЕСКОЕ ЗАДАНИЕ*\n\n` +
    `*Тип мероприятия:* ${esc(eventType)}\n` +
    `*Количество моделей:* ${esc(modelCount)}\n` +
    `*Бюджет:* ${esc(budget)}\n` +
    `*Дата:* ${esc(date)}\n\n` +
    `*Описание от клиента:*\n${esc(description)}\n\n` +
    `_Сгенерировано автоматически на основе вашего описания_`;

  return { plainSpec, mdSpec };
}

async function startTechSpec(chatId) {
  await setSession(chatId, 'techspec_input', {});
  resetSessionTimer(chatId);
  return safeSend(
    chatId,
    'Опишите ваше мероприятие в свободной форме — дата, место, количество гостей, задачи для моделей, бюджет\\. Я создам техническое задание для агентства\\.',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] },
    }
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── Hook new features into the bot after initBot() ──────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

function _registerNewFeatures() {
  if (!bot) return;

  // ── Additional callback_query handlers ─────────────────────────────────────
  bot.on('callback_query', async q => {
    const chatId = q.message.chat.id;
    const msgId = q.message.message_id;
    const data = q.data;
    try {
      await bot.answerCallbackQuery(q.id);
    } catch {}

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
      return showModel(chatId, parseInt(data.replace('fav_view_', '')));
    }

    // Wishlist clear — ask confirmation
    if (data === 'fav_clear') {
      return safeSend(chatId, '🗑 *Очистить список избранного?*\n\nВсе модели будут удалены из вашего списка\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [
              { text: '✅ Да, очистить', callback_data: 'fav_clear_yes' },
              { text: '❌ Отмена', callback_data: 'fav_list_0' },
            ],
          ],
        },
      });
    }

    // Wishlist clear — confirmed
    if (data === 'fav_clear_yes') {
      try {
        await bot.answerCallbackQuery(q.id, { text: '🗑 Список очищен' });
      } catch {}
      await run('DELETE FROM wishlists WHERE chat_id=?', [String(chatId)]).catch(() => {});
      await run('DELETE FROM favorites WHERE chat_id=?', [String(chatId)]).catch(() => {});
      return safeSend(chatId, '🗑 *Список избранного очищен\\.*\n\nВы можете добавить новые модели из каталога\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '💃 Перейти в каталог', callback_data: 'cat_cat__0' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
          ],
        },
      });
    }

    // Favorites add — add to wishlists, answer callback with toast, edit keyboard
    if (data.startsWith('fav_add_')) {
      const favModelId = parseInt(data.replace('fav_add_', ''));
      if (!favModelId || favModelId <= 0) return;
      const favModel = await get('SELECT id, name FROM models WHERE id=?', [favModelId]).catch(() => null);
      if (!favModel) {
        try {
          await bot.answerCallbackQuery(q.id, { text: '❌ Модель не найдена', show_alert: true });
        } catch {}
        return;
      }
      await addToWishlist(chatId, favModelId);
      try {
        await bot.answerCallbackQuery(q.id, { text: STRINGS.wishlistAdded });
      } catch {}
      try {
        const favKb = (q.message.reply_markup?.inline_keyboard || []).map(row =>
          row.map(btn =>
            btn.callback_data === `fav_add_${favModelId}`
              ? { text: '💔 Убрать из избранного', callback_data: `fav_remove_${favModelId}` }
              : btn
          )
        );
        await bot.editMessageReplyMarkup({ inline_keyboard: favKb }, { chat_id: chatId, message_id: msgId });
      } catch {}
      return;
    }

    // Favorites remove — remove from wishlists, answer callback with toast, edit keyboard
    if (data.startsWith('fav_remove_')) {
      const remModelId = parseInt(data.replace('fav_remove_', ''));
      if (!remModelId || remModelId <= 0) return;
      await removeFromWishlist(chatId, remModelId);
      try {
        await bot.answerCallbackQuery(q.id, { text: STRINGS.wishlistRemoved });
      } catch {}
      try {
        const remKb = q.message.reply_markup?.inline_keyboard || [];
        if (remKb.some(row => row.some(btn => btn.callback_data === `fav_view_${remModelId}`))) {
          return showWishlist(chatId, 0);
        }
        const newRemKb = remKb.map(row =>
          row.map(btn =>
            btn.callback_data === `fav_remove_${remModelId}`
              ? { text: '❤️ В избранное', callback_data: `fav_add_${remModelId}` }
              : btn
          )
        );
        await bot.editMessageReplyMarkup({ inline_keyboard: newRemKb }, { chat_id: chatId, message_id: msgId });
      } catch {}
      return;
    }

    // Category search
    if (data.startsWith('cat_search_cat_')) {
      const cat = data.replace('cat_search_cat_', '');
      if (!['fashion', 'commercial', 'events'].includes(cat)) return;
      return showCatalog(chatId, cat, 0);
    }

    // Quick booking
    if (data === 'bk_quick') return bkQuickStart(chatId);

    // Tech spec generator: start
    if (data === 'techspec_start') return startTechSpec(chatId);

    // Tech spec generator: confirm — send to manager
    if (data.startsWith('techspec_confirm_yes_')) {
      const tsChatId = data.replace('techspec_confirm_yes_', '');
      if (String(chatId) !== String(tsChatId)) return;
      const sess = await getSession(chatId);
      const sd = sessionData(sess);
      if (!sd || !sd.spec) {
        return safeSend(chatId, '❌ Данные не найдены\\. Попробуйте ещё раз\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] },
        });
      }
      try {
        const orderNum = generateOrderNumber();
        const tsClientName = [q.from.first_name, q.from.last_name].filter(Boolean).join(' ') || 'Клиент (тех. задание)';
        await run(
          `INSERT INTO orders (order_number,client_name,client_phone,event_type,comments,client_chat_id,status)
           VALUES (?,?,?,'other',?,?,'new')`,
          [orderNum, tsClientName, '', sd.spec, String(chatId)]
        );
        const order = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]);
        await clearSession(chatId);
        await safeSend(
          chatId,
          `✅ *Техническое задание отправлено менеджеру\\!*\n\nНомер заявки: *${esc(orderNum)}*\n\nМы свяжемся с вами в ближайшее время\\.`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: {
              inline_keyboard: [
                [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
                [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
              ],
            },
          }
        );
        if (order) {
          const adminIds = await getAdminChatIds();
          const clientName = [q.from.first_name, q.from.last_name].filter(Boolean).join(' ') || 'Клиент';
          const username = q.from.username ? ` @${q.from.username}` : '';
          await Promise.allSettled(
            adminIds.map(id =>
              safeSend(
                id,
                `📋 *Новое тех\\. задание от клиента*\n\nОт: ${esc(clientName)}${esc(username)}\nTelegram ID: \`${chatId}\`\n\n${esc(sd.spec)}\n\n📋 Заявка: *${esc(orderNum)}*`,
                {
                  parse_mode: 'MarkdownV2',
                  reply_markup: {
                    inline_keyboard: [
                      [
                        { text: '✅ Подтвердить', callback_data: `adm_confirm_${order.id}` },
                        { text: '❌ Отклонить', callback_data: `adm_reject_${order.id}` },
                      ],
                      [{ text: '📋 Открыть заявку', callback_data: `adm_order_${order.id}` }],
                    ],
                  },
                }
              )
            )
          );
        }
      } catch (e) {
        console.error('[Bot] techspec submit:', e.message);
        await clearSession(chatId);
        return safeSend(chatId, '❌ *Не удалось отправить заявку\\.* Попробуйте позже или напишите менеджеру\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '💬 Менеджер', callback_data: 'contact_mgr' }]] },
        });
      }
      return;
    }

    // Tech spec generator: cancel
    if (data.startsWith('techspec_confirm_no_')) {
      await clearSession(chatId);
      return showMainMenu(chatId, q.from.first_name);
    }

    // Height search manual input
    if (data === 'search_height_input') return showHeightSearchInput(chatId);

    // srch_height / srch_age — text-input prompts (alias callbacks)
    if (data === 'srch_height') {
      await setSession(chatId, 'search_height', {});
      return safeSend(chatId, '📏 Введите диапазон роста, например: *165\\-175*\nИли просто одно число: *170*', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🔙 Назад', callback_data: 'cat_search' }]] },
      });
    }
    if (data === 'srch_age') {
      await setSession(chatId, 'search_age', {});
      return safeSend(chatId, '🎂 Введите диапазон возраста, например: *22\\-28*\nИли одно число: *25*', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🔙 Назад', callback_data: 'cat_search' }]] },
      });
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
        reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] },
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
          reply_markup: { inline_keyboard: [[{ text: '🤖 Сгенерировать', callback_data: `adm_ai_bio_${modelId}` }]] },
        });
      }
      await run('UPDATE models SET bio=? WHERE id=?', [bio, modelId]).catch(() => {});
      await clearSession(chatId);
      return safeSend(chatId, '✅ Описание сохранено\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '✏️ Редактировать', callback_data: `adm_editmodel_${modelId}` }],
            [{ text: '← Карточка', callback_data: `adm_model_${modelId}` }],
          ],
        },
      });
    }
    if (data.startsWith('adm_ai_bio_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('adm_ai_bio_', ''));
      return generateAiBio(chatId, modelId);
    }

    // ── Model confirm booking: mdl_confirm_{orderId}
    if (data.startsWith('mdl_confirm_')) {
      const orderId = parseInt(data.replace('mdl_confirm_', ''));
      if (!orderId) return;
      const order = await get(
        `SELECT o.*,m.name as model_name,m.telegram_chat_id as model_chat_id
         FROM orders o LEFT JOIN models m ON o.model_id=m.id
         WHERE o.id=?`,
        [orderId]
      ).catch(() => null);
      if (!order) return safeSend(chatId, '❌ Заявка не найдена\\.', { parse_mode: 'MarkdownV2' });
      // Verify the sender is indeed the assigned model
      if (String(order.model_chat_id) !== String(chatId)) {
        return safeSend(chatId, '❌ Эта заявка не назначена вам\\.', { parse_mode: 'MarkdownV2' });
      }
      const noteText = 'Модель подтвердила участие';
      get('SELECT * FROM order_notes WHERE order_id=? AND admin_note=? LIMIT 1', [orderId, noteText])
        .then(existing => {
          if (!existing) {
            run('INSERT INTO order_notes (order_id, admin_note) VALUES (?,?)', [orderId, noteText]).catch(() => {});
          }
        })
        .catch(() => {});
      await safeSend(
        chatId,
        `✅ *Вы подтвердили участие в заявке \\#${esc(order.order_number || String(orderId))}*\n\nМенеджер будет уведомлён\\.`,
        { parse_mode: 'MarkdownV2' }
      );
      // Notify all admins
      const adminIds = await getAdminChatIds().catch(() => [...ADMIN_IDS]);
      for (const adminId of adminIds) {
        safeSend(
          adminId,
          `✅ Модель *${esc(order.model_name || '—')}* подтвердила заявку *\\#${esc(order.order_number || String(orderId))}*`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '📋 Открыть заявку', callback_data: `adm_order_${orderId}` }]] },
          }
        ).catch(() => {});
      }
      return;
    }

    // ── Model reject booking: mdl_reject_{orderId}
    if (data.startsWith('mdl_reject_')) {
      const orderId = parseInt(data.replace('mdl_reject_', ''));
      if (!orderId) return;
      const order = await get(
        `SELECT o.*,m.name as model_name,m.telegram_chat_id as model_chat_id
         FROM orders o LEFT JOIN models m ON o.model_id=m.id
         WHERE o.id=?`,
        [orderId]
      ).catch(() => null);
      if (!order) return safeSend(chatId, '❌ Заявка не найдена\\.', { parse_mode: 'MarkdownV2' });
      // Verify the sender is indeed the assigned model
      if (String(order.model_chat_id) !== String(chatId)) {
        return safeSend(chatId, '❌ Эта заявка не назначена вам\\.', { parse_mode: 'MarkdownV2' });
      }
      const noteText = 'Модель отклонила участие';
      get('SELECT * FROM order_notes WHERE order_id=? AND admin_note=? LIMIT 1', [orderId, noteText])
        .then(existing => {
          if (!existing) {
            run('INSERT INTO order_notes (order_id, admin_note) VALUES (?,?)', [orderId, noteText]).catch(() => {});
          }
        })
        .catch(() => {});
      await safeSend(
        chatId,
        `❌ *Вы отклонили участие в заявке \\#${esc(order.order_number || String(orderId))}*\n\nМенеджер будет уведомлён\\.`,
        { parse_mode: 'MarkdownV2' }
      );
      // Notify all admins
      const adminIds = await getAdminChatIds().catch(() => [...ADMIN_IDS]);
      for (const adminId of adminIds) {
        safeSend(
          adminId,
          `❌ Модель *${esc(order.model_name || '—')}* отклонила заявку *\\#${esc(order.order_number || String(orderId))}*\\. Необходимо выбрать другую модель\\.`,
          {
            parse_mode: 'MarkdownV2',
            reply_markup: { inline_keyboard: [[{ text: '📋 Открыть заявку', callback_data: `adm_order_${orderId}` }]] },
          }
        ).catch(() => {});
      }
      return;
    }
  });

  // ── Additional message state handlers ─────────────────────────────────────
  bot.on('message', async msg => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId = msg.chat.id;
    const text = msg.text.trim();
    const session = await getSession(chatId);
    const state = session?.state || 'idle';
    const d = sessionData(session);

    // Tech spec: collect event description
    if (state === 'techspec_input') {
      if (!text || text.trim().length < 10) {
        return safeSend(chatId, '❌ Описание слишком короткое\\. Расскажите подробнее \\(минимум 10 символов\\):', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] },
        });
      }
      if (text.trim().length > 2000) {
        return safeSend(chatId, '❌ Описание слишком длинное \\(максимум 2000 символов\\)\\. Сократите текст:', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'main_menu' }]] },
        });
      }
      const description = text.trim();
      const { plainSpec, mdSpec } = generateTechSpec(description);

      // Save plain spec to session for DB storage later
      await setSession(chatId, 'techspec_confirm', { spec: plainSpec, description });
      return safeSend(chatId, `${mdSpec}\n\nОтправить это техническое задание менеджеру?`, {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [
              { text: '✅ Да, отправить', callback_data: `techspec_confirm_yes_${chatId}` },
              { text: '❌ Нет', callback_data: `techspec_confirm_no_${chatId}` },
            ],
          ],
        },
      });
    }

    // Quick booking: collect name
    if (state === 'bk_quick_name') {
      if (text.length < 2) return safeSend(chatId, STRINGS.bookingErrorName);
      if (text.length > 100) return safeSend(chatId, STRINGS.bookingErrorNameLong);
      d.quick_name = text.slice(0, 100);
      return bkQuickPhone(chatId, d);
    }

    // Quick booking: collect phone
    if (state === 'bk_quick_phone') {
      if (!/^[\d\s+\-()]{7,20}$/.test(text)) {
        return safeSend(chatId, STRINGS.bookingErrorPhone);
      }
      d.quick_phone = text;
      return bkQuickSubmit(chatId, d);
    }

    // Height search: manual range input
    if (state === 'search_height') {
      const clean = text.replace(/\s/g, '');
      const rangeMatch = clean.match(/^(\d{3})-(\d{3})$/);
      const singleMatch = clean.match(/^(\d{3})$/);
      if (rangeMatch) {
        await clearSession(chatId);
        const f = getSearchFilters(chatId);
        f.height_min = parseInt(rangeMatch[1]);
        f.height_max = parseInt(rangeMatch[2]);
        return showSearchResults(chatId, f, 0);
      } else if (singleMatch) {
        await clearSession(chatId);
        const h = parseInt(singleMatch[1]);
        const f = getSearchFilters(chatId);
        f.height_min = h;
        f.height_max = h;
        return showSearchResults(chatId, f, 0);
      } else {
        return safeSend(
          chatId,
          '❌ Неверный формат\\. Введите диапазон, например: *170\\-180* или одно значение *175*',
          { parse_mode: 'MarkdownV2' }
        );
      }
    }

    // ── Model registration: phone input
    if (state === 'model_reg_phone') {
      const phone = text.replace(/\s/g, '');
      if (!/^[\d+\-()]{7,20}$/.test(phone)) {
        return safeSend(chatId, '❌ Некорректный номер телефона\\. Введите номер в формате \\+7XXXXXXXXXX:', {
          parse_mode: 'MarkdownV2',
        });
      }
      // Normalise: try the raw value and also strip leading + or 8
      const phoneVariants = [phone];
      const stripped = phone.replace(/^[+8]/, '');
      if (stripped !== phone) phoneVariants.push(stripped, '+7' + stripped, '8' + stripped);
      // Build a LIKE-search covering all variants
      const whereClauses = phoneVariants.map(() => 'phone LIKE ?').join(' OR ');
      const likeParams = phoneVariants.map(p => `%${p}%`);
      const model = await get(
        `SELECT id, name, telegram_chat_id FROM models WHERE (${whereClauses}) LIMIT 1`,
        likeParams
      ).catch(() => null);
      if (!model) {
        await clearSession(chatId);
        return safeSend(
          chatId,
          '❌ *Телефон не найден в базе моделей\\.*\n\nОбратитесь к администратору агентства\\.',
          { parse_mode: 'MarkdownV2' }
        );
      }
      // Guard against one user silently overwriting another user's linked account.
      // If the model already has a DIFFERENT telegram_chat_id, block re-registration
      // and notify admins so they can resolve the conflict manually.
      if (model.telegram_chat_id && String(model.telegram_chat_id) !== String(chatId)) {
        await clearSession(chatId);
        const adminIds = await getAdminChatIds().catch(() => [...ADMIN_IDS]);
        for (const adminId of adminIds) {
          safeSend(
            adminId,
            `⚠️ Попытка повторной регистрации модели *${esc(model.name)}*\\.\n` +
              `Новый chatId: \`${chatId}\`, уже привязан: \`${model.telegram_chat_id}\`\\.\n` +
              `Если это законный запрос — сбросьте вручную через панель администратора\\.`,
            { parse_mode: 'MarkdownV2' }
          ).catch(() => {});
        }
        return safeSend(
          chatId,
          '⚠️ *Этот номер уже привязан к другому аккаунту Telegram\\.*\n\nОбратитесь к администратору агентства для смены привязки\\.',
          { parse_mode: 'MarkdownV2' }
        );
      }
      await run('UPDATE models SET telegram_chat_id=? WHERE id=?', [String(chatId), model.id]).catch(() => {});
      await clearSession(chatId);
      return safeSend(
        chatId,
        `✅ *Вы зарегистрированы как модель ${esc(model.name)}\\!*\n\nТеперь вы будете получать уведомления о заявках и сможете подтверждать или отклонять участие\\.`,
        { parse_mode: 'MarkdownV2' }
      );
    }

    // Age search: manual range input (via srch_age callback)
    if (state === 'search_age') {
      const clean = text.replace(/\s/g, '');
      const rangeMatch = clean.match(/^(\d{1,2})-(\d{1,2})$/);
      const singleMatch = clean.match(/^(\d{1,2})$/);
      if (rangeMatch) {
        await clearSession(chatId);
        const f = getSearchFilters(chatId);
        f.age_min = parseInt(rangeMatch[1]);
        f.age_max = parseInt(rangeMatch[2]);
        return showSearchResults(chatId, f, 0);
      } else if (singleMatch) {
        await clearSession(chatId);
        const a = parseInt(singleMatch[1]);
        const f = getSearchFilters(chatId);
        f.age_min = a;
        f.age_max = a + 5;
        return showSearchResults(chatId, f, 0);
      } else {
        return safeSend(chatId, '❌ Неверный формат\\. Введите диапазон, например: *22\\-28* или одно значение *25*', {
          parse_mode: 'MarkdownV2',
        });
      }
    }

    // City search: free-text city input
    if (state === 'search_city_input') {
      const city = text.trim();
      if (!city || city.length < 2) {
        return safeSend(chatId, '❌ Введите название города \\(минимум 2 символа\\)', { parse_mode: 'MarkdownV2' });
      }
      await clearSession(chatId);
      const f = getSearchFilters(chatId);
      f.city = city;
      return showSearchMenu(chatId);
    }
  });
}

module.exports = {
  initBot,
  notifyAdmin,
  notifyNewOrder,
  notifyStatusChange,
  sendMessageToClient,
  notifyPaymentSuccess,
  _registerNewFeatures,
};
