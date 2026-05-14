require('dotenv').config();
const crypto = require('crypto');
const TelegramBot = require('node-telegram-bot-api');
const { query, run, get, generateOrderNumber } = require('./database');

const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
const SITE_URL  = process.env.SITE_URL || 'http://localhost:3000';
const WEBHOOK_URL    = process.env.WEBHOOK_URL || '';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || crypto.randomBytes(32).toString('hex');

// ─── Dictionaries (must match website exactly) ───────────────────────────────

const STATUS_LABELS = {
  new:        '🆕 Новая',
  reviewing:  '🔍 На рассмотрении',
  confirmed:  '✅ Подтверждена',
  in_progress:'▶️ В процессе',
  completed:  '🏁 Завершена',
  cancelled:  '❌ Отменена',
};

const EVENT_TYPES = {
  fashion_show: 'Показ мод',
  photo_shoot:  'Фотосессия',
  event:        'Корпоратив / Мероприятие',
  commercial:   'Коммерческая съёмка',
  runway:       'Подиум',
  other:        'Другое',
};

const CATEGORIES = {
  '':         'Все',
  fashion:    'Fashion',
  commercial: 'Commercial',
  events:     'Events',
};

const DURATIONS = ['1', '2', '3', '4', '6', '8', '12'];

let bot = null;

// ─── Helpers ──────────────────────────────────────────────────────────────────

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
  try { return await bot.sendPhoto(chatId, photo, opts); }
  catch { return safeSend(chatId, opts.caption || '📷', { parse_mode: opts.parse_mode }); }
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

// ─── Keyboards ────────────────────────────────────────────────────────────────

// Persistent ReplyKeyboard — всегда показывается внизу чата вместо клавиатуры
const REPLY_KB_CLIENT = {
  keyboard: [
    [{ text: '💃 Каталог' }, { text: '📝 Подать заявку' }],
    [{ text: '📋 Мои заявки' }, { text: '🔍 Статус заявки' }],
    [{ text: '❓ FAQ' }, { text: '👤 Профиль' }, { text: '📞 Контакты' }],
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

function buildClientKeyboard() {
  const rows = [
    [{ text: '💃 Каталог моделей',      callback_data: 'cat_cat__0'   }],
    [{ text: '📝 Оформить заявку',      callback_data: 'bk_start'     }],
    [{ text: '📋 Мои заявки',           callback_data: 'my_orders'    }],
    [{ text: '🔍 Проверить статус',     callback_data: 'check_status' }],
    [{ text: '📞 Контакты',             callback_data: 'contacts'     }],
    [{ text: '❓ FAQ',                  callback_data: 'faq'          },
     { text: '👤 Мой профиль',         callback_data: 'profile'      }],
  ];
  if (SITE_URL.startsWith('https://')) {
    // Полноценный Mini App только на HTTPS
    const webappUrl = SITE_URL.replace(/\/$/, '') + '/webapp.html';
    rows.unshift([{ text: '📱 Открыть Mini App', web_app: { url: webappUrl } }]);
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
       { text: `🤖 Организм${health}`,       callback_data: 'adm_organism'   }],
      [{ text: '⚙️ Настройки',              callback_data: 'adm_settings'   },
       { text: '📢 Рассылка',               callback_data: 'adm_broadcast'  }],
      [{ text: '➕ Добавить модель',         callback_data: 'adm_addmodel'   },
       { text: '📤 Экспорт заявок',         callback_data: 'adm_export'     }],
      [{ text: '👑 Администраторы',          callback_data: 'adm_admins'     },
       { text: '📡 Фид агентов',            callback_data: 'agent_feed_0'   }],
      [{ text: '⭐ Отзывы',                 callback_data: 'adm_reviews'    },
       { text: '💬 Обсуждения',            callback_data: 'adm_discussions'}],
      ...(SITE_URL.startsWith('https://') ? [[
        { text: '📱 Mini App', web_app: { url: SITE_URL.replace(/\/$/, '') + '/webapp.html' } },
        { text: '🌐 Сайт', url: SITE_URL },
      ]] : []),
    ]
  };
};

// ─── Client screens ───────────────────────────────────────────────────────────

async function showMainMenu(chatId, name) {
  await clearSession(chatId);
  const greeting = await getSetting('greeting').catch(() => null);
  // Сначала показываем persistent ReplyKeyboard
  await safeSend(chatId,
    `💎 Nevesty Models — меню активировано`,
    { reply_markup: REPLY_KB_CLIENT }
  );
  if (greeting) {
    const text = greeting.replace('{name}', name || 'гость');
    return safeSend(chatId, text, { reply_markup: buildClientKeyboard() });
  }
  return safeSend(chatId,
    `💎 *Nevesty Models*\n\nДобро пожаловать${name ? ', ' + esc(name) : ''}\\!\n\n_Агентство профессиональных моделей — Fashion, Commercial, Events_\n\nВыберите действие:`,
    { parse_mode: 'MarkdownV2', reply_markup: buildClientKeyboard() }
  );
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

async function showCatalog(chatId, cat, page) {
  try {
    const models = cat
      ? await query('SELECT * FROM models WHERE available=1 AND category=? ORDER BY id', [cat])
      : await query('SELECT * FROM models WHERE available=1 ORDER BY id');

    if (!models.length) {
      return safeSend(chatId, '📭 Моделей по выбранному фильтру нет\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'main_menu' }]] }
      });
    }

    const perPage = 5;
    const total   = models.length;
    const slice   = models.slice(page * perPage, page * perPage + perPage);

    // Category filter row
    const catRow = Object.entries(CATEGORIES).map(([k, v]) => ({
      text: (k === cat ? '✅ ' : '') + v,
      callback_data: `cat_cat_${k}_0`
    }));

    // Model buttons
    const modelBtns = slice.map(m => [{
      text: `${m.available ? '🟢' : '🔴'} ${m.name}  ·  ${m.height}см  ·  ${m.hair_color || ''}`,
      callback_data: `cat_model_${m.id}`
    }]);

    // Pagination
    const nav = [];
    if (page > 0)                         nav.push({ text: '◀️',  callback_data: `cat_cat_${cat}_${page-1}` });
    if ((page+1)*perPage < total)         nav.push({ text: '▶️',  callback_data: `cat_cat_${cat}_${page+1}` });

    const keyboard = [
      catRow,
      ...modelBtns,
      ...(nav.length ? [nav] : []),
      [{ text: '📝 Оформить заявку', callback_data: 'bk_start' }],
      [{ text: '🏠 Главное меню',    callback_data: 'main_menu' }],
    ];

    const label = CATEGORIES[cat] || 'Все';
    return safeSend(chatId,
      `💃 *Каталог моделей — ${esc(label)}*\n\nНайдено: ${total} ${ru_plural(total,'модель','модели','моделей')}\n\nВыберите модель для просмотра:`,
      { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } }
    );
  } catch (e) { console.error('[Bot] showCatalog:', e.message); }
}

async function showModel(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена\\.', { parse_mode: 'MarkdownV2' });

    const lines = [];
    if (m.age)                             lines.push(`Возраст: *${m.age}* лет`);
    if (m.height)                          lines.push(`Рост: *${m.height}* см`);
    if (m.weight)                          lines.push(`Вес: *${m.weight}* кг`);
    if (m.bust && m.waist && m.hips)       lines.push(`Параметры: *${m.bust}/${m.waist}/${m.hips}*`);
    if (m.shoe_size)                       lines.push(`Обувь: *${esc(m.shoe_size)}*`);
    if (m.hair_color)                      lines.push(`Волосы: *${esc(m.hair_color)}*`);
    if (m.eye_color)                       lines.push(`Глаза: *${esc(m.eye_color)}*`);
    if (m.category)                        lines.push(`Категория: *${esc(m.category)}*`);
    if (m.instagram)                       lines.push(`Instagram: @${esc(m.instagram)}`);

    const bio   = m.bio   ? `\n\n_${esc(m.bio)}_` : '';
    const avail = m.available ? '🟢 Доступна для заказа' : '🔴 Временно недоступна';
    const caption = `💃 *${esc(m.name)}*\n${lines.join(' \\| ')}${bio}\n\n${avail}`;

    const keyboard = {
      inline_keyboard: [
        m.available ? [{ text: '📝 Заказать эту модель', callback_data: `bk_model_${m.id}` }] : [],
        [{ text: '← Каталог', callback_data: 'cat_cat__0' }, { text: '🏠 Меню', callback_data: 'main_menu' }],
      ].filter(r => r.length)
    };

    // Собираем все фото: photo_main + галерея из поля photos
    let galleryUrls = [];
    try { galleryUrls = JSON.parse(m.photos || '[]'); } catch {}
    if (m.photo_main && !galleryUrls.includes(m.photo_main)) {
      galleryUrls.unshift(m.photo_main);
    }

    if (galleryUrls.length >= 2) {
      // Отправляем медиагруппу (до 10 фото), последнее фото несёт caption
      const media = galleryUrls.slice(0, 8).map((url, i) => {
        const item = { type: 'photo', media: url };
        if (i === galleryUrls.slice(0, 8).length - 1) {
          item.caption        = caption;
          item.parse_mode     = 'MarkdownV2';
        }
        return item;
      });
      try {
        await bot.sendMediaGroup(chatId, media);
      } catch (e) {
        console.warn('[Bot] sendMediaGroup failed, fallback to single photo:', e.message);
        await safePhoto(chatId, galleryUrls[0], { caption, parse_mode: 'MarkdownV2' });
      }
      // Кнопки шлём отдельным сообщением (медиагруппы не поддерживают reply_markup)
      return safeSend(chatId, `📸 Фотогалерея: ${galleryUrls.length} фото`, { reply_markup: keyboard });
    }

    if (m.photo_main) {
      return safePhoto(chatId, m.photo_main, { caption, parse_mode: 'MarkdownV2', reply_markup: keyboard });
    }
    return safeSend(chatId, caption, { parse_mode: 'MarkdownV2', reply_markup: keyboard });
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

async function showMyOrders(chatId) {
  try {
    const orders = await query(
      `SELECT o.*,m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE o.client_chat_id=? ORDER BY o.created_at DESC LIMIT 10`,
      [String(chatId)]
    );
    if (!orders.length) {
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
    let text = '📋 *Ваши заявки:*\n\n';
    const btns = orders.map(o => {
      text += `${STATUS_LABELS[o.status]||o.status} *${esc(o.order_number)}*\n`;
      text += `${esc(EVENT_TYPES[o.event_type]||o.event_type)}`;
      if (o.event_date) text += ` · ${esc(o.event_date)}`;
      text += '\n\n';
      return [{ text: `${o.order_number}  ${STATUS_LABELS[o.status]||o.status}`, callback_data: `client_order_${o.id}` }];
    });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [...btns, [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
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
      return safeSend(chatId, '❌ Заявка не найдена\\.', { parse_mode: 'MarkdownV2' });
    }
    const msgs = await query(
      'SELECT * FROM messages WHERE order_id=? ORDER BY created_at DESC LIMIT 3',
      [orderId]
    );
    let text = `📋 *Заявка ${esc(o.order_number)}*\n\n`;
    text += `Статус: ${STATUS_LABELS[o.status]||o.status}\n`;
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
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '← Мои заявки', callback_data: 'my_orders'  }],
        [{ text: '🏠 Меню',      callback_data: 'main_menu'  }],
      ]}
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
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') + 'Введите место проведения \\(город, адрес\\):',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 2e — budget (optional)
async function bkStep2Budget(chatId, data) {
  await setSession(chatId, 'bk_s2_budget', data);
  return safeSend(chatId,
    stepHeader(2,'Детали мероприятия') + 'Укажите бюджет \\(необязательно\\):',
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
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + 'Введите ваше имя и фамилию:',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 3b — phone
async function bkStep3Phone(chatId, data) {
  await setSession(chatId, 'bk_s3_phone', data);
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + 'Введите номер телефона:',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'bk_cancel' }]] }
    }
  );
}

// STEP 3c — email (optional)
async function bkStep3Email(chatId, data) {
  await setSession(chatId, 'bk_s3_email', data);
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + 'Введите email \\(необязательно\\):',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '⏭ Пропустить', callback_data: 'bk_skip_email' }],
        [{ text: '❌ Отменить',   callback_data: 'bk_cancel'     }],
      ]}
    }
  );
}

