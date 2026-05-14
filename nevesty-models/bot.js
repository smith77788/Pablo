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

async function getSession(chatId) {
  try { return await get('SELECT * FROM telegram_sessions WHERE chat_id=?', [String(chatId)]); }
  catch { return null; }
}

async function setSession(chatId, state, data = {}) {
  try {
    await run(
      `INSERT OR REPLACE INTO telegram_sessions (chat_id,state,data,updated_at)
       VALUES (?,?,?,CURRENT_TIMESTAMP)`,
      [String(chatId), state, JSON.stringify(data)]
    );
  } catch (e) { console.error('[Bot] setSession:', e.message); }
}

async function clearSession(chatId) { await setSession(chatId, 'idle', {}); }

function sessionData(session) {
  try { return JSON.parse(session?.data || '{}'); } catch { return {}; }
}

// ─── Keyboards ────────────────────────────────────────────────────────────────

const KB_MAIN_CLIENT = {
  inline_keyboard: [
    [{ text: '💃 Каталог моделей',      callback_data: 'cat_cat__0' }],
    [{ text: '📝 Оформить заявку',      callback_data: 'bk_start'   }],
    [{ text: '📋 Мои заявки',           callback_data: 'my_orders'  }],
    [{ text: '🔍 Проверить статус',     callback_data: 'check_status' }],
    [{ text: '📞 Контакты',             callback_data: 'contacts'   }],
  ]
};

const KB_MAIN_ADMIN = (badge) => ({
  inline_keyboard: [
    [{ text: `📋 Заявки${badge}`,       callback_data: 'adm_orders__0' }],
    [{ text: '💃 Модели',               callback_data: 'adm_models_0'  }],
    [{ text: '📊 Статистика',           callback_data: 'adm_stats'     }],
    [{ text: '🤖 Фид агентов',          callback_data: 'agent_feed_0'  }],
  ]
});

// ─── Client screens ───────────────────────────────────────────────────────────

async function showMainMenu(chatId, name) {
  await clearSession(chatId);
  return safeSend(chatId,
    `💎 *Nevesty Models*\n\nДобро пожаловать${name ? ', ' + esc(name) : ''}\\!\n\nВыберите действие:`,
    { parse_mode: 'MarkdownV2', reply_markup: KB_MAIN_CLIENT }
  );
}

async function showAdminMenu(chatId, name) {
  await clearSession(chatId);
  try {
    const n = (await get("SELECT COUNT(*) as n FROM orders WHERE status='new'")).n;
    const badge = n > 0 ? ` 🔴${n}` : '';
    return safeSend(chatId,
      `👑 *Панель администратора*${name ? `\n${esc(name)}` : ''}`,
      { parse_mode: 'MarkdownV2', reply_markup: KB_MAIN_ADMIN(badge) }
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
    if (m.instagram)                       lines.push(`Instagram: *${esc(m.instagram)}*`);

    const bio  = m.bio  ? `\n\n${esc(m.bio)}`  : '';
    const avail = m.available ? '🟢 Доступна для заказа' : '🔴 Временно недоступна';
    const caption = `💃 *${esc(m.name)}*\n${lines.join(' \\| ')}${bio}\n\n${avail}`;

    const keyboard = {
      inline_keyboard: [
        m.available ? [{ text: '📝 Заказать эту модель', callback_data: `bk_model_${m.id}` }] : [],
        [{ text: '← Каталог', callback_data: 'cat_cat__0' }, { text: '🏠 Меню', callback_data: 'main_menu' }],
      ].filter(r => r.length)
    };

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
  const phone = process.env.AGENCY_PHONE || '+7 (800) 555-00-00';
  const email = process.env.AGENCY_EMAIL || 'info@nevesty-models.ru';
  return safeSend(chatId,
    `📞 *Контакты Nevesty Models*\n\nТелефон: ${esc(phone)}\nEmail: ${esc(email)}\nСайт: ${esc(SITE_URL)}\n\nПн\\-Вс: 09:00 — 21:00`,
    {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [[{ text: '🏠 Главное меню', callback_data: 'main_menu' }]] }
    }
  );
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

// STEP 1 — model selection
async function bkStep1(chatId, data = {}) {
  await setSession(chatId, 'bk_s1', data);
  try {
    const models = await query('SELECT id,name,height,hair_color FROM models WHERE available=1 ORDER BY id LIMIT 12');
    const btns = models.map(m => [{
      text: `${m.name}  ·  ${m.height}см  ·  ${m.hair_color||''}`,
      callback_data: `bk_pick_${m.id}`
    }]);
    const preNote = data.model_name ? `✅ Выбрана: *${esc(data.model_name)}*\n\n` : '';
    return safeSend(chatId,
      stepHeader(1,'Выберите модель') + preNote + 'Выберите из списка или нажмите «Менеджер подберёт»:',
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

async function showAdminOrders(chatId, statusFilter, page) {
  try {
    const safe = VALID_STATUSES.includes(statusFilter) ? statusFilter : null;
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

    let text = `📋 *${o.order_number}*\nСтатус: ${STATUS_LABELS[o.status]||o.status}\n\n`;
    text += `👤 ${o.client_name}\n📞 ${o.client_phone}\n`;
    if (o.client_email)    text += `📧 ${o.client_email}\n`;
    if (o.client_telegram) text += `💬 @${o.client_telegram.replace('@','')}\n`;
    text += `\n🎭 ${EVENT_TYPES[o.event_type]||o.event_type}\n`;
    if (o.event_date)      text += `📅 ${o.event_date}\n`;
    if (o.event_duration)  text += `⏱ ${o.event_duration} ч.\n`;
    if (o.location)        text += `📍 ${o.location}\n`;
    if (o.model_name)      text += `💃 ${o.model_name}\n`;
    if (o.budget)          text += `💰 ${o.budget}\n`;
    if (o.comments)        text += `💬 ${o.comments}\n`;
    if (msgs.length) {
      text += `\n📨 Последние сообщения:\n`;
      msgs.reverse().forEach(m => {
        const who = m.sender_type==='admin' ? '👤' : '🙋';
        text += `${who} ${m.content}\n`;
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

    return safeSend(chatId, text, { parse_mode: 'Markdown', reply_markup: { inline_keyboard: keyboard } });
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
    return safeSend(chatId,
      `📊 *Статистика Nevesty Models*\n\n` +
      `Всего заявок: *${total.n}*\n` +
      `🆕 Новых: *${newO.n}*\n` +
      `🔍 На рассмотрении: *${rev.n}*\n` +
      `✅ Подтверждено: *${conf.n}*\n` +
      `▶️ В работе: *${ip.n}*\n` +
      `🏁 Завершено: *${done.n}*\n` +
      `❌ Отклонено: *${canc.n}*\n\n` +
      `💃 Доступно моделей: *${models.n}*`,
      {
        parse_mode: 'Markdown',
        reply_markup: { inline_keyboard: [
          [{ text: '📋 Все заявки', callback_data: 'adm_orders__0' }],
          [{ text: '← Меню',        callback_data: 'admin_menu'    }],
        ]}
      }
    );
  } catch (e) { console.error('[Bot] showAdminStats:', e.message); }
}

async function showAdminModels(chatId, page) {
  try {
    const all = await query('SELECT * FROM models ORDER BY id DESC');
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
    let text = `💃 *${m.name}*\n\n`;
    text += `Рост: ${m.height}см, Возраст: ${m.age||'—'} лет\n`;
    text += `Категория: ${m.category}\nЗаявок всего: ${cnt}\n`;
    text += `Статус: ${m.available ? '🟢 Доступна' : '🔴 Недоступна'}\n`;
    if (m.bio) text += `\n${m.bio}`;
    return safeSend(chatId, text, {
      parse_mode: 'Markdown',
      reply_markup: { inline_keyboard: [
        [{ text: m.available ? '🔴 Отметить недоступной' : '🟢 Отметить доступной', callback_data: `adm_toggle_${m.id}` }],
        [{ text: '← К моделям', callback_data: 'adm_models_0' }],
      ]}
    });
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

  // ── /msg (admin direct reply) ──────────────────────────────────────────────
  bot.onText(/\/msg (\S+) (.+)/, async (msg, match) => {
    if (!isAdmin(msg.chat.id)) return;
    const chatId   = msg.chat.id;
    const orderNum = match[1].trim().toUpperCase();
    const text     = match[2].trim();
    const order    = await get('SELECT * FROM orders WHERE order_number=?', [orderNum]).catch(()=>null);
    if (!order) return safeSend(chatId, `❌ Заявка *${esc(orderNum)}* не найдена.`, { parse_mode: 'Markdown' });
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
    if (data === 'admin_menu') return showAdminMenu(chatId, q.from.first_name);
    if (data === 'contacts')   return showContacts(chatId);
    if (data === 'my_orders')  return showMyOrders(chatId);
    if (data === 'check_status') return showStatusInput(chatId);
    if (data === 'adm_stats')  return showAdminStats(chatId);

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

    // ── Agent feed
    if (data.startsWith('agent_feed_')) {
      if (!isAdmin(chatId)) return;
      const page = parseInt(data.replace('agent_feed_','')) || 0;
      return showAgentFeed(chatId, page);
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
        parse_mode: 'Markdown',
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
  await Promise.allSettled(ids.map(id => safeSend(id, text, { parse_mode: 'Markdown', ...opts })));
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
    parse_mode: 'Markdown',
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
  if (text) await safeSend(clientChatId, text, { parse_mode: 'Markdown' });
}

async function sendMessageToClient(clientChatId, orderNumber, text) {
  if (!bot || !clientChatId) return;
  await safeSend(clientChatId, `💬 *Сообщение от менеджера* \\(${esc(orderNumber)}\\):\n\n${esc(text)}`, { parse_mode: 'MarkdownV2' });
}

module.exports = { initBot, notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient };
