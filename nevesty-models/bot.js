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
const VALID_STATUSES = ['new','reviewing','confirmed','in_progress','completed','cancelled'];

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

function buildClientKeyboard() {
  const rows = [
    [{ text: '⭐ Топ-модели',           callback_data: 'cat_top_0'          },
     { text: '💃 Все модели',           callback_data: 'cat_cat__0'         }],
    [{ text: '👗 Fashion',              callback_data: 'cat_filter_fashion'  },
     { text: '📷 Commercial',           callback_data: 'cat_filter_commercial'},
     { text: '🎉 Events',              callback_data: 'cat_filter_events'   }],
    [{ text: '🔍 Поиск по параметрам', callback_data: 'cat_search'          },
     { text: '📏 Поиск по росту',      callback_data: 'search_height_input' }],
    [{ text: '📝 Оформить заявку',      callback_data: 'bk_start'           },
     { text: '⚡ Быстрая заявка',       callback_data: 'bk_quick'           }],
    [{ text: '❤️ Избранное',            callback_data: 'fav_list_0'         },
     { text: '💬 Написать менеджеру',   callback_data: 'contact_mgr'        }],
    [{ text: '📋 Мои заявки',           callback_data: 'my_orders'          },
     { text: '🔍 Статус заявки',        callback_data: 'check_status'       }],
    [{ text: '⭐ Отзывы',              callback_data: 'show_reviews'        },
     { text: '💰 Прайс-лист',          callback_data: 'pricing'            }],
    [{ text: 'ℹ️ О нас',               callback_data: 'about_us'           },
     { text: '📞 Контакты',             callback_data: 'contacts'           },
     { text: '❓ FAQ',                  callback_data: 'faq'                }],
    [{ text: '👤 Мой профиль',          callback_data: 'profile'            }],
  ];
  if (SITE_URL.startsWith('https://')) {
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
       { text: '📈 Дашборд',                callback_data: 'adm_dashboard'  }],
      [{ text: `🤖 Организм${health}`,       callback_data: 'adm_organism'   },
       { text: '⚙️ Настройки',              callback_data: 'adm_settings'   }],
      [{ text: '📢 Рассылка',               callback_data: 'adm_broadcast'  },
       { text: '📤 Экспорт заявок',         callback_data: 'adm_export'     }],
      [{ text: '➕ Добавить модель',         callback_data: 'adm_addmodel'   },
       { text: '👑 Администраторы',          callback_data: 'adm_admins'     }],
      [{ text: '📡 Фид агентов',            callback_data: 'agent_feed_0'   },
       { text: '⭐ Отзывы',                 callback_data: 'adm_reviews'    },
       { text: '💬 Обсуждения',            callback_data: 'adm_discussions'}],
      [{ text: '🏭 AI Factory',             callback_data: 'adm_factory'    },
       { text: '💡 Growth Actions',         callback_data: 'adm_factory_growth' }],
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
    // Greeting is user-edited content — send as plain text to avoid injection
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
    if (!m) return safeSend(chatId, '❌ Модель не найдена\\.', {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '💃 Каталог', callback_data: 'cat_cat__0' }]] }
    });

    const lines = [];
    if (m.age)                       lines.push(`📅 Возраст: *${m.age}* лет`);
    if (m.height)                    lines.push(`📏 Рост: *${m.height}* см`);
    if (m.weight)                    lines.push(`⚖️ Вес: *${m.weight}* кг`);
    if (m.bust && m.waist && m.hips) lines.push(`📐 Параметры: *${m.bust}/${m.waist}/${m.hips}*`);
    if (m.shoe_size)                 lines.push(`👟 Обувь: *${esc(m.shoe_size)}*`);
    if (m.hair_color)                lines.push(`💇 Волосы: *${esc(m.hair_color)}*`);
    if (m.eye_color)                 lines.push(`👁 Глаза: *${esc(m.eye_color)}*`);
    if (m.category)                  lines.push(`🏷 Категория: *${esc(m.category)}*`);
    if (m.city)                      lines.push(`🏙 Город: *${esc(m.city)}*`);
    if (m.instagram)                 lines.push(`📸 @${esc(m.instagram)}`);

    const avail   = m.available ? '🟢 Доступна для заказа' : '🔴 Временно недоступна';
    const star    = m.featured ? '⭐ ' : '';
    // Caption ≤ 1024 chars (Telegram limit for media)
    const bioEsc  = m.bio ? esc(m.bio) : '';
    const bioFits = bioEsc.slice(0, 180) + (bioEsc.length > 180 ? '…' : '');
    const captionParts = [`💃 ${star}*${esc(m.name)}*`, '', ...lines, '', avail];
    if (bioFits) captionParts.push('', `_${bioFits}_`);
    const caption = captionParts.join('\n').slice(0, 1020);

    const contactBtn = m.phone || m.instagram
      ? [{ text: '📱 Получить контакт', callback_data: `model_contact_${m.id}` }]
      : [];
    const keyboard = {
      inline_keyboard: [
        m.available ? [{ text: '📝 Заказать эту модель', callback_data: `bk_model_${m.id}` }] : [],
        contactBtn,
        [{ text: '❤️ В избранное', callback_data: `fav_add_${m.id}` },
         { text: '💔 Убрать',      callback_data: `fav_remove_${m.id}` }],
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
      return safeSend(chatId, '❌ Заявка не найдена\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '📋 Мои заявки', callback_data: 'my_orders' }]] }
      });
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
    const repeatBtn = (o.status === 'completed' || o.status === 'cancelled')
      ? [{ text: '🔁 Повторить заявку', callback_data: `repeat_order_${o.id}` }]
      : [];
    const reviewBtn = o.status === 'completed'
      ? [{ text: '⭐ Оставить отзыв', callback_data: `leave_review_${o.id}` }]
      : [];

    const kb = [
      [{ text: '← Мои заявки', callback_data: 'my_orders' }],
      [{ text: '🏠 Меню',      callback_data: 'main_menu' }],
    ];
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

    const activeFilter = safe || '';
    const filterRow1 = [
      { text: (activeFilter === '') ? '📋 Все ✓' : '📋 Все',             callback_data: 'adm_orders__0'         },
      { text: (activeFilter === 'new') ? '🆕 Новые ✓' : '🆕 Новые',       callback_data: 'adm_orders_new_0'      },
      { text: (activeFilter === 'confirmed') ? '✅ Подтвержд. ✓' : '✅ Подтвержд.', callback_data: 'adm_orders_confirmed_0' },
    ];
    const filterRow2 = [
      { text: (activeFilter === 'cancelled') ? '❌ Отменённые ✓' : '❌ Отменённые', callback_data: 'adm_orders_cancelled_0' },
      { text: (activeFilter === 'completed') ? '🏁 Завершённые ✓' : '🏁 Завершённые', callback_data: 'adm_orders_completed_0' },
    ];

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        filterRow1,
        filterRow2,
        ...btns,
        ...(nav.length ? [nav] : []),
        [{ text: '🔍 Найти заявку', callback_data: 'adm_search_order' },
         { text: '← Меню',         callback_data: 'admin_menu'        }],
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

    let text = `📋 *${esc(o.order_number)}*\nСтатус: ${esc(STATUS_LABELS[o.status]||o.status)}\n\n`;
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
    keyboard.push([{ text: '🕐 История статусов', callback_data: `adm_order_history_${orderId}` }]);
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
    if (!order) return safeSend(chatId, '❌ Заявка не найдена.');

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

async function showAdminStats(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [
      total, todayR, weekR, monthR,
      active,
      done, canc,
      newClients,
    ] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE date(created_at) = date('now')"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now','-7 days')"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now','-30 days')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('new','reviewing','confirmed','in_progress')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='completed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='cancelled'"),
      get("SELECT COUNT(DISTINCT client_chat_id) as n FROM orders WHERE created_at >= datetime('now','-30 days') AND client_chat_id IS NOT NULL"),
    ]);

    // Conversion: completed / (total - cancelled) * 100
    const denominator = (total.n || 0) - (canc.n || 0);
    const conversion = denominator > 0 ? Math.round((done.n / denominator) * 100) : 0;

    // Average budget
    let avgBudget = null;
    try {
      const budgetRow = await get(
        `SELECT AVG(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)) as avg
         FROM orders WHERE budget IS NOT NULL AND budget != '' AND budget GLOB '[0-9]*'`
      );
      if (budgetRow && budgetRow.avg) avgBudget = Math.round(budgetRow.avg);
    } catch {}

    // Top-3 models by order count
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

    const medals = ['🥇','🥈','🥉'];

    let text = `*📊 Статистика Nevesty Models*\n\n`;
    text += `*📅 Заявки:*\n`;
    text += `  Сегодня: *${esc(String(todayR.n))}*\n`;
    text += `  За неделю: *${esc(String(weekR.n))}*\n`;
    text += `  За месяц: *${esc(String(monthR.n))}*\n`;
    text += `  Всего: *${esc(String(total.n))}*\n\n`;
    text += `*🔥 Активных прямо сейчас:* ${esc(String(active.n))}\n`;
    text += `*✅ Завершено:* ${esc(String(done.n))}\n`;
    text += `*❌ Отклонено:* ${esc(String(canc.n))}\n\n`;
    text += `*📈 Конверсия:* ${esc(String(conversion))}%\n`;
    if (avgBudget) text += `*💰 Средний бюджет:* ${esc(String(avgBudget.toLocaleString('ru')))} руб\\.\n`;
    text += `*🆕 Новые клиенты \\(30 дней\\):* ${esc(String(newClients.n))}\n`;

    if (topModels.length) {
      text += `\n*🏆 Топ\\-3 модели по заявкам:*\n`;
      topModels.forEach((m, i) => {
        text += `  ${medals[i] || (i+1+'.')} ${esc(m.name)} — ${esc(String(m.cnt))} заявок\n`;
      });
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
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
    const [welcomePhoto, menuText, wishlistEnabled, searchEnabled, botLang] = await Promise.all([
      getSetting('welcome_photo_url'), getSetting('main_menu_text'),
      getSetting('wishlist_enabled'), getSetting('search_enabled'), getSetting('bot_language'),
    ]);
    const text =
      `🤖 Бот и интерфейс\n\n` +
      `🌐 Язык: ${botLang||'ru'}\n` +
      `🖼 Фото приветствия: ${welcomePhoto ? '✅ Задано' : '❌ Нет'}\n` +
      `📋 Текст главного меню: ${(menuText||'').slice(0,40)||'—'}\n` +
      `❤️ Избранное: ${wishlistEnabled==='0'?'❌ Выкл':'✅ Вкл'}\n` +
      `🔍 Поиск по росту: ${searchEnabled==='0'?'❌ Выкл':'✅ Вкл'}`;
    return safeSend(chatId, text, {
      reply_markup: { inline_keyboard: [
        [{ text: wishlistEnabled==='0' ? '❤️ Избранное ВКЛ' : '❤️ Избранное ВЫКЛ',
           callback_data: wishlistEnabled==='0' ? 'adm_wishlist_on' : 'adm_wishlist_off' }],
        [{ text: searchEnabled==='0' ? '🔍 Поиск ВКЛ' : '🔍 Поиск ВЫКЛ',
           callback_data: searchEnabled==='0' ? 'adm_search_on' : 'adm_search_off' }],
        [{ text: '🖼 Фото приветствия', callback_data: 'adm_set_welcome_photo'  },
         { text: '📋 Текст меню',      callback_data: 'adm_set_main_menu_text'  }],
        [{ text: '← Настройки', callback_data: 'adm_settings' }],
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
      [{ text: '📞 Телефон',    callback_data: `adm_ef_${modelId}_phone`      },
       { text: '🏙 Город',      callback_data: `adm_ef_${modelId}_city`       }],
      [{ text: '📝 Описание',   callback_data: `adm_ef_${modelId}_bio`        }],
      [{ text: '📷 Галерея фото', callback_data: `adm_gallery_${modelId}`      }],
      [{ text: m.available ? '🔴 Недоступна' : '🟢 Доступна', callback_data: `adm_toggle_${modelId}` },
       { text: m.featured ? '⭐ Убрать из топа' : '⭐ В топ', callback_data: `adm_featured_${modelId}` }],
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
  if (!clients.length) return safeSend(chatId, '❌ Нет клиентов для рассылки.', {
    reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] }
  });
  let sent = 0, failed = 0;
  for (const c of clients) {
    try {
      await bot.sendMessage(c.client_chat_id, `📢 *Сообщение от Nevesty Models*\n\n${esc(text)}`, { parse_mode: 'MarkdownV2' });
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

    // Deep-link: /start model_NNN  — прямая ссылка на карточку модели
    const ref = match[1]?.trim();
    if (ref) {
      const modelMatch = ref.match(/^model_(\d+)$/);
      if (modelMatch) {
        const modelId = parseInt(modelMatch[1]);
        const m = await get('SELECT id FROM models WHERE id=? AND available=1', [modelId]).catch(()=>null);
        if (m) return showModel(chatId, modelId);
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
    return showMainMenu(chatId, firstName);
  });

  // ── /admin ─────────────────────────────────────────────────────────────────
  bot.onText(/\/admin/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    return showAdminMenu(msg.chat.id, msg.from.first_name);
  });

  // ── /cancel ────────────────────────────────────────────────────────────────
  bot.onText(/\/cancel/, async (msg) => {
    const chatId = msg.chat.id;
    const s = await getSession(chatId);
    if (!s || s.state === 'idle') return safeSend(chatId, 'ℹ️ Нет активного действия.');
    await clearSession(chatId);
    return safeSend(chatId, '❌ Действие отменено.', {
      reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
    });
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
    if (data === 'about_us')   return showAboutUs(chatId);
    if (data === 'pricing')    return showPricing(chatId);
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

    // ── Admin order status history
    if (data.startsWith('adm_order_history_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_order_history_',''));
      return showOrderStatusHistory(chatId, id);
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
    if (data.startsWith('adm_featured_')) {
      if (!isAdmin(chatId)) return;
      const id = parseInt(data.replace('adm_featured_',''));
      const m  = await get('SELECT featured FROM models WHERE id=?', [id]).catch(()=>null);
      if (m) await run('UPDATE models SET featured=? WHERE id=?', [m.featured ? 0 : 1, id]);
      await bot.answerCallbackQuery(q.id, { text: m?.featured ? '⭐ Убрано из топа' : '⭐ Добавлено в топ' }).catch(()=>{});
      return showAdminModel(chatId, id);
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
    if (data === 'adm_broadcast') { if (!isAdmin(chatId)) return; await setSession(chatId, 'adm_broadcast_msg', {}); return showBroadcast(chatId); }
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
    if (data === 'adm_export')    { if (!isAdmin(chatId)) { await bot.answerCallbackQuery(q.id, { text: '⛔ Нет доступа', show_alert: true }).catch(()=>{}); return; } return exportOrders(chatId); }
    if (data === 'adm_addmodel')  { if (!isAdmin(chatId)) return; return showAddModelStep(chatId, { _step: 'name' }); }

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
                            hair_color:'цвет волос', eye_color:'цвет глаз', params:'параметры (ОГ/ОТ/ОБ)',
                            phone:'телефон модели', city:'город' };
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
    if (data === 'cat_filter_fashion')     return showCatalog(chatId, 'fashion',    0);
    if (data === 'cat_filter_commercial')  return showCatalog(chatId, 'commercial', 0);
    if (data === 'cat_filter_events')      return showCatalog(chatId, 'events',     0);

    // ── Поиск модели по параметрам
    if (data === 'cat_search') return showSearchMenu(chatId);
    if (data.startsWith('cat_search_height_')) {
      const range = data.replace('cat_search_height_', '');
      return showSearchResults(chatId, 'height', range, 0);
    }
    if (data.startsWith('cat_search_age_')) {
      const range = data.replace('cat_search_age_', '');
      return showSearchResults(chatId, 'age', range, 0);
    }
    if (data.startsWith('cat_search_res_')) {
      // cat_search_res_{type}_{range}_{page}
      const rest  = data.replace('cat_search_res_', '');
      const parts = rest.split('_');
      const page2 = parseInt(parts.pop()) || 0;
      const range = parts.pop();
      const type  = parts.join('_');
      return showSearchResults(chatId, type, range, page2);
    }

    // ── Отзывы (публичные)
    if (data === 'show_reviews')           return showPublicReviews(chatId, 0);
    if (data.startsWith('show_reviews_')) {
      const page = parseInt(data.replace('show_reviews_', '')) || 0;
      return showPublicReviews(chatId, page);
    }

    // ── Оставить отзыв
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

    // ── Повторить заявку
    if (data.startsWith('repeat_order_')) {
      const orderId = parseInt(data.replace('repeat_order_', ''));
      return repeatOrder(chatId, orderId);
    }

    // ── Профиль: изменить контакты
    if (data === 'profile_edit_contacts') return startEditProfile(chatId);
    if (data === 'profile_edit_phone') {
      await setSession(chatId, 'profile_edit_phone', {});
      return safeSend(chatId, '📞 Введите новый номер телефона:', {
        reply_markup: { inline_keyboard: [[{ text: '❌ Отмена', callback_data: 'profile' }]] }
      });
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
                         shoe_size:'shoe_size', instagram:'instagram', bio:'bio', eye_color:'eye_color',
                         hair_color:'hair_color', phone:'phone', city:'city' };
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

    // ── Leave review: text input
    if (state === 'leave_review_text') {
      if (!text || text.length < 5) {
        return safeSend(chatId, '❌ Отзыв слишком короткий. Напишите хотя бы несколько слов:');
      }
      const orderId = d.review_order_id;
      const rating  = d.review_rating || 5;
      let clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      try {
        const ord = await get('SELECT client_name FROM orders WHERE id=?', [orderId]);
        if (ord?.client_name) clientName = ord.client_name;
      } catch {}
      await run('INSERT INTO reviews (client_name, rating, text, model_id, approved) VALUES (?,?,?,?,0)',
        [clientName, rating, text, null]).catch(e => console.error('[Bot] insert review:', e.message));
      await clearSession(chatId);
      const adminIds2 = await getAdminChatIds();
      await Promise.allSettled(adminIds2.map(id => safeSend(id,
        `⭐ Новый отзыв от *${esc(clientName)}*\nОценка: ${'⭐'.repeat(rating)}\n\n${esc(text)}`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '✅ Модерация отзывов', callback_data: 'adm_reviews' }]] }
        }
      )));
      return safeSend(chatId,
        '✅ Спасибо за отзыв\\!\n\nОн появится после модерации\\.', {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
        }
      );
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

  // После завершения — предлагаем оставить отзыв
  if (newStatus === 'completed') {
    try {
      const order = await get('SELECT id FROM orders WHERE order_number=?', [orderNumber]).catch(()=>null);
      if (order) {
        keyboard.inline_keyboard.unshift([
          { text: '⭐ Оставить отзыв', callback_data: `leave_review_${order.id}` }
        ]);
      }
    } catch {}
  }

  await safeSend(clientChatId, text, { parse_mode: 'MarkdownV2', reply_markup: keyboard });
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

    let text = `👤 *Мой профиль*\n\n`;
    text += `Имя: *${esc(firstName || lastOrderFull?.client_name || 'Гость')}*\n`;
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
        [{ text: '✏️ Изменить контакты', callback_data: 'profile_edit_contacts' }],
        [{ text: '📝 Новая заявка',       callback_data: 'bk_start'             }],
        [{ text: '🏠 Главное меню',       callback_data: 'main_menu'            }],
      ]}
    });
  } catch (e) { console.error('[Bot] showUserProfile:', e.message); }
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
  const phone  = await getSetting('contacts_phone').catch(() => '+7 (900) 000-00-00');
  const insta  = await getSetting('contacts_insta').catch(() => '@nevesty_models');
  await setSession(chatId, 'msg_to_manager', {});
  return safeSend(chatId,
    `💬 *Связаться с менеджером*\n\n` +
    `Напишите ваш вопрос прямо здесь — менеджер ответит в течение часа\\.\n\n` +
    `Или свяжитесь напрямую:\n` +
    `📞 ${esc(phone)}\n` +
    `📸 Instagram: ${esc(insta)}`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '✍️ Написать вопрос сейчас', callback_data: 'msg_manager_start' }],
        [{ text: '🏠 Главное меню', callback_data: 'main_menu' }],
      ]}
    }
  );
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
  const pricing = await getSetting('pricing').catch(() =>
    'Fashion/Commercial — от 5000₽/час\nEvents — от 8000₽/час\nRunway — от 10000₽/час'
  );
  return safeSend(chatId,
    `💰 *Прайс\\-лист Nevesty Models*\n\n${esc(pricing)}\n\n` +
    `_Точная стоимость зависит от типа съёмки, длительности и модели\\._\n\n` +
    `Для точного расчёта — оставьте заявку или напишите менеджеру\\.`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📝 Оставить заявку',    callback_data: 'bk_start'    }],
        [{ text: '💬 Спросить менеджера', callback_data: 'contact_mgr' }],
        [{ text: '🏠 Меню',               callback_data: 'main_menu'   }],
      ]}
    }
  );
}

// ─── Каталог по городу ────────────────────────────────────────────────────────

async function showCatalogByCity(chatId, city, page = 0) {
  try {
    const perPage = 5;
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

// ─── Поиск модели по параметрам ──────────────────────────────────────────────

async function showSearchMenu(chatId) {
  return safeSend(chatId,
    `🔍 *Поиск модели по параметрам*\n\nВыберите критерий поиска:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '📏 Рост 160–165',  callback_data: 'cat_search_height_160-165' },
         { text: '📏 Рост 165–170',  callback_data: 'cat_search_height_165-170' }],
        [{ text: '📏 Рост 170–175',  callback_data: 'cat_search_height_170-175' },
         { text: '📏 Рост 175–185',  callback_data: 'cat_search_height_175-185' }],
        [{ text: '🎂 Возраст 18–22', callback_data: 'cat_search_age_18-22'     },
         { text: '🎂 Возраст 22–26', callback_data: 'cat_search_age_22-26'     }],
        [{ text: '🎂 Возраст 26–35', callback_data: 'cat_search_age_26-35'     }],
        [{ text: '🏠 Главное меню',   callback_data: 'main_menu'               }],
      ]}
    }
  );
}

async function showSearchResults(chatId, type, range, page) {
  try {
    page = parseInt(page) || 0;
    const [minStr, maxStr] = range.split('-');
    const minVal = parseInt(minStr) || 0;
    const maxVal = parseInt(maxStr) || 999;
    const perPage = 5;

    const col = type === 'height' ? 'height' : 'age';
    const models = await query(
      `SELECT * FROM models WHERE available=1 AND ${col} >= ? AND ${col} <= ? ORDER BY ${col}`,
      [minVal, maxVal]
    );

    const label = type === 'height' ? `рост ${range} см` : `возраст ${range} лет`;

    if (!models.length) {
      return safeSend(chatId,
        `🔍 По запросу «${esc(label)}» ничего не найдено\\.`,
        {
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: [
            [{ text: '🔍 Изменить поиск', callback_data: 'cat_search'  }],
            [{ text: '💃 Все модели',      callback_data: 'cat_cat__0' }],
          ]}
        }
      );
    }

    const total = models.length;
    const slice = models.slice(page * perPage, page * perPage + perPage);
    const modelBtns = slice.map(m => [{
      text: `${m.name}  ·  ${m.height}см  ·  ${m.age || '?'}л`,
      callback_data: `cat_model_${m.id}`
    }]);

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `cat_search_res_${type}_${range}_${page-1}` });
    if ((page+1)*perPage < total) nav.push({ text: '▶️', callback_data: `cat_search_res_${type}_${range}_${page+1}` });

    return safeSend(chatId,
      `🔍 *Результаты: ${esc(label)}*\n\nНайдено: ${total} ${ru_plural(total,'модель','модели','моделей')}`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [
          ...modelBtns,
          ...(nav.length ? [nav] : []),
          [{ text: '🔍 Новый поиск',  callback_data: 'cat_search'  }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu'   }],
        ]}
      }
    );
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
      text += `\n_${esc(r.text)}_\n\n`;
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
      return safeSend(chatId, '❌ Заявка не найдена\\.', {
        parse_mode: 'MarkdownV2',
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

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FEATURE B: Быстрая заявка (Quick Booking) ────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

async function bkQuickStart(chatId) {
  await setSession(chatId, 'bk_quick_name', {});
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
  return safeSend(chatId,
    `⚡ *Быстрая заявка*\n\n✅ Имя: *${esc(data.quick_name)}*\n\n📝 Шаг 2/2 — Введите номер телефона:`, {
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

    // Favorites list
    if (data.startsWith('fav_list_')) {
      const page = parseInt(data.replace('fav_list_', '')) || 0;
      return showFavorites(chatId, page);
    }

    // Favorites add/remove
    if (data.startsWith('fav_add_')) {
      return addFavorite(chatId, parseInt(data.replace('fav_add_', '')));
    }
    if (data.startsWith('fav_remove_')) {
      return removeFavorite(chatId, parseInt(data.replace('fav_remove_', '')));
    }

    // Quick booking
    if (data === 'bk_quick') return bkQuickStart(chatId);

    // Height search manual input
    if (data === 'search_height_input') return showHeightSearchInput(chatId);

    // Admin dashboard
    if (data === 'adm_dashboard') {
      if (!isAdmin(chatId)) return;
      return showAdminDashboard(chatId);
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
      d.quick_name = text;
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
        return showSearchResults(chatId, 'height', `${rangeMatch[1]}-${rangeMatch[2]}`, 0);
      } else if (singleMatch) {
        await clearSession(chatId);
        const h = parseInt(singleMatch[1]);
        return showSearchResults(chatId, 'height', `${h}-${h}`, 0);
      } else {
        return safeSend(chatId,
          '❌ Неверный формат\\. Введите диапазон, например: *170\\-180* или одно значение *175*',
          { parse_mode: 'MarkdownV2' }
        );
      }
    }
  });
}

module.exports = { initBot, notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient, _registerNewFeatures };