// STEP 3d — telegram username (optional)
async function bkStep3Telegram(chatId, data, tgUsername) {
  await setSession(chatId, 'bk_s3_tg', data);
  const hint = tgUsername
    ? `_Ваш username в Telegram: @${esc(tgUsername)}_\n\n`
    : '';
  return safeSend(chatId,
    stepHeader(3,'Ваши контакты') + hint + 'Введите Telegram username для связи \\(необязательно\\):\n_Пример: @username_',
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        tgUsername ? [{ text: `✅ Использовать @${tgUsername}`, callback_data: `bk_use_tg_${tgUsername}` }] : [],
        [{ text: '⏭ Пропустить', callback_data: 'bk_skip_tg' }],
        [{ text: '❌ Отменить',   callback_data: 'bk_cancel'  }],
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
    return safeSend(chatId, '❌ Ошибка при отправке заявки\\. Попробуйте позже или свяжитесь с нами\\.', { parse_mode: 'MarkdownV2' });
  }
}

// ─── Admin screens ────────────────────────────────────────────────────────────

const VALID_STATUSES = ['new','reviewing','confirmed','in_progress','completed','cancelled'];

async function showAdminOrders(chatId, statusFilter, page = 0) {
  try {
    const safe = VALID_STATUSES.includes(statusFilter) ? statusFilter : null;
    page = parseInt(page) || 0;
    const [total, orders] = await Promise.all([
      safe
        ? get('SELECT COUNT(*) as n FROM orders WHERE status=?', [safe])
        : get('SELECT COUNT(*) as n FROM orders'),
      safe
        ? query('SELECT o.*,m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id=m.id WHERE o.status=? ORDER BY o.created_at DESC LIMIT 8 OFFSET ?', [safe, page*8])
        : query('SELECT o.*,m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id=m.id ORDER BY o.created_at DESC LIMIT 8 OFFSET ?', [page*8])
    ]);

    if (!orders.length) {
      return safeSend(chatId, '📭 Заявок нет.', {
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] }
      });
    }

    const filterKey = safe || '';
    const filterLabel = safe ? (STATUS_LABELS[safe]||safe) : 'Все';
    let text = `📋 *Заявки — ${filterLabel}* \\(${total.n}\\)\n\n`;

    const btns = orders.map(o => {
      const icon = STATUS_LABELS[o.status]?.split(' ')[0]||'';
      text += `${icon} *${o.order_number}* — ${esc(o.client_name)}\n`;
      return [{ text: `${o.order_number}  ·  ${o.client_name}`, callback_data: `adm_order_${o.id}` }];
    });

    const nav = [];
    if (page > 0)           nav.push({ text: '◀️', callback_data: `adm_orders_${filterKey}_${page-1}` });
    if ((page+1)*8 < total.n) nav.push({ text: '▶️', callback_data: `adm_orders_${filterKey}_${page+1}` });

    const filterRow = [
      { text: 'Все',   callback_data: 'adm_orders__0' },
      { text: '🆕 Нов', callback_data: 'adm_orders_new_0' },
      { text: '✅ Подт', callback_data: 'adm_orders_confirmed_0' },
      { text: '🏁 Гот', callback_data: 'adm_orders_completed_0' },
    ];

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...btns,
        ...(nav.length ? [nav] : []),
        filterRow,
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminOrders:', e.message); }
}

async function showAdminOrder(chatId, orderId) {
  try {
    const o = await get(
      'SELECT o.*,m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id=m.id WHERE o.id=?',
      [orderId]
    );
    if (!o) return safeSend(chatId, '❌ Заявка не найдена.');

    const msgs = await query('SELECT * FROM messages WHERE order_id=? ORDER BY created_at DESC LIMIT 3', [orderId]);

    const e = s => String(s||'').replace(/[_*`\[\]]/g, '\\$&');
    let text = `📋 *${o.order_number}*\nСтатус: ${STATUS_LABELS[o.status]||o.status}\n\n`;
    text += `👤 ${e(o.client_name)}\n📞 ${e(o.client_phone)}\n`;
    if (o.client_email)    text += `📧 ${e(o.client_email)}\n`;
    if (o.client_telegram) text += `💬 @${e(o.client_telegram.replace('@',''))}\n`;
    text += `\n🎭 ${e(EVENT_TYPES[o.event_type]||o.event_type)}\n`;
    if (o.event_date)      text += `📅 ${e(o.event_date)}\n`;
    if (o.event_duration)  text += `⏱ ${e(o.event_duration)} ч\\.\n`;
    if (o.location)        text += `📍 ${e(o.location)}\n`;
    if (o.model_name)      text += `💃 ${e(o.model_name)}\n`;
    if (o.budget)          text += `💰 ${e(o.budget)}\n`;
    if (o.comments)        text += `💬 ${e(o.comments)}\n`;
    if (msgs.length) {
      text += `\n📨 Последние сообщения:\n`;
      msgs.reverse().forEach(m => {
        const who = m.sender_type==='admin' ? '👤' : '🙋';
        text += `${who} ${e(m.content)}\n`;
      });
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
    keyboard.push([{ text: '← К заявкам', callback_data: 'adm_orders__0' }]);

    return safeSend(chatId, text, { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } });
  } catch (e) { console.error('[Bot] showAdminOrder:', e.message); }
}

async function showAdminStats(chatId) {
  try {
    const [total,newO,rev,conf,ip,done,canc,models] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE status='new'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='reviewing'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='confirmed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='in_progress'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='completed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='cancelled'"),
      get('SELECT COUNT(*) as n FROM models WHERE available=1'),
    ]);

    // Revenue estimate: confirmed + completed orders × avg price 15000₽
    const paidOrders = (conf.n || 0) + (done.n || 0);
    const AVG_PRICE = 15000;
    const revenueEst = paidOrders * AVG_PRICE;

    // Top 3 most popular models
    let topModels = [];
    try {
      topModels = await query(
        `SELECT m.name, COUNT(o.id) as cnt
         FROM models m
         JOIN orders o ON o.model_id = m.id
         GROUP BY m.id, m.name
         ORDER BY cnt DESC
         LIMIT 3`
      );
    } catch {}

    // Peak booking days of week
    let peakDays = [];
    try {
      peakDays = await query(
        `SELECT strftime('%w', created_at) as dow, COUNT(*) as cnt
         FROM orders
         GROUP BY dow
         ORDER BY cnt DESC
         LIMIT 3`
      );
    } catch {}
    const DAY_NAMES = ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'];

    let text = `📊 Статистика Nevesty Models\n\n`;
    text += `Всего заявок: ${total.n}\n`;
    text += `Новых: ${newO.n}\n`;
    text += `На рассмотрении: ${rev.n}\n`;
    text += `Подтверждено: ${conf.n}\n`;
    text += `В работе: ${ip.n}\n`;
    text += `Завершено: ${done.n}\n`;
    text += `Отклонено: ${canc.n}\n\n`;
    text += `Доступно моделей: ${models.n}\n\n`;
    text += `Оценка выручки (подтв. + завершённые × 15 000 руб.): ~${revenueEst.toLocaleString('ru')} руб.\n`;

    if (topModels.length) {
      text += `\nТоп моделей по заявкам:\n`;
      topModels.forEach((m, i) => {
        text += `  ${i + 1}. ${m.name} — ${m.cnt} заявок\n`;
      });
    }

    if (peakDays.length) {
      text += `\nПиковые дни бронирований:\n`;
      peakDays.forEach(d => {
        const dayName = DAY_NAMES[parseInt(d.dow)] || d.dow;
        text += `  ${dayName} — ${d.cnt} заявок\n`;
      });
    }

    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '📋 Все заявки', callback_data: 'adm_orders__0' }],
        [{ text: '← Меню',        callback_data: 'admin_menu'    }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminStats:', e.message); }
}

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

async function showAdminModels(chatId, page) {
  try {
    const all = await query('SELECT * FROM models ORDER BY id DESC LIMIT 500');
    const perPage = 8;
    const slice = all.slice(page*perPage, page*perPage+perPage);
    let text = `💃 *Модели агентства* \\(всего: ${all.length}\\)\n\n`;
    const btns = slice.map(m => {
      text += `${m.available ? '🟢' : '🔴'} *${esc(m.name)}* — ${m.height}см, ${esc(m.category)}\n`;
      return [{ text: `${m.available?'🟢':'🔴'} ${m.name}`, callback_data: `adm_model_${m.id}` }];
    });
    const nav = [];
    if (page > 0)                         nav.push({ text:'◀️', callback_data:`adm_models_${page-1}` });
    if ((page+1)*perPage < all.length)    nav.push({ text:'▶️', callback_data:`adm_models_${page+1}` });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...btns,
        ...(nav.length ? [nav] : []),
        [{ text:'← Меню', callback_data:'admin_menu' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminModels:', e.message); }
}

async function showAdminModel(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id=?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена.');
    const cnt = (await get('SELECT COUNT(*) as n FROM orders WHERE model_id=?', [modelId])).n;

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
    text += `📋 Заявок: ${cnt}\n`;
    text += `Статус: ${m.available ? '🟢 Доступна' : '🔴 Недоступна'}\n`;
    if (m.bio) text += `\n_${esc(m.bio)}_`;

    const keyboard = { inline_keyboard: [
      [{ text: '✏️ Редактировать', callback_data: `adm_editmodel_${m.id}` },
       { text: m.available ? '🔴 Недоступна' : '🟢 Доступна', callback_data: `adm_toggle_${m.id}` }],
      [{ text: '← К моделям', callback_data: 'adm_models_0' }],
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

async function showAgentDiscussions(chatId) {
  try {
    const rows = await query('SELECT * FROM agent_discussions ORDER BY created_at DESC LIMIT 10');
    if (!rows.length) return safeSend(chatId, '💬 Обсуждений агентов пока нет.', {
      reply_markup: { inline_keyboard: [[{ text:'← Меню', callback_data:'admin_menu' }]] }
    });
    let text = `💬 *Обсуждения агентов*\n\n`;
    const now = Date.now();
    rows.reverse().forEach(d => {
      const ageTo = d.to_agent ? esc(d.to_agent) : 'all';
      const mins = Math.round((now - new Date(d.created_at).getTime()) / 60000);
      const timeStr = mins < 60 ? `${mins} мин назад` : `${Math.round(mins/60)} ч назад`;
      const snippet = esc((d.message || '').slice(0, 150));
      text += `🤖 *${esc(d.from_agent || '?')}* → ${esc(ageTo)} \\(${esc(timeStr)}\\):\n"${snippet}${(d.message||'').length > 150 ? '…' : ''}"\n\n`;
    });
    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text:'🔄 Обновить', callback_data:'adm_discussions' }],
        [{ text:'← Меню',     callback_data:'admin_menu'       }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAgentDiscussions:', e.message); }
}

// ─── Settings menu ────────────────────────────────────────────────────────────

async function showAdminSettings(chatId) {
  if (!isAdmin(chatId)) return;
  const [greeting, phone, email, insta, notifNew, notifSt] = await Promise.all([
    getSetting('greeting'), getSetting('contacts_phone'), getSetting('contacts_email'),
    getSetting('contacts_insta'), getSetting('notif_new_order'), getSetting('notif_status'),
  ]);
  const text = `⚙️ Настройки бота и агентства\n\n` +
    `📝 Приветствие: ${(greeting||'').slice(0,50)}${(greeting||'').length>50?'...':''}\n` +
    `📞 Телефон: ${phone||'—'}\n` +
    `📧 Email: ${email||'—'}\n` +
    `📸 Instagram: ${insta||'—'}\n` +
    `🔔 Уведомления о заявках: ${notifNew==='1'?'✅ Вкл':'❌ Выкл'}\n` +
    `🔔 Уведомления о статусах: ${notifSt==='1'?'✅ Вкл':'❌ Выкл'}`;
  return safeSend(chatId, text, {
    reply_markup: { inline_keyboard: [
      [{ text: '📝 Приветствие', callback_data: 'adm_set_greeting' },
       { text: 'ℹ️ О нас',      callback_data: 'adm_set_about'    }],
      [{ text: '📞 Телефон',    callback_data: 'adm_set_phone'   },
       { text: '📧 Email',      callback_data: 'adm_set_email'   }],
      [{ text: '📸 Instagram',  callback_data: 'adm_set_insta'   },
       { text: '📍 Адрес',      callback_data: 'adm_set_addr'    }],
      [{ text: '💰 Прайс-лист', callback_data: 'adm_set_pricing' }],
      [{ text: notifNew==='1' ? '🔕 Выкл уведом. заявки' : '🔔 Вкл уведом. заявки',
         callback_data: notifNew==='1' ? 'adm_notif_new_off' : 'adm_notif_new_on' },
       { text: notifSt==='1'  ? '🔕 Выкл уведом. статус' : '🔔 Вкл уведом. статус',
         callback_data: notifSt==='1'  ? 'adm_notif_st_off'  : 'adm_notif_st_on'  }],
      [{ text: '← Меню', callback_data: 'admin_menu' }],
    ]}
  });
}

// ─── Add Model wizard ─────────────────────────────────────────────────────────

const MODEL_HAIR_COLORS = ['Блонд','Тёмный блонд','Шатен','Брюнетка','Рыжая','Другой'];
const MODEL_EYE_COLORS  = ['Голубые','Серые','Зелёные','Карие','Чёрные'];
const MODEL_CATEGORIES  = { fashion:'Fashion', commercial:'Commercial', events:'Events' };

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
      [{ text: '📝 Описание',   callback_data: `adm_ef_${modelId}_bio`        }],
      [{ text: '📷 Галерея фото', callback_data: `adm_gallery_${modelId}`      }],
      [{ text: m.available ? '🔴 Недоступна' : '🟢 Доступна', callback_data: `adm_toggle_${modelId}` }],
      [{ text: '🗑 Удалить модель', callback_data: `adm_del_model_${modelId}` }],
      [{ text: '← Карточка',   callback_data: `adm_model_${modelId}`          }],
    ]}
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
    `📷 Галерея: *${m.name}*\nФото: *${count}/8* загружено\n\nОтправляйте фото одно за другим (до 8 штук).\nПервое фото станет главным.`,
    {
      parse_mode: 'Markdown',
      reply_markup: { inline_keyboard: [
        [{ text: '🗑 Очистить все фото', callback_data: `adm_gallery_clear_${modelId}` }],
        [{ text: '✅ Готово',           callback_data: `adm_model_${modelId}`           }],
        [{ text: '← Редактировать',    callback_data: `adm_editmodel_${modelId}`       }],
      ]}
    }
  );
}

// ─── Broadcast ────────────────────────────────────────────────────────────────

async function showBroadcast(chatId) {
  if (!isAdmin(chatId)) return;
  const r = await get("SELECT COUNT(DISTINCT client_chat_id) as n FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != ''").catch(()=>({n:0}));
  return safeSend(chatId,
    `📢 *Рассылка клиентам*\n\nКлиентов с заявками: *${r.n}*\n\nВведите сообщение для рассылки — оно будет отправлено всем клиентам, которые оформляли заявки через бота.\n\n⚠️ _Используйте аккуратно_`,
    {
      reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'admin_menu' }]] } }
  );
}

async function sendBroadcast(chatId, text) {
  const clients = await query("SELECT DISTINCT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != ''").catch(()=>[]);
  if (!clients.length) return safeSend(chatId, '❌ Нет клиентов для рассылки.');
  let sent = 0, failed = 0;
  for (const c of clients) {
    try {
      await bot.sendMessage(c.client_chat_id, `📢 *Сообщение от Nevesty Models*\n\n${text}`, {});
      sent++;
    } catch { failed++; }
    await new Promise(r => setTimeout(r, 50)); // rate limit
  }
  await clearSession(chatId);
  return safeSend(chatId, `✅ *Рассылка завершена*\n\nОтправлено: ${sent}\nОшибок: ${failed}`, {
    reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
  });
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

// ─── Export orders ────────────────────────────────────────────────────────────

async function exportOrders(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const orders = await query(
      `SELECT o.order_number,o.client_name,o.client_phone,o.client_email,o.client_telegram,
              o.event_type,o.event_date,o.event_duration,o.location,o.budget,o.comments,
              o.status,o.created_at,m.name as model_name
       FROM orders o LEFT JOIN models m ON o.model_id=m.id
       ORDER BY o.created_at DESC`
    );
    const header = ['Номер','Клиент','Телефон','Email','Telegram','Тип события','Дата','Длит(ч)','Место','Бюджет','Комментарий','Статус','Создан','Модель'];
    const rows = orders.map(o => [
      o.order_number, o.client_name, o.client_phone, o.client_email||'', o.client_telegram||'',
      o.event_type, o.event_date||'', o.event_duration||'', o.location||'', o.budget||'',
      (o.comments||'').replace(/"/g,'""'), o.status,
      new Date(o.created_at).toLocaleString('ru'), o.model_name||''
    ].map(v => `"${v}"`).join(','));
    const csv = [header.join(','), ...rows].join('\n');
    const buf = Buffer.from('﻿' + csv, 'utf8'); // BOM для Excel
    await bot.sendDocument(chatId, buf, {
      caption: `📤 Экспорт заявок — ${orders.length} записей\n${new Date().toLocaleString('ru')}`,
    }, { filename: `orders_${Date.now()}.csv`, contentType: 'text/csv' });
  } catch (e) { return safeSend(chatId, `❌ Ошибка экспорта: ${e.message}`); }
}

// ─── Admin order actions ──────────────────────────────────────────────────────

async function adminChangeStatus(chatId, orderId, newStatus) {
  try {
    const immutable = ['completed','cancelled'];
    if (newStatus === 'confirmed')   {
      const r = await run("UPDATE orders SET status='confirmed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",[orderId]);
      if (r.changes === 0) return safeSend(chatId,'⚠️ Заявка уже обработана.');
    } else if (newStatus === 'reviewing') {
      const r = await run("UPDATE orders SET status='reviewing',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",[orderId]);
      if (r.changes === 0) return safeSend(chatId,'⚠️ Заявка уже обработана.');
    } else if (newStatus === 'cancelled') {
      const r = await run("UPDATE orders SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('completed','cancelled')",[orderId]);
      if (r.changes === 0) return safeSend(chatId,'⚠️ Заявка уже обработана.');
    } else if (newStatus === 'completed') {
      const r = await run("UPDATE orders SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status!='cancelled'",[orderId]);
      if (r.changes === 0) return safeSend(chatId,'⚠️ Заявка уже обработана.');
    }
    const order = await get('SELECT * FROM orders WHERE id=?', [orderId]);
    if (order?.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, newStatus);
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
    { command: 'start',   description: '🏠 Главное меню' },
    { command: 'catalog', description: '💃 Каталог моделей' },
    { command: 'booking', description: '📝 Оформить заявку' },
    { command: 'orders',  description: '📋 Мои заявки' },
    { command: 'status',  description: '🔍 Статус заявки по номеру' },
    { command: 'faq',     description: '❓ Часто задаваемые вопросы' },
    { command: 'profile', description: '👤 Мой профиль' },
    { command: 'contacts',description: '📞 Контакты агентства' },
    { command: 'help',    description: '📖 Справка' },
    { command: 'cancel',  description: '❌ Отменить действие' },
  ]).catch(e => console.warn('[Bot] setMyCommands:', e.message));

  // ── /start ─────────────────────────────────────────────────────────────────
  bot.onText(/\/start(.*)/, async (msg, match) => {
    const chatId    = msg.chat.id;
    const firstName = msg.from.first_name;
    await setSession(chatId, 'idle', {});

    // Deep-link: /start ORDER_NUMBER
    const ref = match[1]?.trim();
    if (ref) {
      const order = await get('SELECT * FROM orders WHERE order_number=?', [ref]).catch(()=>null);
      if (order) {
        if (order.client_chat_id && order.client_chat_id !== String(chatId))
          return safeSend(chatId, '❌ Эта заявка уже привязана к другому чату.');
        await run('UPDATE orders SET client_chat_id=? WHERE order_number=?', [String(chatId), ref]);
        return safeSend(chatId,
          `✅ Заявка *${ref}* привязана к вашему чату\\!\n\nВы будете получать уведомления о статусе\\.`,
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
    return showMainMenu(chatId, firstName);
  });

  // ── /cancel ────────────────────────────────────────────────────────────────
  bot.onText(/\/cancel/, async (msg) => {
    const chatId = msg.chat.id;
    const s = await getSession(chatId);
    if (!s || s.state === 'idle') return safeSend(chatId, 'ℹ️ Нет активного действия.');
    await clearSession(chatId);
    return safeSend(chatId, '❌ Действие отменено. Нажмите /start для возврата в меню.');
  });

  // ── /status ────────────────────────────────────────────────────────────────
  bot.onText(/\/status (.+)/, async (msg, match) => {
    await showOrderStatus(msg.chat.id, match[1].trim());
  });

  // ── /help ──────────────────────────────────────────────────────────────────
  bot.onText(/\/help/, (msg) => {
    const chatId = msg.chat.id;
    if (isAdmin(chatId)) {
      return safeSend(chatId,
        `📖 *Справка администратора*\n\n/start — главное меню\n/cancel — сбросить действие\n/msg НМ\\-XXXX текст — написать клиенту\n\nВсе функции управления — через кнопки меню\\.`,
        { parse_mode: 'MarkdownV2' }
      );
    }
    return safeSend(chatId,
      `📖 *Справка Nevesty Models*\n\n/start — главное меню\n/status НОМЕР — статус заявки\n/cancel — отменить действие\n\nЕсли есть вопросы — напишите нам, менеджер ответит\\!`,
      { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: [[{ text:'🏠 Меню', callback_data:'main_menu' }]] } }
    );
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

  // ── /contacts ──────────────────────────────────────────────────────────────
  bot.onText(/\/contacts/, async (msg) => {
    return showContacts(msg.chat.id);
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
    if (data === 'profile')    return showUserProfile(chatId, q.from.first_name);
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
      d.event_type = data.replace('bk_etype_','');
      return bkStep2Date(chatId, d);
    }

    // ── Booking: duration
    if (data.startsWith('bk_dur_')) {
      const session = await getSession(chatId);
      const d = sessionData(session);
      d.event_duration = data.replace('bk_dur_','');
      return bkStep2Location(chatId, d);
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
      await clearSession(chatId);
      return isAdmin(chatId) ? showAdminMenu(chatId, q.from.first_name) : showMainMenu(chatId, q.from.first_name);
    }

    // ── Admin orders list: adm_orders_{status}_{page}
    if (data.startsWith('adm_orders_')) {
      if (!isAdmin(chatId)) return;
      const parts  = data.replace('adm_orders_','').split('_');
      const page   = parseInt(parts.pop()) || 0;
      const status = parts.join('_');
      return showAdminOrders(chatId, status, page);
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
      if (!order) return safeSend(chatId, '❌ Заявка не найдена.');
      await setSession(chatId, 'replying', { order_id: orderId, order_number: order.order_number, client_name: order.client_name });
      return safeSend(chatId,
        `💬 Введите сообщение для клиента *${order.client_name}* \\(${esc(order.order_number)}\\):\n\n_/cancel — отменить_`,
        { parse_mode: 'MarkdownV2' }
      );
    }

    // ── Admin models
    if (data.startsWith('adm_models_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('adm_models_','')) || 0;
      return showAdminModels(chatId, page);
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

    // ── Settings
    if (data === 'adm_settings')  { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return showAdminSettings(chatId); }
    if (data === 'adm_broadcast') { if (!isAdmin(chatId)) return; await setSession(chatId, 'adm_broadcast_msg', {}); return showBroadcast(chatId); }
    if (data === 'adm_reviews')   { if (!isAdmin(chatId)) return; return showAdminReviews(chatId); }
    if (data.startsWith('rev_approve_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_approve_', ''));
      await run('UPDATE reviews SET approved=1 WHERE id=?', [id]).catch(()=>{});
      return safeSend(chatId, `Отзыв #${id} одобрен.`);
    }
    if (data.startsWith('rev_delete_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('rev_delete_', ''));
      await run('DELETE FROM reviews WHERE id=?', [id]).catch(()=>{});
      return safeSend(chatId, `Отзыв #${id} удалён.`);
    }
    if (data === 'adm_admins')    { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return showAdminManagement(chatId); }
    if (data === 'adm_export')    { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return exportOrders(chatId); }
    if (data === 'adm_addmodel')  { if (!isAdmin(chatId)) return; return showAddModelStep(chatId, { _step: 'name' }); }

    // ── Settings inputs — set session and ask for text
    const settingPrompts = {
      'adm_set_greeting': '📝 Введите новый текст *приветствия*\n\n_Текущий отображается при /start_:',
      'adm_set_about':    'ℹ️ Введите новый текст *«О нас»*:',
      'adm_set_phone':    '📞 Введите новый *номер телефона* агентства:',
      'adm_set_email':    '📧 Введите новый *email* агентства:',
      'adm_set_insta':    '📸 Введите новый *Instagram* (без @):',
      'adm_set_addr':     '📍 Введите новый *адрес* агентства:',
      'adm_set_pricing':  '💰 Введите новый *прайс-лист*\n(Можно несколько строк):',
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
      d2.category = data.replace('adm_mdl_cat_',''); d2._step = 'instagram';
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
                            hair_color:'цвет волос', eye_color:'цвет глаз', params:'параметры (ОГ/ОТ/ОБ)' };
      await setSession(chatId, `adm_ef_${modelId}_${field}`, {});
      return safeSend(chatId, `✏️ Введите новое *${fieldLabels[field]||field}*:`, {
        reply_markup: { inline_keyboard: [[{ text: '← Отмена', callback_data: `adm_editmodel_${modelId}` }]] } });
    }
    if (data.startsWith('adm_efc_')) {  // edit field category
      if (!isAdmin(chatId)) return;
      const parts = data.replace('adm_efc_','').split('_');
      const modelId = parseInt(parts[0]);
      const cat = parts[1];
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

    // ── Agent feed
    if (data.startsWith('agent_feed_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('agent_feed_','')) || 0;
      return showAgentFeed(chatId, page);
    }

    // ── Agent discussions feed
    if (data === 'adm_discussions') {
      if (!isAdmin(chatId)) return;
      return showAgentDiscussions(chatId);
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
  });

  // ── Message handler ────────────────────────────────────────────────────────
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId  = msg.chat.id;
    const text    = msg.text.trim();
    const session = await getSession(chatId);
    const state   = session?.state || 'idle';
    const d       = sessionData(session);

    // ── ReplyKeyboard кнопки клиента ─────────────────────────────────────────
    if (state === 'idle' || state === 'check_status') {
      // Клиентские кнопки
      if (!isAdmin(chatId)) {
        if (text === '💃 Каталог')         return showCatalog(chatId, null, 0);
        if (text === '📝 Подать заявку')   return bkStep1(chatId);
        if (text === '📋 Мои заявки')      return showMyOrders(chatId);
        if (text === '🔍 Статус заявки') {
          await setSession(chatId, 'check_status', {});
          return safeSend(chatId, '🔍 Введите номер заявки (например, НМ-001):');
        }
        if (text === '❓ FAQ')             return showFaq(chatId);
        if (text === '👤 Профиль')         return showUserProfile(chatId, msg.from.first_name);
        if (text === '📞 Контакты')        return showContacts(chatId);
      }
      // Кнопки администратора
      if (isAdmin(chatId)) {
        if (text === '📋 Заявки')          return showAdminOrders(chatId, 0);
        if (text === '💃 Модели')          return showAdminModels(chatId, 0);
        if (text === '📊 Статистика')      return showAdminStats(chatId);
        if (text === '🤖 Организм')        return showOrganismStatus(chatId);
        if (text === '📡 Фид агентов')     return showAgentFeed(chatId, 0);
        if (text === '💬 Обсуждения')      return showAgentDiscussions(chatId);
        if (text === '⚙️ Настройки')      return showAdminSettings(chatId);
        if (text === '📢 Рассылка')        return showBroadcast(chatId);
        if (text === '📤 Экспорт')         return exportOrders(chatId);
      }
    }

    // ── Admin: settings text inputs
    if (isAdmin(chatId)) {
      const settingStates = {
        'adm_set_greeting': ['greeting',       '📝 Приветствие обновлено!'],
        'adm_set_about':    ['about',           'ℹ️ Текст «О нас» обновлён!'],
        'adm_set_phone':    ['contacts_phone',  '📞 Телефон обновлён!'],
        'adm_set_email':    ['contacts_email',  '📧 Email обновлён!'],
        'adm_set_insta':    ['contacts_insta',  '📸 Instagram обновлён!'],
        'adm_set_addr':     ['contacts_addr',   '📍 Адрес обновлён!'],
        'adm_set_pricing':  ['pricing',         '💰 Прайс-лист обновлён!'],
      };
      if (settingStates[state]) {
        const [key, okMsg] = settingStates[state];
        await setSetting(key, text);
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

      // ── Broadcast
      if (state === 'adm_broadcast_msg') {
        return sendBroadcast(chatId, text);
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
                         shoe_size:'shoe_size', instagram:'instagram', bio:'bio', eye_color:'eye_color', hair_color:'hair_color' };
      if (field === 'params') {
        const ps = text.split('/').map(x => parseInt(x.trim()));
        if (ps.length === 3 && ps.every(Boolean)) {
          await run('UPDATE models SET bust=?,waist=?,hips=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
            [ps[0],ps[1],ps[2],modelId]).catch(()=>{});
        }
      } else if (fieldMap[field]) {
        const val = ['age','height','weight'].includes(field) ? (parseInt(text)||null) : text;
        await run(`UPDATE models SET ${fieldMap[field]}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?`, [val, modelId]).catch(()=>{});
      }
      await clearSession(chatId);
      return safeSend(chatId, '✅ Поле обновлено!', {
        reply_markup: { inline_keyboard: [[{ text: '✏️ Редактировать ещё', callback_data: `adm_editmodel_${modelId}` }, { text: '← Карточка', callback_data: `adm_model_${modelId}` }]] }
      });
    }

    // ── Admin reply to client
    if (isAdmin(chatId) && state === 'replying' && d.order_id) {
      const order = await get('SELECT * FROM orders WHERE id=?', [d.order_id]).catch(()=>null);
      if (!order) { await clearSession(chatId); return safeSend(chatId, '❌ Заявка не найдена.'); }
      const adm = await get('SELECT username FROM admins WHERE telegram_id=?', [String(chatId)]).catch(()=>null);
      await run('INSERT INTO messages (order_id,sender_type,sender_name,content) VALUES (?,?,?,?)',
        [d.order_id, 'admin', adm?.username||'Менеджер', text]);
      if (order.client_chat_id) await sendMessageToClient(order.client_chat_id, order.order_number, text);
      await clearSession(chatId);
      return safeSend(chatId, `✅ Сообщение отправлено клиенту ${order.client_name}.`, {
        reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `adm_order_${d.order_id}` }]] }
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
        ? `📩 *Сообщение от клиента*\nЗаявка: *${order.order_number}*\nКлиент: ${clientName} ${username}\n\n`
        : `📩 *Новое сообщение*\n${clientName} ${username}\n\n`;
      await Promise.allSettled(adminIds.map(id => safeSend(id, header + text, {
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
    `🆕 *Новая заявка!*\n\n` +
    `📋 *${order.order_number}*\n` +
    `👤 ${order.client_name}\n📞 ${order.client_phone}\n` +
    (order.client_email    ? `📧 ${order.client_email}\n`                          : '') +
    (order.client_telegram ? `💬 @${String(order.client_telegram).replace('@','')}\n` : '') +
    `\n🎭 ${EVENT_TYPES[order.event_type]||order.event_type}\n` +
    (order.event_date  ? `📅 ${order.event_date}\n`  : '') +
    (order.location    ? `📍 ${order.location}\n`    : '') +
    (order.budget      ? `💰 ${order.budget}\n`      : '') +
    (modelName         ? `💃 ${modelName}\n`         : '') +
    (order.comments    ? `\n💬 ${order.comments}`    : '');

  const ids = await getAdminChatIds();
  await Promise.allSettled(ids.map(id => safeSend(id, text, {
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
  const msgs = {
    confirmed:   `✅ *Заявка ${orderNumber} подтверждена!*\n\nМенеджер свяжется с вами для уточнения деталей.`,
    reviewing:   `🔍 *Заявка ${orderNumber} принята в работу.*\n\nМы изучаем ваш запрос.`,
    in_progress: `▶️ *Заявка ${orderNumber} выполняется.*`,
    completed:   `🏁 *Заявка ${orderNumber} завершена!*\n\nСпасибо, что выбрали Nevesty Models! 💎`,
    cancelled:   `❌ *Заявка ${orderNumber} отклонена.*\n\nЕсли есть вопросы — свяжитесь с нами.`,
  };
  const text = msgs[newStatus];
  if (text) await safeSend(clientChatId, text, {});
}

async function sendMessageToClient(clientChatId, orderNumber, text) {
  if (!bot || !clientChatId) return;
  await safeSend(clientChatId, `💬 *Сообщение от менеджера* \\(${esc(orderNumber)}\\):\n\n${esc(text)}`, { parse_mode: 'MarkdownV2' });
}

// ─── FAQ ──────────────────────────────────────────────────────────────────────

async function showFaq(chatId) {
  const text =
    `FAQ - Часто задаваемые вопросы\n\n` +
    `1. Как заказать модель?\n` +
    `Нажмите "Оформить заявку" в главном меню, выберите модель и заполните форму. Менеджер свяжется с вами в течение 1 часа.\n\n` +
    `2. Какова минимальная длительность работы?\n` +
    `Минимальный заказ — 1 час. Доступны варианты 1, 2, 3, 4, 6, 8 и 12 часов.\n\n` +
    `3. Какие типы мероприятий доступны?\n` +
    `Показы мод, фотосессии, корпоративы и мероприятия, коммерческие съёмки, подиум и другие форматы.\n\n` +
    `4. Как отследить статус заявки?\n` +
    `Используйте команду /status НОМЕР-ЗАЯВКИ или кнопку "Проверить статус" в меню. Номер заявки приходит сразу после оформления.\n\n` +
    `5. Можно ли выбрать конкретную модель?\n` +
    `Да! В каталоге доступны все свободные модели с параметрами. Либо укажите пожелания, и менеджер подберёт подходящий вариант.\n\n` +
    `6. В каких городах работает агентство?\n` +
    `Мы работаем по всей России. Укажите город при оформлении заявки — менеджер уточнит условия выезда.\n\n` +
    `7. Как происходит оплата?\n` +
    `Оплата обсуждается с менеджером после подтверждения заявки. Принимаем банковский перевод и наличные.\n\n` +
    `8. Можно ли отменить заявку?\n` +
    `Да, свяжитесь с менеджером или напишите в чат. Отмена возможна не позднее чем за 24 часа до мероприятия без штрафных санкций.`;

  return safeSend(chatId, text, {
    reply_markup: { inline_keyboard: [
      [{ text: '📝 Оформить заявку', callback_data: 'bk_start'   }],
      [{ text: '📞 Контакты',        callback_data: 'contacts'   }],
      [{ text: '🏠 Главное меню',    callback_data: 'main_menu'  }],
    ]}
  });
}

// ─── User Profile ──────────────────────────────────────────────────────────────

async function showUserProfile(chatId, firstName) {
  try {
    const orders = await query(
      `SELECT o.status, o.created_at FROM orders o
       WHERE o.client_chat_id = ?
       ORDER BY o.created_at ASC`,
      [String(chatId)]
    );

    if (!orders.length) {
      return safeSend(chatId,
        `Ваш профиль\n\nИмя: ${firstName || 'Гость'}\n\nУ вас пока нет заявок. Оформите первую прямо сейчас и начните сотрудничество с Nevesty Models!`,
        {
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

    const firstDate = orders[0].created_at
      ? new Date(orders[0].created_at).toLocaleDateString('ru')
      : 'неизвестно';

    let text = `Ваш профиль\n\n`;
    text += `Имя: ${firstName || 'Гость'}\n`;
    text += `Всего заявок: ${orders.length}\n`;
    text += `Первая заявка: ${firstDate}\n\n`;
    text += `По статусам:\n`;

    const statusOrder = ['new','reviewing','confirmed','in_progress','completed','cancelled'];
    for (const st of statusOrder) {
      if (counts[st]) {
        const label = STATUS_LABELS[st] || st;
        text += `  ${label}: ${counts[st]}\n`;
      }
    }

    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: '📋 Мои заявки',    callback_data: 'my_orders'  }],
        [{ text: '📝 Новая заявка',  callback_data: 'bk_start'   }],
        [{ text: '🏠 Главное меню',  callback_data: 'main_menu'  }],
      ]}
    });
  } catch (e) { console.error('[Bot] showUserProfile:', e.message); }
}

// ─── Admin Reviews ────────────────────────────────────────────────────────────

async function showAdminReviews(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const reviews = await query('SELECT * FROM reviews WHERE approved=0 ORDER BY created_at DESC').catch(()=>[]);
    if (!reviews.length) {
      return safeSend(chatId, 'Нет отзывов на модерации.', {
        reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
      });
    }
    for (const r of reviews) {
      const stars = '⭐'.repeat(Math.max(1, Math.min(5, r.rating || 1)));
      const text = `Отзыв #${r.id}\nИмя: ${r.client_name}\nОценка: ${stars}\n\n${r.text}`;
      await safeSend(chatId, text, {
        reply_markup: { inline_keyboard: [[
          { text: '✅ Одобрить', callback_data: `rev_approve_${r.id}` },
          { text: '❌ Удалить',  callback_data: `rev_delete_${r.id}`  },
        ]]}
      });
    }
    return safeSend(chatId, `Всего на модерации: ${reviews.length}`, {
      reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
    });
  } catch (e) { console.error('[Bot] showAdminReviews:', e.message); }
}

module.exports = { initBot, notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient };
