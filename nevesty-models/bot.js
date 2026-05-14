require('dotenv').config();
const crypto = require('crypto');
const TelegramBot = require('node-telegram-bot-api');
const { query, run, get, generateOrderNumber } = require('./database');

const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
const SITE_URL = process.env.SITE_URL || 'http://localhost:3000';
const WEBHOOK_URL = process.env.WEBHOOK_URL || '';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || crypto.randomBytes(32).toString('hex');

const STATUS_LABELS = {
  new: '🆕 Новая',
  reviewing: '🔍 На рассмотрении',
  confirmed: '✅ Подтверждена',
  in_progress: '▶️ В процессе',
  completed: '🏁 Завершена',
  cancelled: '❌ Отменена'
};

const EVENT_TYPES = {
  fashion_show: '👗 Показ мод',
  photo_shoot: '📸 Фотосессия',
  event: '🎉 Корпоратив / Мероприятие',
  commercial: '📺 Коммерческая съёмка',
  runway: '🎭 Подиум',
  other: '✨ Другое'
};

const MODELS_PER_PAGE = 5;
const ORDERS_PER_PAGE = 8;
const AGENT_LOGS_PER_PAGE = 10;

let bot = null;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/([_*\[\]()~`>#+\-=|{}.!\\])/g, '\\$1');
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
  try {
    return await bot.sendMessage(chatId, text, opts);
  } catch (e) {
    if (opts.parse_mode && /parse entities|can't parse/i.test(e.message)) {
      try {
        const { parse_mode, ...rest } = opts;
        return await bot.sendMessage(chatId, text, rest);
      } catch {}
    }
    console.warn(`[Bot] send to ${chatId} failed: ${e.message}`);
    return null;
  }
}

async function safeEdit(chatId, messageId, text, opts = {}) {
  try {
    return await bot.editMessageText(text, { chat_id: chatId, message_id: messageId, ...opts });
  } catch (e) {
    if (opts.parse_mode && /parse entities|can't parse/i.test(e.message)) {
      try {
        const { parse_mode, ...rest } = opts;
        return await bot.editMessageText(text, { chat_id: chatId, message_id: messageId, ...rest });
      } catch {}
    }
  }
  return null;
}

// ─── Session helpers ──────────────────────────────────────────────────────────

const SESSION_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours

async function getSession(chatId) {
  try {
    const session = await get('SELECT * FROM telegram_sessions WHERE chat_id = ?', [String(chatId)]);
    if (!session) return null;
    // Expire stale non-idle sessions after 24 hours
    if (session.state !== 'idle' && session.updated_at) {
      const updatedAt = new Date(session.updated_at).getTime();
      if (Date.now() - updatedAt > SESSION_TTL_MS) {
        await clearSession(chatId);
        return null;
      }
    }
    return session;
  } catch { return null; }
}

async function setSession(chatId, state, data = {}) {
  try {
    await run(
      `INSERT OR REPLACE INTO telegram_sessions (chat_id, state, data, updated_at)
       VALUES (?, ?, ?, CURRENT_TIMESTAMP)`,
      [String(chatId), state, JSON.stringify(data)]
    );
  } catch (e) { console.error('[Bot] setSession error:', e.message); }
}

async function clearSession(chatId) {
  await setSession(chatId, 'idle', {});
}

function parseSessionData(session) {
  try { return JSON.parse(session?.data || '{}'); } catch { return {}; }
}

// ─── Client UI ────────────────────────────────────────────────────────────────

async function showMainMenu(chatId, firstName) {
  await clearSession(chatId);
  const name = firstName ? `, ${esc(firstName)}` : '';
  const keyboard = [
    [{ text: '💃 Каталог моделей', callback_data: 'catalog_0' }],
    [{ text: '📝 Оформить заявку', callback_data: 'start_booking' }],
    [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
    [{ text: '📞 Контакты', callback_data: 'contacts' }]
  ];
  if (SITE_URL.startsWith('https://')) {
    keyboard.push([{ text: '🌐 Открыть сайт', web_app: { url: SITE_URL } }]);
  }
  return safeSend(chatId,
    `💎 *Nevesty Models*\n\nДобро пожаловать${name}\\!\n\nЧем могу помочь?`,
    { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: keyboard } }
  );
}

async function showCatalog(chatId, page = 0) {
  try {
    const total = (await get('SELECT COUNT(*) as n FROM models WHERE available = 1')).n;
    if (!total) {
      return safeSend(chatId, '📭 Каталог временно недоступен\\. Попробуйте позже\\.', { parse_mode: 'MarkdownV2' });
    }
    const offset = page * MODELS_PER_PAGE;
    const models = await query(
      'SELECT * FROM models WHERE available = 1 ORDER BY id DESC LIMIT ? OFFSET ?',
      [MODELS_PER_PAGE, offset]
    );

    let text = `💃 *Каталог моделей* \\(стр\\. ${page + 1}/${Math.ceil(total / MODELS_PER_PAGE)}\\)\n\n`;
    const modelButtons = models.map(m => [{
      text: `${esc(m.name)} · ${m.height}см · ${esc(m.category)}`,
      callback_data: `model_${m.id}`
    }]);

    const nav = [];
    if (page > 0) nav.push({ text: '◀️ Назад', callback_data: `catalog_${page - 1}` });
    if (offset + MODELS_PER_PAGE < total) nav.push({ text: 'Вперёд ▶️', callback_data: `catalog_${page + 1}` });

    const keyboard = [
      ...modelButtons,
      ...(nav.length ? [nav] : []),
      [{ text: '📝 Оформить заявку', callback_data: 'start_booking' }],
      [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
    ];

    for (const m of models) {
      text += `▪️ *${esc(m.name)}* — ${m.height}см, ${m.age || '?'} лет, ${esc(m.category)}\n`;
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: keyboard }
    });
  } catch (e) {
    console.error('[Bot] showCatalog error:', e.message);
  }
}

async function showModel(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id = ?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена\\.', { parse_mode: 'MarkdownV2' });

    const params = [
      m.height && `Рост: ${m.height} см`,
      m.weight && `Вес: ${m.weight} кг`,
      m.bust && m.waist && m.hips && `Параметры: ${m.bust}/${m.waist}/${m.hips}`,
      m.shoe_size && `Обувь: ${m.shoe_size}`,
      m.hair_color && `Волосы: ${esc(m.hair_color)}`,
      m.eye_color && `Глаза: ${esc(m.eye_color)}`,
    ].filter(Boolean);

    let text = `💃 *${esc(m.name)}*\n`;
    if (m.age) text += `Возраст: ${m.age} лет\n`;
    if (params.length) text += params.join(' \\| ') + '\n';
    if (m.category) text += `Категория: ${esc(m.category)}\n`;
    if (m.bio) text += `\n${esc(m.bio)}\n`;
    if (m.instagram) text += `\n📸 Instagram: ${esc(m.instagram)}\n`;

    const available = m.available ? '🟢 Доступна для заказа' : '🔴 Временно недоступна';
    text += `\n${available}`;

    const keyboard = [
      m.available ? [{ text: '📝 Заказать эту модель', callback_data: `book_model_${m.id}` }] : [],
      [{ text: '← Каталог', callback_data: 'catalog_0' }, { text: '🏠 Меню', callback_data: 'main_menu' }]
    ].filter(r => r.length > 0);

    if (m.photo_main) {
      try {
        await bot.sendPhoto(chatId, m.photo_main, {
          caption: text,
          parse_mode: 'MarkdownV2',
          reply_markup: { inline_keyboard: keyboard }
        });
        return;
      } catch {}
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: keyboard }
    });
  } catch (e) {
    console.error('[Bot] showModel error:', e.message);
  }
}

async function showClientOrders(chatId) {
  try {
    const orders = await query(
      `SELECT o.*, m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       WHERE o.client_chat_id = ?
       ORDER BY o.created_at DESC LIMIT 10`,
      [String(chatId)]
    );

    if (!orders.length) {
      return safeSend(chatId,
        '📭 *Ваши заявки*\n\nУ вас пока нет заявок\\. Оформите первую прямо сейчас\\!',
        {
          parse_mode: 'MarkdownV2',
          reply_markup: {
            inline_keyboard: [
              [{ text: '📝 Оформить заявку', callback_data: 'start_booking' }],
              [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
            ]
          }
        }
      );
    }

    let text = `📋 *Ваши заявки:*\n\n`;
    const buttons = orders.map(o => {
      const status = STATUS_LABELS[o.status] || o.status;
      text += `${status} *${esc(o.order_number)}*\n`;
      text += `${esc(EVENT_TYPES[o.event_type] || o.event_type)}`;
      if (o.event_date) text += ` · ${esc(o.event_date)}`;
      text += '\n\n';
      return [{ text: `${o.order_number} — ${STATUS_LABELS[o.status] || o.status}`, callback_data: `client_order_${o.id}` }];
    });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [...buttons, [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]]
      }
    });
  } catch (e) {
    console.error('[Bot] showClientOrders error:', e.message);
  }
}

async function showClientOrder(chatId, orderId) {
  try {
    const o = await get(
      `SELECT o.*, m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id = m.id WHERE o.id = ?`,
      [orderId]
    );
    if (!o || o.client_chat_id !== String(chatId)) {
      return safeSend(chatId, '❌ Заявка не найдена\\.', { parse_mode: 'MarkdownV2' });
    }

    let text = `📋 *Заявка ${esc(o.order_number)}*\n\n`;
    text += `Статус: ${STATUS_LABELS[o.status] || o.status}\n`;
    text += `Мероприятие: ${esc(EVENT_TYPES[o.event_type] || o.event_type)}\n`;
    if (o.event_date) text += `Дата: ${esc(o.event_date)}\n`;
    if (o.location) text += `Место: ${esc(o.location)}\n`;
    if (o.model_name) text += `Модель: ${esc(o.model_name)}\n`;
    if (o.budget) text += `Бюджет: ${esc(o.budget)}\n`;

    const lastMsg = await get(
      'SELECT * FROM messages WHERE order_id = ? ORDER BY created_at DESC LIMIT 1',
      [orderId]
    );
    if (lastMsg) {
      text += `\n💬 Последнее сообщение:\n_${esc(lastMsg.content)}_`;
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '← Мои заявки', callback_data: 'my_orders' }],
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
        ]
      }
    });
  } catch (e) {
    console.error('[Bot] showClientOrder error:', e.message);
  }
}

async function showContacts(chatId) {
  const phone = esc(process.env.AGENCY_PHONE || '+7 (800) 555-00-00');
  const email = esc(process.env.AGENCY_EMAIL || 'info@nevesty-models.ru');
  const site = esc(SITE_URL);
  return safeSend(chatId,
    `📞 *Контакты Nevesty Models*\n\nТелефон: ${phone}\nEmail: ${email}\nСайт: ${site}\n\nМы работаем 7 дней в неделю с 9:00 до 21:00`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
        ]
      }
    }
  );
}

// ─── Booking wizard ───────────────────────────────────────────────────────────

async function startBooking(chatId, preModelId = null) {
  const data = {};
  if (preModelId) {
    try {
      const m = await get('SELECT id, name FROM models WHERE id = ? AND available = 1', [preModelId]);
      if (m) { data.model_id = m.id; data.model_name = m.name; }
    } catch {}
  }

  await setSession(chatId, 'bk_name', data);
  const modelHint = data.model_name ? `\n\n✅ Модель выбрана: *${esc(data.model_name)}*` : '';
  return safeSend(chatId,
    `📝 *Оформление заявки*${modelHint}\n\n*Шаг 1/8:* Введите ваше имя и фамилию:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'booking_cancel' }]] }
    }
  );
}

async function bookingAskPhone(chatId, data) {
  await setSession(chatId, 'bk_phone', data);
  return safeSend(chatId,
    `*Шаг 2/8:* Введите ваш номер телефона:\n_Например: \\+7 900 123 45 67_`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'booking_cancel' }]] }
    }
  );
}

async function bookingAskEmail(chatId, data) {
  await setSession(chatId, 'bk_email', data);
  return safeSend(chatId,
    `*Шаг 3/8:* Введите ваш email \\(необязательно\\):`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '⏭️ Пропустить', callback_data: 'bk_skip_email' }],
          [{ text: '❌ Отменить', callback_data: 'booking_cancel' }]
        ]
      }
    }
  );
}

async function bookingAskEventType(chatId, data) {
  await setSession(chatId, 'bk_event', data);
  const buttons = Object.entries(EVENT_TYPES).map(([k, v]) => [{ text: v, callback_data: `bk_event_${k}` }]);
  return safeSend(chatId,
    `*Шаг 4/8:* Выберите тип мероприятия:`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [...buttons, [{ text: '❌ Отменить', callback_data: 'booking_cancel' }]]
      }
    }
  );
}

async function bookingAskDate(chatId, data) {
  await setSession(chatId, 'bk_date', data);
  return safeSend(chatId,
    `*Шаг 5/8:* Введите дату мероприятия:\n_Например: 25\\.06\\.2025 или июнь 2025_`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'booking_cancel' }]] }
    }
  );
}

async function bookingAskModel(chatId, data) {
  if (data.model_id) {
    return bookingAskDuration(chatId, data);
  }
  await setSession(chatId, 'bk_model', data);
  try {
    const models = await query('SELECT id, name, category FROM models WHERE available = 1 ORDER BY id LIMIT 10');
    const buttons = models.map(m => [{ text: `${m.name} (${m.category})`, callback_data: `bk_model_${m.id}` }]);
    return safeSend(chatId,
      `*Шаг 6/8:* Выберите модель или укажите любую:`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            ...buttons,
            [{ text: '✨ Любая подходящая', callback_data: 'bk_model_any' }],
            [{ text: '❌ Отменить', callback_data: 'booking_cancel' }]
          ]
        }
      }
    );
  } catch (e) {
    console.error('[Bot] bookingAskModel error:', e.message);
  }
}

async function bookingAskDuration(chatId, data) {
  await setSession(chatId, 'bk_duration', data);
  const modelNote = data.model_name ? `\n✅ Модель: *${esc(data.model_name)}*` : '';
  return safeSend(chatId,
    `*Шаг ${data.model_id ? '6' : '7'}/8:* Выберите продолжительность:${modelNote}`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [
            { text: '2 часа', callback_data: 'bk_dur_2' },
            { text: '4 часа', callback_data: 'bk_dur_4' },
            { text: '8 часов', callback_data: 'bk_dur_8' }
          ],
          [{ text: 'Весь день', callback_data: 'bk_dur_12' }],
          [{ text: '❌ Отменить', callback_data: 'booking_cancel' }]
        ]
      }
    }
  );
}

async function bookingAskLocation(chatId, data) {
  await setSession(chatId, 'bk_location', data);
  return safeSend(chatId,
    `*Шаг 7/8:* Укажите место проведения \\(город, адрес\\):`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '❌ Отменить', callback_data: 'booking_cancel' }]] }
    }
  );
}

async function bookingAskBudget(chatId, data) {
  await setSession(chatId, 'bk_budget', data);
  return safeSend(chatId,
    `*Шаг 8/8:* Укажите ваш бюджет \\(необязательно\\):`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [{ text: '⏭️ Пропустить', callback_data: 'bk_skip_budget' }],
          [{ text: '❌ Отменить', callback_data: 'booking_cancel' }]
        ]
      }
    }
  );
}

async function bookingShowConfirm(chatId, data) {
  // Guard: if required fields are missing (corrupted session), abort and restart
  if (!data.client_name || !data.client_phone || !data.event_type || !data.event_date) {
    await clearSession(chatId);
    return safeSend(chatId,
      '❌ Данные заявки повреждены или устарели\\. Начните заново:',
      {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '📝 Новая заявка', callback_data: 'start_booking' }]] }
      }
    );
  }

  await setSession(chatId, 'bk_confirm', data);

  const dur = data.duration ? `${data.duration} ч.` : '';
  let text = `📋 *Проверьте данные заявки:*\n\n`;
  text += `👤 Имя: *${esc(data.client_name)}*\n`;
  text += `📞 Телефон: *${esc(data.client_phone)}*\n`;
  if (data.client_email) text += `📧 Email: ${esc(data.client_email)}\n`;
  text += `🎭 Мероприятие: *${esc(EVENT_TYPES[data.event_type] || data.event_type)}*\n`;
  text += `📅 Дата: *${esc(data.event_date)}*\n`;
  if (data.model_name) text += `💃 Модель: *${esc(data.model_name)}*\n`;
  else text += `💃 Модель: любая подходящая\n`;
  if (dur) text += `⏱ Продолжительность: ${dur}\n`;
  if (data.location) text += `📍 Место: ${esc(data.location)}\n`;
  if (data.budget) text += `💰 Бюджет: ${esc(data.budget)}\n`;

  text += `\nВсё верно?`;

  return safeSend(chatId, text, {
    parse_mode: 'MarkdownV2',
    reply_markup: {
      inline_keyboard: [
        [
          { text: '✅ Подтвердить заявку', callback_data: 'bk_submit' },
          { text: '❌ Отменить', callback_data: 'booking_cancel' }
        ]
      ]
    }
  });
}

async function bookingSubmit(chatId, data) {
  try {
    const orderNum = generateOrderNumber();
    await run(
      `INSERT INTO orders (order_number, client_name, client_phone, client_email, client_telegram,
        client_chat_id, model_id, event_type, event_date, event_duration, location, budget, status)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')`,
      [
        orderNum,
        data.client_name,
        data.client_phone,
        data.client_email || null,
        data.client_telegram || null,
        String(chatId),
        data.model_id || null,
        data.event_type,
        data.event_date,
        data.duration || 4,
        data.location || null,
        data.budget || null
      ]
    );
    const order = await get('SELECT * FROM orders WHERE order_number = ?', [orderNum]);
    await clearSession(chatId);

    await safeSend(chatId,
      `🎉 *Заявка оформлена\\!*\n\nНомер вашей заявки: *${esc(orderNum)}*\n\nМенеджер свяжется с вами в ближайшее время для подтверждения\\.\n\nСохраните номер заявки — по нему вы можете узнать статус в любое время\\.`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '📋 Мои заявки', callback_data: 'my_orders' }],
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
          ]
        }
      }
    );

    if (order) notifyNewOrder(order);
  } catch (e) {
    console.error('[Bot] bookingSubmit error:', e.message);
    await clearSession(chatId);
    return safeSend(chatId, '❌ Ошибка при оформлении заявки\\. Попробуйте позже или свяжитесь с нами\\.', { parse_mode: 'MarkdownV2' });
  }
}

// Handle text input during booking wizard
async function handleBookingInput(chatId, session, text) {
  const data = parseSessionData(session);

  switch (session.state) {
    case 'bk_name':
      if (text.length < 2 || text.length > 80) {
        return safeSend(chatId, '❌ Имя должно быть от 2 до 80 символов\\. Попробуйте ещё раз:', { parse_mode: 'MarkdownV2' });
      }
      data.client_name = text;
      return bookingAskPhone(chatId, data);

    case 'bk_phone':
      const phone = text.replace(/\s/g, '');
      if (!/^[\+\d\-\(\)]{7,20}$/.test(phone)) {
        return safeSend(chatId, '❌ Введите корректный номер телефона:', { parse_mode: 'MarkdownV2' });
      }
      data.client_phone = text;
      return bookingAskEmail(chatId, data);

    case 'bk_email':
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text)) {
        return safeSend(chatId, '❌ Введите корректный email или нажмите «Пропустить»:', { parse_mode: 'MarkdownV2' });
      }
      data.client_email = text;
      return bookingAskEventType(chatId, data);

    case 'bk_date':
      if (text.length < 3 || text.length > 50) {
        return safeSend(chatId, '❌ Введите дату мероприятия:', { parse_mode: 'MarkdownV2' });
      }
      data.event_date = text;
      return bookingAskModel(chatId, data);

    case 'bk_location':
      data.location = text;
      return bookingAskBudget(chatId, data);

    case 'bk_budget':
      data.budget = text;
      return bookingShowConfirm(chatId, data);

    default:
      return safeSend(chatId, '⚠️ Нажмите /cancel чтобы сбросить состояние, или /start чтобы начать заново\\.', { parse_mode: 'MarkdownV2' });
  }
}

// ─── Admin UI ─────────────────────────────────────────────────────────────────

async function showAdminMenu(chatId, firstName) {
  await clearSession(chatId);
  const name = firstName ? `, ${esc(firstName)}` : '';
  try {
    const newCount = (await get("SELECT COUNT(*) as n FROM orders WHERE status='new'")).n;
    const badge = newCount > 0 ? ` 🔴${newCount}` : '';
    const adminKeyboard = [
      [{ text: `📋 Заявки${badge}`, callback_data: 'admin_orders_all_0' }],
      [{ text: '💃 Модели', callback_data: 'admin_models_0' }],
      [{ text: '📊 Статистика', callback_data: 'admin_stats' }],
      [{ text: '🤖 Фид агентов', callback_data: 'agent_feed_0' }],
    ];
    if (SITE_URL.startsWith('https://')) {
      adminKeyboard.push([{ text: '🌐 Панель (Mini App)', web_app: { url: `${SITE_URL}/admin/` } }]);
    } else {
      adminKeyboard.push([{ text: '🌐 Открыть панель', url: `${SITE_URL}/admin/` }]);
    }
    return safeSend(chatId,
      `👑 *Панель администратора*${name ? `\nДобро пожаловать${name}` : ''}`,
      { parse_mode: 'MarkdownV2', reply_markup: { inline_keyboard: adminKeyboard } }
    );
  } catch (e) {
    console.error('[Bot] showAdminMenu error:', e.message);
  }
}

async function showAdminStats(chatId) {
  try {
    const [total, newO, reviewing, confirmed, inProgress, completed, cancelled, models, managers] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE status='new'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='reviewing'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='confirmed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='in_progress'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='completed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='cancelled'"),
      get('SELECT COUNT(*) as n FROM models WHERE available=1'),
      get('SELECT COUNT(*) as n FROM admins'),
    ]);

    const text =
      `📊 *Статистика Nevesty Models*\n\n` +
      `*Заявки:*\n` +
      `📋 Всего: ${total.n}\n` +
      `🆕 Новых: ${newO.n}\n` +
      `🔍 На рассмотрении: ${reviewing.n}\n` +
      `✅ Подтверждено: ${confirmed.n}\n` +
      `▶️ В работе: ${inProgress.n}\n` +
      `🏁 Завершено: ${completed.n}\n` +
      `❌ Отклонено: ${cancelled.n}\n\n` +
      `*Агентство:*\n` +
      `💃 Доступно моделей: ${models.n}\n` +
      `👤 Менеджеров: ${managers.n}`;

    return safeSend(chatId, text, {
      parse_mode: 'Markdown',
      reply_markup: {
        inline_keyboard: [
          [{ text: '📋 Все заявки', callback_data: 'admin_orders_all_0' }],
          [{ text: '← Главное меню', callback_data: 'admin_menu' }]
        ]
      }
    });
  } catch (e) {
    console.error('[Bot] showAdminStats error:', e.message);
  }
}

async function showAdminOrders(chatId, statusFilter = 'all', page = 0) {
  try {
    // Validate statusFilter against known values to prevent SQL injection
    const VALID_STATUSES = ['all', 'new', 'reviewing', 'confirmed', 'in_progress', 'completed', 'cancelled'];
    if (!VALID_STATUSES.includes(statusFilter)) statusFilter = 'all';

    const whereClause = statusFilter === 'all' ? '' : 'WHERE o.status = ?';
    const whereParams = statusFilter === 'all' ? [] : [statusFilter];
    const total = (await get(`SELECT COUNT(*) as n FROM orders o ${whereClause}`, whereParams)).n;

    const orders = await query(
      `SELECT o.*, m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id = m.id
       ${whereClause}
       ORDER BY o.created_at DESC LIMIT ? OFFSET ?`,
      [...whereParams, ORDERS_PER_PAGE, page * ORDERS_PER_PAGE]
    );

    if (!orders.length) {
      return safeSend(chatId,
        statusFilter === 'all' ? '📭 Заявок нет.' : `📭 Нет заявок со статусом "${STATUS_LABELS[statusFilter] || statusFilter}"`,
        {
          reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] }
        }
      );
    }

    const filterLabel = statusFilter === 'all' ? 'Все' : (STATUS_LABELS[statusFilter] || statusFilter);
    let text = `📋 *Заявки — ${filterLabel}* (стр. ${page + 1}/${Math.ceil(total / ORDERS_PER_PAGE)})\n\n`;

    const orderButtons = orders.map(o => {
      const statusIcon = STATUS_LABELS[o.status]?.split(' ')[0] || '';
      text += `${statusIcon} *${o.order_number}* — ${o.client_name}\n`;
      return [{ text: `${o.order_number} · ${o.client_name}`, callback_data: `admin_order_${o.id}` }];
    });

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `admin_orders_${statusFilter}_${page - 1}` });
    if ((page + 1) * ORDERS_PER_PAGE < total) nav.push({ text: '▶️', callback_data: `admin_orders_${statusFilter}_${page + 1}` });

    const filterButtons = [
      { text: '🆕 Новые', callback_data: 'admin_orders_new_0' },
      { text: '✅ Подтв.', callback_data: 'admin_orders_confirmed_0' },
      { text: '🏁 Готово', callback_data: 'admin_orders_completed_0' },
    ];

    return safeSend(chatId, text, {
      parse_mode: 'Markdown',
      reply_markup: {
        inline_keyboard: [
          ...orderButtons,
          ...(nav.length ? [nav] : []),
          filterButtons,
          [{ text: '← Главное меню', callback_data: 'admin_menu' }]
        ]
      }
    });
  } catch (e) {
    console.error('[Bot] showAdminOrders error:', e.message);
  }
}

async function showAdminOrder(chatId, orderId) {
  try {
    const o = await get(
      `SELECT o.*, m.name as model_name FROM orders o
       LEFT JOIN models m ON o.model_id = m.id WHERE o.id = ?`,
      [orderId]
    );
    if (!o) return safeSend(chatId, '❌ Заявка не найдена.');

    const lastMsg = await get(
      'SELECT * FROM messages WHERE order_id = ? ORDER BY created_at DESC LIMIT 1',
      [orderId]
    );

    let text = `📋 *Заявка ${o.order_number}*\n`;
    text += `Статус: ${STATUS_LABELS[o.status] || o.status}\n\n`;
    text += `👤 *Клиент:* ${o.client_name}\n`;
    text += `📞 ${o.client_phone}\n`;
    if (o.client_email) text += `📧 ${o.client_email}\n`;
    if (o.client_telegram) text += `💬 @${o.client_telegram.replace('@', '')}\n`;
    text += `\n🎭 *Мероприятие:* ${EVENT_TYPES[o.event_type] || o.event_type}\n`;
    if (o.event_date) text += `📅 ${o.event_date}\n`;
    if (o.event_duration) text += `⏱ ${o.event_duration} ч.\n`;
    if (o.location) text += `📍 ${o.location}\n`;
    if (o.model_name) text += `💃 Модель: ${o.model_name}\n`;
    if (o.budget) text += `💰 Бюджет: ${o.budget}\n`;
    if (o.comments) text += `\n💬 Комментарий:\n${o.comments}\n`;
    if (lastMsg) text += `\n📨 Последнее сообщение:\n_${lastMsg.content}_`;

    const actionRow = [];
    if (!['confirmed', 'completed', 'cancelled'].includes(o.status)) {
      actionRow.push({ text: '✅ Подтвердить', callback_data: `confirm_order_${orderId}` });
    }
    if (!['reviewing', 'completed', 'cancelled'].includes(o.status)) {
      actionRow.push({ text: '🔍 В работу', callback_data: `review_order_${orderId}` });
    }
    if (!['cancelled', 'completed'].includes(o.status)) {
      actionRow.push({ text: '❌ Отклонить', callback_data: `reject_order_${orderId}` });
    }

    const keyboard = [];
    if (actionRow.length) keyboard.push(actionRow);
    keyboard.push([
      { text: '💬 Написать клиенту', callback_data: `contact_order_${orderId}` },
      { text: '🔗 Открыть', url: `${SITE_URL}/admin/#orders/${orderId}` }
    ]);
    keyboard.push([{ text: '← Назад', callback_data: 'admin_orders_all_0' }]);

    return safeSend(chatId, text, {
      parse_mode: 'Markdown',
      reply_markup: { inline_keyboard: keyboard }
    });
  } catch (e) {
    console.error('[Bot] showAdminOrder error:', e.message);
  }
}

async function showAgentFeed(chatId, page = 0) {
  try {
    const total = (await get('SELECT COUNT(*) as n FROM agent_logs')).n;
    const logs = await query(
      'SELECT * FROM agent_logs ORDER BY created_at DESC LIMIT ? OFFSET ?',
      [AGENT_LOGS_PER_PAGE, page * AGENT_LOGS_PER_PAGE]
    );

    if (!logs.length) {
      return safeSend(chatId, '🤖 Фид агентов пуст.', {
        reply_markup: { inline_keyboard: [[{ text: '← Главное меню', callback_data: 'admin_menu' }]] }
      });
    }

    let text = `🤖 *Фид агентов* (стр. ${page + 1}/${Math.ceil(total / AGENT_LOGS_PER_PAGE)})\n\n`;
    for (const log of logs.reverse()) {
      const ts = new Date(log.created_at).toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'
      });
      const from = log.from_name || 'Claude';
      const msg = log.message.length > 120 ? log.message.slice(0, 120) + '…' : log.message;
      text += `[${ts}] *${from}*\n${msg}\n\n`;
    }

    const nav = [];
    if (page > 0) nav.push({ text: '◀️ Старше', callback_data: `agent_feed_${page - 1}` });
    if ((page + 1) * AGENT_LOGS_PER_PAGE < total) nav.push({ text: 'Новее ▶️', callback_data: `agent_feed_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'Markdown',
      reply_markup: {
        inline_keyboard: [
          ...(nav.length ? [nav] : []),
          [{ text: '← Главное меню', callback_data: 'admin_menu' }]
        ]
      }
    });
  } catch (e) {
    console.error('[Bot] showAgentFeed error:', e.message);
  }
}

async function showAdminModels(chatId, page = 0) {
  try {
    const total = (await get('SELECT COUNT(*) as n FROM models')).n;
    const models = await query(
      'SELECT * FROM models ORDER BY id DESC LIMIT ? OFFSET ?',
      [MODELS_PER_PAGE, page * MODELS_PER_PAGE]
    );

    let text = `💃 *Модели агентства* (стр. ${page + 1}/${Math.ceil(total / MODELS_PER_PAGE)})\n\n`;
    const buttons = models.map(m => {
      const avail = m.available ? '🟢' : '🔴';
      text += `${avail} *${m.name}* — ${m.height}см, ${m.age || '?'} лет\n`;
      return [{ text: `${avail} ${m.name}`, callback_data: `admin_model_${m.id}` }];
    });

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `admin_models_${page - 1}` });
    if ((page + 1) * MODELS_PER_PAGE < total) nav.push({ text: '▶️', callback_data: `admin_models_${page + 1}` });

    return safeSend(chatId, text, {
      parse_mode: 'Markdown',
      reply_markup: {
        inline_keyboard: [
          ...buttons,
          ...(nav.length ? [nav] : []),
          [{ text: '← Главное меню', callback_data: 'admin_menu' }]
        ]
      }
    });
  } catch (e) {
    console.error('[Bot] showAdminModels error:', e.message);
  }
}

async function showAdminModel(chatId, modelId) {
  try {
    const m = await get('SELECT * FROM models WHERE id = ?', [modelId]);
    if (!m) return safeSend(chatId, '❌ Модель не найдена.');

    const ordersCount = (await get('SELECT COUNT(*) as n FROM orders WHERE model_id = ?', [modelId])).n;

    let text = `💃 *${m.name}*\n\n`;
    text += `Возраст: ${m.age || '—'} лет\n`;
    text += `Рост: ${m.height || '—'} см\n`;
    text += `Категория: ${m.category}\n`;
    text += `Статус: ${m.available ? '🟢 Доступна' : '🔴 Недоступна'}\n`;
    text += `Заявок всего: ${ordersCount}\n`;
    if (m.bio) text += `\n${m.bio}`;

    const avail = m.available;
    return safeSend(chatId, text, {
      parse_mode: 'Markdown',
      reply_markup: {
        inline_keyboard: [
          [{ text: avail ? '🔴 Отметить недоступной' : '🟢 Отметить доступной', callback_data: `toggle_model_${m.id}` }],
          [{ text: '🔗 Открыть в панели', url: `${SITE_URL}/admin/#models/${m.id}` }],
          [{ text: '← К моделям', callback_data: 'admin_models_0' }]
        ]
      }
    });
  } catch (e) {
    console.error('[Bot] showAdminModel error:', e.message);
  }
}

// ─── Main bot init ────────────────────────────────────────────────────────────

function initBot(app) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token || token === 'your_bot_token_here') {
    console.warn('⚠️  TELEGRAM_BOT_TOKEN not set – bot disabled');
    return null;
  }

  if (WEBHOOK_URL) {
    bot = new TelegramBot(token, { webHook: false });
    const webhookPath = '/api/tg-webhook';
    const fullUrl = WEBHOOK_URL.replace(/\/$/, '') + webhookPath;
    bot.setWebHook(fullUrl, { secret_token: WEBHOOK_SECRET })
      .then(() => console.log(`🤖 Bot started (webhook: ${fullUrl})`))
      .catch(e => console.error('[Bot] setWebHook error:', e.message));

    if (app) {
      app.post(webhookPath, (req, res) => {
        if (req.headers['x-telegram-bot-api-secret-token'] !== WEBHOOK_SECRET) return res.sendStatus(403);
        bot.processUpdate(req.body);
        res.sendStatus(200);
      });
    }
  } else {
    bot = new TelegramBot(token, { polling: true });
    console.log('🤖 Bot started (polling)');
    bot.on('polling_error', err => {
      const code = err.code || (err.response && err.response.statusCode) || 'UNKNOWN';
      console.error(`[Bot] Polling error (${code}): ${err.message}`);
    });
  }

  // ─── Set menu button (Web App shortcut in chat header) ───────────────────
  if (SITE_URL.startsWith('https://')) {
    bot.setMyCommands([
      { command: 'start', description: '🏠 Главное меню' },
      { command: 'status', description: '📋 Статус заявки' },
      { command: 'help', description: '📖 Справка' },
      { command: 'cancel', description: '❌ Отменить действие' },
    ]).catch(() => {});
    // Global Web App menu button
    bot.callApi('setChatMenuButton', {
      menu_button: JSON.stringify({ type: 'web_app', text: '🌐 Сайт', web_app: { url: SITE_URL } })
    }).catch(() => {});
  }

  // ─── /start ───────────────────────────────────────────────────────────────
  bot.onText(/\/start(.*)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const firstName = msg.from.first_name;

    await setSession(chatId, 'idle', {});

    // Handle deep link (order number)
    const ref = match[1]?.trim();
    if (ref) {
      try {
        const order = await get('SELECT * FROM orders WHERE order_number = ?', [ref]);
        if (order) {
          if (order.client_chat_id && order.client_chat_id !== String(chatId)) {
            return safeSend(chatId, '❌ Эта заявка уже привязана к другому чату.');
          }
          await run('UPDATE orders SET client_chat_id = ? WHERE order_number = ?', [String(chatId), ref]);
          return safeSend(chatId,
            `✅ *Заявка ${esc(ref)} привязана к вашему чату\\!*\n\nВы будете получать уведомления об изменении статуса\\.`,
            {
              parse_mode: 'MarkdownV2',
              reply_markup: {
                inline_keyboard: [
                  [{ text: '📋 Статус заявки', callback_data: `client_order_${order.id}` }],
                  [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
                ]
              }
            }
          );
        }
      } catch (e) {
        console.error('[Bot] /start deep link error:', e.message);
      }
    }

    if (isAdmin(chatId)) return showAdminMenu(chatId, firstName);
    return showMainMenu(chatId, firstName);
  });

  // ─── /cancel ──────────────────────────────────────────────────────────────
  bot.onText(/\/cancel/, async (msg) => {
    const chatId = msg.chat.id;
    const session = await getSession(chatId);
    if (!session || session.state === 'idle') {
      return safeSend(chatId, 'ℹ️ Нет активного действия.');
    }
    await clearSession(chatId);
    return safeSend(chatId, '❌ Действие отменено\\. Нажмите /start для возврата в меню\\.', { parse_mode: 'MarkdownV2' });
  });

  // ─── /status ──────────────────────────────────────────────────────────────
  bot.onText(/\/status (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const orderNum = match[1].trim().toUpperCase();
    try {
      const o = await get(
        'SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id WHERE o.order_number = ?',
        [orderNum]
      );
      if (!o) return safeSend(chatId, `❌ Заявка *${esc(orderNum)}* не найдена.`, { parse_mode: 'Markdown' });

      let text = `📋 *Заявка ${o.order_number}*\n\n`;
      text += `Статус: ${STATUS_LABELS[o.status] || o.status}\n`;
      text += `Мероприятие: ${EVENT_TYPES[o.event_type] || o.event_type}\n`;
      if (o.event_date) text += `Дата: ${o.event_date}\n`;
      if (o.model_name) text += `Модель: ${o.model_name}\n`;

      return safeSend(chatId, text, { parse_mode: 'Markdown' });
    } catch (e) {
      console.error('[Bot] /status error:', e.message);
    }
  });

  // ─── /help ────────────────────────────────────────────────────────────────
  bot.onText(/\/help/, (msg) => {
    const chatId = msg.chat.id;
    if (isAdmin(chatId)) {
      return safeSend(chatId,
        `📖 *Справка для администратора*\n\n` +
        `Используйте кнопки меню для управления заявками и моделями\\.\n\n` +
        `*Прямой ответ клиенту:*\n` +
        `/msg НМ\\-XXXX\\-XXXX текст\n\n` +
        `*Команды:*\n` +
        `/start — открыть главное меню\n` +
        `/cancel — сбросить текущее действие`,
        { parse_mode: 'MarkdownV2' }
      );
    }
    return safeSend(chatId,
      `📖 *Справка Nevesty Models*\n\n` +
      `/start — главное меню\n` +
      `/status НОМЕР — статус вашей заявки\n` +
      `/cancel — отменить текущее действие\n\n` +
      `Если есть вопросы — просто напишите нам\\!`,
      {
        parse_mode: 'MarkdownV2',
        reply_markup: {
          inline_keyboard: [
            [{ text: '🏠 Главное меню', callback_data: 'main_menu' }]
          ]
        }
      }
    );
  });

  // ─── /msg (admin) ─────────────────────────────────────────────────────────
  bot.onText(/\/msg (\S+) (.+)/, async (msg, match) => {
    if (!isAdmin(msg.chat.id)) return;
    const chatId = msg.chat.id;
    const orderNum = match[1].trim().toUpperCase();
    const text = match[2].trim();
    try {
      const order = await get('SELECT * FROM orders WHERE order_number = ?', [orderNum]);
      if (!order) return safeSend(chatId, `❌ Заявка *${esc(orderNum)}* не найдена.`, { parse_mode: 'Markdown' });
      const admin = await get('SELECT username FROM admins WHERE telegram_id = ?', [String(chatId)]);
      const adminName = admin?.username || 'Менеджер';
      await run('INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)', [order.id, 'admin', adminName, text]);
      if (order.client_chat_id) {
        await sendMessageToClient(order.client_chat_id, order.order_number, text);
        return safeSend(chatId, `✅ Сообщение отправлено клиенту.`);
      }
      return safeSend(chatId, `⚠️ Сообщение сохранено, но клиент не подключил Telegram.`);
    } catch (e) {
      console.error('[Bot] /msg error:', e.message);
    }
  });

  // ─── Callback queries ─────────────────────────────────────────────────────
  bot.on('callback_query', async (q) => {
    const chatId = q.message.chat.id;
    const msgId = q.message.message_id;
    const data = q.data;

    try { await bot.answerCallbackQuery(q.id); } catch {}

    // ── Navigation
    if (data === 'main_menu') {
      if (isAdmin(chatId)) return showAdminMenu(chatId, q.from.first_name);
      return showMainMenu(chatId, q.from.first_name);
    }

    if (data === 'admin_menu') return showAdminMenu(chatId, q.from.first_name);
    if (data === 'admin_stats') return showAdminStats(chatId);
    if (data === 'contacts') return showContacts(chatId);
    if (data === 'my_orders') return showClientOrders(chatId);
    if (data === 'start_booking') return startBooking(chatId);

    // ── Catalog
    if (data.startsWith('catalog_')) {
      const page = parseInt(data.split('_')[1]) || 0;
      return showCatalog(chatId, page);
    }

    // ── Model detail (client) — matches catalog model_<id> buttons only
    if (/^model_\d+$/.test(data)) {
      const modelId = parseInt(data.replace('model_', ''));
      if (modelId) return showModel(chatId, modelId);
    }

    // ── Book specific model
    if (data.startsWith('book_model_')) {
      const modelId = parseInt(data.replace('book_model_', ''));
      return startBooking(chatId, modelId);
    }

    // ── Client order detail
    if (data.startsWith('client_order_')) {
      const orderId = parseInt(data.replace('client_order_', ''));
      return showClientOrder(chatId, orderId);
    }

    // ── Booking wizard callbacks
    if (data === 'booking_cancel' || data === 'bk_cancel') {
      await clearSession(chatId);
      if (isAdmin(chatId)) return showAdminMenu(chatId, q.from.first_name);
      return showMainMenu(chatId, q.from.first_name);
    }

    if (data === 'bk_skip_email') {
      const session = await getSession(chatId);
      const d = parseSessionData(session);
      return bookingAskEventType(chatId, d);
    }

    if (data === 'bk_skip_budget') {
      const session = await getSession(chatId);
      const d = parseSessionData(session);
      return bookingShowConfirm(chatId, d);
    }

    if (data.startsWith('bk_event_')) {
      const eventType = data.replace('bk_event_', '');
      if (!EVENT_TYPES[eventType]) return;
      const session = await getSession(chatId);
      const d = parseSessionData(session);
      d.event_type = eventType;
      return bookingAskDate(chatId, d);
    }

    if (data.startsWith('bk_model_')) {
      const session = await getSession(chatId);
      const d = parseSessionData(session);
      const modelKey = data.replace('bk_model_', '');
      if (modelKey === 'any') {
        d.model_id = null;
        d.model_name = null;
      } else {
        const modelId = parseInt(modelKey);
        try {
          const m = await get('SELECT id, name FROM models WHERE id = ?', [modelId]);
          if (m) { d.model_id = m.id; d.model_name = m.name; }
        } catch {}
      }
      return bookingAskDuration(chatId, d);
    }

    if (data.startsWith('bk_dur_')) {
      const hours = parseInt(data.replace('bk_dur_', ''));
      const session = await getSession(chatId);
      const d = parseSessionData(session);
      d.duration = hours;
      return bookingAskLocation(chatId, d);
    }

    if (data === 'bk_submit') {
      const session = await getSession(chatId);
      const d = parseSessionData(session);
      if (!d.client_name || !d.client_phone || !d.event_type || !d.event_date) {
        return safeSend(chatId, '❌ Данные заявки неполные. Начните заново с /start.');
      }
      // Store telegram username
      if (q.from.username) d.client_telegram = q.from.username;
      return bookingSubmit(chatId, d);
    }

    // ── Admin orders
    if (data.startsWith('admin_orders_')) {
      const parts = data.replace('admin_orders_', '').split('_');
      const page = parseInt(parts.pop()) || 0;
      const statusFilter = parts.join('_') || 'all';
      return showAdminOrders(chatId, statusFilter, page);
    }

    // ── Admin order detail
    if (data.startsWith('admin_order_')) {
      const orderId = parseInt(data.replace('admin_order_', ''));
      return showAdminOrder(chatId, orderId);
    }

    // ── Admin models
    if (data.startsWith('admin_models_')) {
      const page = parseInt(data.replace('admin_models_', '')) || 0;
      return showAdminModels(chatId, page);
    }

    // ── Admin model detail
    if (data.startsWith('admin_model_')) {
      const modelId = parseInt(data.replace('admin_model_', ''));
      return showAdminModel(chatId, modelId);
    }

    // ── Toggle model availability
    if (data.startsWith('toggle_model_')) {
      if (!isAdmin(chatId)) return;
      const modelId = parseInt(data.replace('toggle_model_', ''));
      try {
        const m = await get('SELECT available FROM models WHERE id = ?', [modelId]);
        if (m) {
          await run('UPDATE models SET available = ? WHERE id = ?', [m.available ? 0 : 1, modelId]);
          return showAdminModel(chatId, modelId);
        }
      } catch (e) { console.error('[Bot] toggle_model error:', e.message); }
    }

    // ── Agent feed
    if (data.startsWith('agent_feed_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('agent_feed_', '')) || 0;
      return showAgentFeed(chatId, page);
    }

    // ── Order actions (admin)
    if (!isAdmin(chatId)) return;

    if (data.startsWith('confirm_order_')) {
      const orderId = parseInt(data.replace('confirm_order_', ''));
      try {
        const order = await get('SELECT * FROM orders WHERE id = ?', [orderId]);
        if (!order) return safeSend(chatId, '❌ Заявка не найдена.');
        const r = await run(
          "UPDATE orders SET status='confirmed', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",
          [orderId]
        );
        if (r.changes === 0) return safeSend(chatId, `⚠️ Заявка ${order.order_number} уже обработана.`);
        if (order.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, 'confirmed');
        await safeEdit(chatId, msgId, `✅ Заявка *${order.order_number}* подтверждена.`, { parse_mode: 'Markdown' });
        return showAdminOrder(chatId, orderId);
      } catch (e) { console.error('[Bot] confirm_order error:', e.message); }
    }

    if (data.startsWith('review_order_')) {
      const orderId = parseInt(data.replace('review_order_', ''));
      try {
        const order = await get('SELECT * FROM orders WHERE id = ?', [orderId]);
        if (!order) return safeSend(chatId, '❌ Заявка не найдена.');
        const r = await run(
          "UPDATE orders SET status='reviewing', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",
          [orderId]
        );
        if (r.changes === 0) return safeSend(chatId, `⚠️ Заявка ${order.order_number} уже обработана.`);
        if (order.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, 'reviewing');
        return showAdminOrder(chatId, orderId);
      } catch (e) { console.error('[Bot] review_order error:', e.message); }
    }

    if (data.startsWith('reject_order_')) {
      const orderId = parseInt(data.replace('reject_order_', ''));
      try {
        const order = await get('SELECT * FROM orders WHERE id = ?', [orderId]);
        if (!order) return safeSend(chatId, '❌ Заявка не найдена.');
        const r = await run(
          "UPDATE orders SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('completed','cancelled')",
          [orderId]
        );
        if (r.changes === 0) return safeSend(chatId, `⚠️ Заявка ${order.order_number} уже обработана.`);
        if (order.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, 'cancelled');
        return showAdminOrder(chatId, orderId);
      } catch (e) { console.error('[Bot] reject_order error:', e.message); }
    }

    if (data.startsWith('contact_order_')) {
      const orderId = parseInt(data.replace('contact_order_', ''));
      try {
        const order = await get('SELECT * FROM orders WHERE id = ?', [orderId]);
        if (!order) return safeSend(chatId, '❌ Заявка не найдена.');
        await setSession(chatId, 'replying', { order_id: orderId, order_number: order.order_number, client_name: order.client_name });
        return safeSend(chatId,
          `💬 Введите сообщение для клиента *${order.client_name}* (заявка ${order.order_number}):\n\n_(Отправьте /cancel для отмены)_`,
          { parse_mode: 'Markdown' }
        );
      } catch (e) { console.error('[Bot] contact_order error:', e.message); }
    }

    // Legacy status_ref support
    if (data.startsWith('status_ref_')) {
      const orderRef = data.replace('status_ref_', '');
      try {
        const o = await get(
          'SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id WHERE o.order_number = ?',
          [orderRef]
        );
        if (!o) return safeSend(chatId, `❌ Заявка *${esc(orderRef)}* не найдена.`, { parse_mode: 'Markdown' });
        const text = `📋 *Заявка ${o.order_number}*\nСтатус: ${STATUS_LABELS[o.status] || o.status}\nМероприятие: ${EVENT_TYPES[o.event_type] || o.event_type}`;
        return safeSend(chatId, text, { parse_mode: 'Markdown' });
      } catch {}
    }
  });

  // ─── Text messages ────────────────────────────────────────────────────────
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId = msg.chat.id;
    const session = await getSession(chatId);

    // ── Admin replying to client
    if (isAdmin(chatId) && session?.state === 'replying') {
      const d = parseSessionData(session);
      const orderId = d.order_id;
      if (!orderId) {
        await clearSession(chatId);
        return safeSend(chatId, '❌ Заявка не найдена. Состояние сброшено.');
      }
      try {
        const order = await get('SELECT * FROM orders WHERE id = ?', [orderId]);
        if (!order) {
          await clearSession(chatId);
          return safeSend(chatId, '❌ Заявка не найдена. Состояние сброшено.');
        }
        const admin = await get('SELECT username FROM admins WHERE telegram_id = ?', [String(chatId)]);
        await run('INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)', [orderId, 'admin', admin?.username || 'Менеджер', msg.text]);
        if (order.client_chat_id) {
          await sendMessageToClient(order.client_chat_id, order.order_number, msg.text);
          await clearSession(chatId);
          return safeSend(chatId, `✅ Сообщение отправлено клиенту ${order.client_name}.`, {
            reply_markup: { inline_keyboard: [[{ text: '← К заявке', callback_data: `admin_order_${orderId}` }]] }
          });
        }
        await clearSession(chatId);
        return safeSend(chatId, `⚠️ Сообщение сохранено, но клиент не подключил Telegram-уведомления.`);
      } catch (e) {
        console.error('[Bot] admin reply error:', e.message);
      }
      return;
    }

    // ── Booking wizard step
    const bookingTextStates = ['bk_name', 'bk_phone', 'bk_email', 'bk_date', 'bk_location', 'bk_budget'];
    const bookingButtonStates = ['bk_event', 'bk_model', 'bk_duration', 'bk_confirm'];
    if (!isAdmin(chatId) && session && bookingTextStates.includes(session.state)) {
      return handleBookingInput(chatId, session, msg.text.trim());
    }
    // If user types text during a button-only step, remind them to use the buttons
    if (!isAdmin(chatId) && session && bookingButtonStates.includes(session.state)) {
      return safeSend(chatId, '⚠️ Пожалуйста, используйте кнопки ниже для выбора варианта\\. Чтобы отменить — нажмите /cancel\\.', { parse_mode: 'MarkdownV2' });
    }

    // ── Client message → forward to all admins
    if (!isAdmin(chatId)) {
      const clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      const username = msg.from.username ? `@${msg.from.username}` : 'нет username';

      let order = null;
      try {
        order = await get('SELECT * FROM orders WHERE client_chat_id = ? ORDER BY created_at DESC LIMIT 1', [String(chatId)]);
      } catch {}

      if (order) {
        try {
          await run('INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)', [order.id, 'client', clientName, msg.text]);
        } catch {}
      }

      const adminIds = await getAdminChatIds();
      const header = order
        ? `📩 *Сообщение от клиента*\nЗаявка: *${order.order_number}*\nКлиент: ${clientName} (${username})\n\n`
        : `📩 *Новое сообщение*\n${clientName} (${username})\n\n`;

      await Promise.allSettled(adminIds.map(id => safeSend(id, header + msg.text, {
        parse_mode: 'Markdown',
        reply_markup: order ? {
          inline_keyboard: [[
            { text: '💬 Ответить', callback_data: `contact_order_${order.id}` },
            { text: '📋 Заявка', callback_data: `admin_order_${order.id}` }
          ]]
        } : undefined
      })));

      return safeSend(chatId, '✅ Ваше сообщение передано менеджеру\\. Мы ответим в ближайшее время\\!', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
      });
    }
  });

  return { notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient, instance: bot };
}

// ─── Notification functions ───────────────────────────────────────────────────

async function notifyAdmin(text, opts = {}) {
  if (!bot) return;
  const adminIds = await getAdminChatIds();
  await Promise.allSettled(adminIds.map(id => safeSend(id, text, { parse_mode: 'Markdown', ...opts })));
}

async function notifyNewOrder(order) {
  if (!bot) return;
  // Guard: order must have a valid id for callback buttons to work
  if (!order || !order.id) {
    console.error('[Bot] notifyNewOrder called with invalid order:', order);
    return;
  }
  let modelInfo = null;
  if (order.model_id) {
    try { modelInfo = await get('SELECT name FROM models WHERE id = ?', [order.model_id]); } catch {}
  }

  const text =
    `🆕 *Новая заявка!*\n\n` +
    `📋 Номер: *${order.order_number}*\n` +
    `👤 Клиент: ${order.client_name}\n` +
    `📞 Телефон: ${order.client_phone}\n` +
    (order.client_email ? `📧 Email: ${order.client_email}\n` : '') +
    (order.client_telegram ? `💬 Telegram: @${String(order.client_telegram).replace('@', '')}\n` : '') +
    `\n🎭 Мероприятие: ${EVENT_TYPES[order.event_type] || order.event_type}\n` +
    (order.event_date ? `📅 Дата: ${order.event_date}\n` : '') +
    (order.location ? `📍 Место: ${order.location}\n` : '') +
    (order.budget ? `💰 Бюджет: ${order.budget}\n` : '') +
    (modelInfo ? `💃 Модель: ${modelInfo.name}\n` : '') +
    (order.comments ? `\n💬 Комментарий:\n${order.comments}` : '');

  const adminIds = await getAdminChatIds();
  await Promise.allSettled(adminIds.map(id => safeSend(id, text, {
    parse_mode: 'Markdown',
    reply_markup: {
      inline_keyboard: [
        [
          { text: '✅ Подтвердить', callback_data: `confirm_order_${order.id}` },
          { text: '🔍 В работу', callback_data: `review_order_${order.id}` },
          { text: '❌ Отклонить', callback_data: `reject_order_${order.id}` }
        ],
        [
          { text: '💬 Написать клиенту', callback_data: `contact_order_${order.id}` },
          { text: '📋 Открыть заявку', callback_data: `admin_order_${order.id}` }
        ]
      ]
    }
  })));
}

async function notifyStatusChange(clientChatId, orderNumber, newStatus) {
  if (!bot || !clientChatId) return;
  const msgs = {
    confirmed: `✅ *Ваша заявка ${orderNumber} подтверждена!*\n\nМенеджер свяжется с вами для уточнения деталей.`,
    reviewing: `🔍 *Заявка ${orderNumber} на рассмотрении.*\n\nМы изучаем ваш запрос и скоро дадим ответ.`,
    in_progress: `▶️ *Заявка ${orderNumber} в процессе выполнения.*`,
    completed: `🏁 *Заявка ${orderNumber} завершена!*\n\nСпасибо, что выбрали Nevesty Models! 💎`,
    cancelled: `❌ *Заявка ${orderNumber} отклонена.*\n\nЕсли есть вопросы — свяжитесь с нами.`
  };
  const text = msgs[newStatus];
  if (text) await safeSend(clientChatId, text, { parse_mode: 'Markdown' });
}

async function sendMessageToClient(clientChatId, orderNumber, text) {
  if (!bot || !clientChatId) return;
  await safeSend(clientChatId, `💬 *Сообщение от менеджера* (${orderNumber}):\n\n${text}`, { parse_mode: 'Markdown' });
}

module.exports = { initBot, notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient };
