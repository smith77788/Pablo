require('dotenv').config();
const crypto = require('crypto');
const TelegramBot = require('node-telegram-bot-api');
const { query, run, get } = require('./database');

const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
const SITE_URL = process.env.SITE_URL || 'http://localhost:3000';
const WEBHOOK_URL = process.env.WEBHOOK_URL || '';
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || crypto.randomBytes(32).toString('hex');

function mdEscape(s) {
  if (s == null) return '';
  return String(s).replace(/([_*\[\]()~`>#+\-=|{}.!\\])/g, '\\$1');
}

const STATUS_LABELS = {
  new: '🆕 Новая',
  reviewing: '🔍 На рассмотрении',
  confirmed: '✅ Подтверждена',
  in_progress: '▶️ В процессе',
  completed: '🏁 Завершена',
  cancelled: '❌ Отменена'
};

const EVENT_TYPES = {
  fashion_show: 'Показ мод',
  photo_shoot: 'Фотосессия',
  event: 'Корпоратив / Мероприятие',
  commercial: 'Коммерческая съёмка',
  runway: 'Подиум',
  other: 'Другое'
};

let bot = null;

function isAdmin(chatId) {
  return ADMIN_IDS.includes(String(chatId));
}

async function getAdminChatIds() {
  try {
    const admins = await query("SELECT telegram_id FROM admins WHERE telegram_id IS NOT NULL AND telegram_id != ''");
    const dbIds = admins.map(a => a.telegram_id).filter(Boolean);
    return [...new Set([...ADMIN_IDS, ...dbIds])];
  } catch (e) {
    console.error('[Bot] getAdminChatIds error:', e.message);
    return [...ADMIN_IDS];
  }
}

// Safe send — never throws even if user blocked the bot
async function safeSend(chatId, text, opts = {}) {
  try {
    return await bot.sendMessage(chatId, text, opts);
  } catch (e) {
    if (opts.parse_mode && /parse entities|can't parse/i.test(e.message)) {
      try {
        const { parse_mode, ...rest } = opts;
        return await bot.sendMessage(chatId, text, rest);
      } catch (e2) {
        console.warn(`[Bot] sendMessage retry to ${chatId} failed: ${e2.message}`);
        return null;
      }
    }
    console.warn(`[Bot] sendMessage to ${chatId} failed: ${e.message}`);
    return null;
  }
}

// Safe editMessageText — never throws
async function safeEdit(text, opts) {
  try {
    return await bot.editMessageText(text, opts);
  } catch (e) {
    console.warn(`[Bot] editMessageText failed: ${e.message}`);
    return null;
  }
}

function initBot(app) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token || token === 'your_bot_token_here') {
    console.warn('⚠️  TELEGRAM_BOT_TOKEN not set – bot disabled');
    return null;
  }

  // ─── Init bot instance ───────────────────────────────────────────────────
  if (WEBHOOK_URL) {
    // Webhook mode — no polling, Express handles updates
    bot = new TelegramBot(token, { webHook: false });
    const webhookPath = '/api/tg-webhook';
    const fullWebhookUrl = WEBHOOK_URL.replace(/\/$/, '') + webhookPath;

    bot.setWebHook(fullWebhookUrl, { secret_token: WEBHOOK_SECRET })
      .then(() => console.log(`🤖 Telegram bot started (webhook: ${fullWebhookUrl})`))
      .catch(e => console.error('[Bot] setWebHook error:', e.message));

    if (app) {
      app.post(webhookPath, (req, res) => {
        const sig = req.headers['x-telegram-bot-api-secret-token'];
        if (sig !== WEBHOOK_SECRET) return res.sendStatus(403);
        bot.processUpdate(req.body);
        res.sendStatus(200);
      });
    }
  } else {
    // Polling mode with error handling — never crashes on bad token
    bot = new TelegramBot(token, { polling: true });
    console.log('🤖 Telegram bot started (polling)');

    bot.on('polling_error', (err) => {
      // Log only — do NOT re-throw or crash the process
      const code = err.code || (err.response && err.response.statusCode) || 'UNKNOWN';
      console.error(`[Bot] Polling error (${code}): ${err.message}`);
    });
  }

  // ─── /start ───────────────────────────────────────────────────────────
  bot.onText(/\/start(.*)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const firstName = msg.from.first_name || 'Гость';

    // Save/update session
    try {
      await run(
        `INSERT OR REPLACE INTO telegram_sessions (chat_id, state, data, updated_at)
         VALUES (?, 'idle', '{}', CURRENT_TIMESTAMP)`,
        [String(chatId)]
      );
    } catch (e) {
      console.error('[Bot] session upsert error:', e.message);
    }

    if (isAdmin(chatId)) {
      return safeSend(chatId,
        `👑 Добро пожаловать в панель управления, ${firstName}!\n\n` +
        `*Доступные команды:*\n` +
        `/orders — все заявки\n` +
        `/new_orders — новые заявки\n` +
        `/models — список моделей\n` +
        `/stats — статистика\n` +
        `/help — справка`,
        { parse_mode: 'Markdown' }
      );
    }

    // Check for deep link order number
    const orderRef = match[1]?.trim();
    if (orderRef) {
      try {
        const order = await get('SELECT * FROM orders WHERE order_number = ?', [orderRef]);
        if (order) {
          if (order.client_chat_id && order.client_chat_id !== String(chatId)) {
            return safeSend(chatId, '❌ Эта заявка уже привязана к другому чату.');
          }
          await run('UPDATE orders SET client_chat_id = ? WHERE order_number = ?', [String(chatId), orderRef]);
          return safeSend(chatId,
            `✅ *Ваша заявка ${mdEscape(orderRef)} привязана к этому чату.*\n\n` +
            `Теперь вы будете получать уведомления о статусе заявки.\n` +
            `Вы можете писать сообщения прямо сюда — менеджер ответит вам.`,
            {
              parse_mode: 'Markdown',
              reply_markup: {
                inline_keyboard: [
                  [{ text: '📊 Статус заявки', callback_data: `status_ref_${orderRef}` }]
                ]
              }
            }
          );
        }
      } catch (e) {
        console.error('[Bot] /start deep link error:', e.message);
      }
    }

    return safeSend(chatId,
      `💎 *Добро пожаловать в Nevesty Models!*\n\n` +
      `${firstName}, мы рады видеть вас!\n\n` +
      `🌐 Оформить заявку: ${SITE_URL}/booking.html\n` +
      `📋 Каталог моделей: ${SITE_URL}/catalog.html\n\n` +
      `Для проверки статуса заявки напишите: /status НМ-XXXX-XXXX\n\n` +
      `Если у вас есть вопросы — просто напишите нам!`,
      {
        parse_mode: 'Markdown',
        reply_markup: {
          inline_keyboard: [
            [{ text: '📋 Каталог моделей', url: `${SITE_URL}/catalog.html` }],
            [{ text: '✍️ Оформить заявку', url: `${SITE_URL}/booking.html` }],
            [{ text: '📞 Связаться с нами', callback_data: 'contact_agency' }]
          ]
        }
      }
    );
  });

  // ─── /cancel ──────────────────────────────────────────────────────────
  bot.onText(/\/cancel/, async (msg) => {
    const chatId = msg.chat.id;
    try {
      const session = await get('SELECT state FROM telegram_sessions WHERE chat_id = ?', [String(chatId)]);
      if (!session || session.state === 'idle') {
        return safeSend(chatId, 'ℹ️ Нет активного действия для отмены.');
      }
      await run(
        "UPDATE telegram_sessions SET state='idle', order_id=NULL WHERE chat_id=?",
        [String(chatId)]
      );
      return safeSend(chatId, '❌ Действие отменено.');
    } catch (e) {
      console.error('[Bot] /cancel error:', e.message);
    }
  });

  // ─── /status ──────────────────────────────────────────────────────────
  bot.onText(/\/status (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const orderNumber = match[1].trim().toUpperCase();
    try {
      const order = await get(
        `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id WHERE o.order_number = ?`,
        [orderNumber]
      );
      if (!order) {
        return safeSend(chatId, `❌ Заявка *${mdEscape(orderNumber)}* не найдена.`, { parse_mode: 'Markdown' });
      }
      const statusLabel = STATUS_LABELS[order.status] || order.status;
      return safeSend(chatId,
        `📋 *Заявка ${mdEscape(order.order_number)}*\n\n` +
        `Клиент: ${mdEscape(order.client_name)}\n` +
        `Статус: ${statusLabel}\n` +
        `Мероприятие: ${EVENT_TYPES[order.event_type] || order.event_type}\n` +
        (order.event_date ? `Дата: ${mdEscape(order.event_date)}\n` : '') +
        (order.model_name ? `Модель: ${mdEscape(order.model_name)}\n` : ''),
        { parse_mode: 'Markdown' }
      );
    } catch (e) {
      console.error('[Bot] /status error:', e.message);
    }
  });

  // ─── /orders (admin) ──────────────────────────────────────────────────
  bot.onText(/\/orders/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    try {
      const orders = await query(
        `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id
         ORDER BY o.created_at DESC LIMIT 10`
      );
      if (!orders.length) return safeSend(msg.chat.id, '📭 Нет заявок.');
      let text = `📋 *Последние заявки:*\n\n`;
      for (const o of orders) {
        text += `${STATUS_LABELS[o.status] || o.status} *${mdEscape(o.order_number)}*\n`;
        text += `  ${mdEscape(o.client_name)} · ${EVENT_TYPES[o.event_type] || o.event_type}\n\n`;
      }
      return safeSend(msg.chat.id, text, {
        parse_mode: 'Markdown',
        reply_markup: {
          inline_keyboard: [[{ text: '🔗 Открыть панель управления', url: `${SITE_URL}/admin/` }]]
        }
      });
    } catch (e) {
      console.error('[Bot] /orders error:', e.message);
    }
  });

  // ─── /new_orders (admin) ──────────────────────────────────────────────
  bot.onText(/\/new_orders/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    try {
      const orders = await query(
        `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id
         WHERE o.status = 'new' ORDER BY o.created_at DESC`
      );
      if (!orders.length) return safeSend(msg.chat.id, '✅ Новых заявок нет.');
      let text = `🆕 *Новые заявки (${orders.length}):*\n\n`;
      for (const o of orders) {
        text += `*${mdEscape(o.order_number)}* — ${mdEscape(o.client_name)}\n`;
        text += `📞 ${mdEscape(o.client_phone)} · ${EVENT_TYPES[o.event_type] || o.event_type}\n\n`;
      }
      return safeSend(msg.chat.id, text, { parse_mode: 'Markdown' });
    } catch (e) {
      console.error('[Bot] /new_orders error:', e.message);
    }
  });

  // ─── /models (admin) ──────────────────────────────────────────────────
  bot.onText(/\/models/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    try {
      const models = await query('SELECT name, height, category, available FROM models ORDER BY id DESC');
      let text = `💃 *Модели агентства (${models.length}):*\n\n`;
      for (const m of models) {
        text += `${m.available ? '🟢' : '🔴'} *${mdEscape(m.name)}* — ${mdEscape(m.height)}см · ${mdEscape(m.category)}\n`;
      }
      return safeSend(msg.chat.id, text, { parse_mode: 'Markdown' });
    } catch (e) {
      console.error('[Bot] /models error:', e.message);
    }
  });

  // ─── /stats (admin) ───────────────────────────────────────────────────
  bot.onText(/\/stats/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    try {
      const [total, newO, confirmed, completed, models] = await Promise.all([
        get('SELECT COUNT(*) as n FROM orders'),
        get("SELECT COUNT(*) as n FROM orders WHERE status = 'new'"),
        get("SELECT COUNT(*) as n FROM orders WHERE status = 'confirmed'"),
        get("SELECT COUNT(*) as n FROM orders WHERE status = 'completed'"),
        get('SELECT COUNT(*) as n FROM models WHERE available = 1'),
      ]);
      return safeSend(msg.chat.id,
        `📊 *Статистика Nevesty Models*\n\n` +
        `📋 Всего заявок: *${total.n}*\n` +
        `🆕 Новых: *${newO.n}*\n` +
        `✅ Подтверждено: *${confirmed.n}*\n` +
        `🏁 Завершено: *${completed.n}*\n` +
        `💃 Доступно моделей: *${models.n}*`,
        { parse_mode: 'Markdown' }
      );
    } catch (e) {
      console.error('[Bot] /stats error:', e.message);
    }
  });

  // ─── /help ────────────────────────────────────────────────────────────
  bot.onText(/\/help/, (msg) => {
    const chatId = msg.chat.id;
    if (isAdmin(chatId)) {
      return safeSend(chatId,
        `📖 *Справка для администратора:*\n\n` +
        `/orders — последние 10 заявок\n` +
        `/new_orders — только новые заявки\n` +
        `/models — список моделей\n` +
        `/stats — статистика агентства\n` +
        `/feed — лента последних сообщений\n\n` +
        `*Ответ клиенту:*\n` +
        `/msg НМ-XXXX текст — написать клиенту напрямую\n` +
        `Или нажмите «💬 Написать клиенту» под уведомлением.\n\n` +
        `/cancel — отменить текущее действие`,
        { parse_mode: 'Markdown' }
      );
    }
    return safeSend(chatId,
      `📖 *Справка Nevesty Models:*\n\n` +
      `📋 /status НОМЕР — узнать статус вашей заявки\n` +
      `❌ /cancel — отменить текущее действие\n\n` +
      `*Как отслеживать заявку:*\n` +
      `После оформления заявки на сайте вы получите номер — отправьте его командой /status.\n\n` +
      `*Оформить заявку:*\n` +
      `${SITE_URL}/booking.html\n\n` +
      `Если есть вопросы — просто напишите, менеджер ответит!`,
      {
        parse_mode: 'Markdown',
        reply_markup: {
          inline_keyboard: [
            [{ text: '✍️ Оформить заявку', url: `${SITE_URL}/booking.html` }],
            [{ text: '📋 Каталог моделей', url: `${SITE_URL}/catalog.html` }]
          ]
        }
      }
    );
  });

  // ─── /feed (admin) — live message feed ───────────────────────────────────
  bot.onText(/\/feed/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    try {
      const messages = await query(
        `SELECT m.content, m.sender_type, m.sender_name, m.created_at,
                o.order_number, o.client_name
         FROM messages m JOIN orders o ON m.order_id = o.id
         ORDER BY m.created_at DESC LIMIT 15`
      );
      if (!messages.length) return safeSend(msg.chat.id, '📭 Сообщений пока нет.');
      let text = `📡 *Лента сообщений (последние 15):*\n\n`;
      for (const m of messages.reverse()) {
        const who = m.sender_type === 'admin' ? `👤 ${mdEscape(m.sender_name)}` : `🙋 ${mdEscape(m.client_name)}`;
        const ts = new Date(m.created_at).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
        text += `[${ts}] *${mdEscape(m.order_number)}* — ${who}\n${mdEscape(m.content)}\n\n`;
      }
      return safeSend(msg.chat.id, text, { parse_mode: 'Markdown' });
    } catch (e) {
      console.error('[Bot] /feed error:', e.message);
    }
  });

  // ─── /msg ORDER_NUMBER text (admin) — send message to client ─────────────
  bot.onText(/\/msg (\S+) (.+)/, async (msg, match) => {
    if (!isAdmin(msg.chat.id)) return;
    const orderNum = match[1].trim().toUpperCase();
    const text = match[2].trim();
    try {
      const order = await get('SELECT * FROM orders WHERE order_number = ?', [orderNum]);
      if (!order) return safeSend(msg.chat.id, `❌ Заявка *${mdEscape(orderNum)}* не найдена.`, { parse_mode: 'Markdown' });
      const admin = await get('SELECT username FROM admins WHERE telegram_id = ?', [String(msg.chat.id)]);
      const adminName = admin?.username || 'Менеджер';
      await run(
        'INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)',
        [order.id, 'admin', adminName, text]
      );
      if (order.client_chat_id) {
        await safeSend(order.client_chat_id,
          `💬 *Сообщение от менеджера* (${mdEscape(order.order_number)}):\n\n${mdEscape(text)}`,
          { parse_mode: 'Markdown' }
        );
        return safeSend(msg.chat.id, `✅ Сообщение отправлено клиенту ${order.client_name}.`);
      } else {
        return safeSend(msg.chat.id, `⚠️ Сообщение сохранено, но клиент ещё не подключил Telegram-уведомления.`);
      }
    } catch (e) {
      console.error('[Bot] /msg error:', e.message);
      return safeSend(msg.chat.id, '❌ Ошибка отправки сообщения.');
    }
  });

  // ─── Callback queries ─────────────────────────────────────────────────
  bot.on('callback_query', async (q) => {
    const chatId = q.message.chat.id;
    const data = q.data;

    // Always answer to remove the loading spinner
    try { await bot.answerCallbackQuery(q.id); } catch {}

    // ── status_ref_<orderRef> — client checks order status via inline button
    if (data.startsWith('status_ref_')) {
      const orderRef = data.replace('status_ref_', '');
      try {
        const order = await get(
          `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id WHERE o.order_number = ?`,
          [orderRef]
        );
        if (!order) {
          return safeSend(chatId, `❌ Заявка *${mdEscape(orderRef)}* не найдена.`, { parse_mode: 'Markdown' });
        }
        const statusLabel = STATUS_LABELS[order.status] || order.status;
        return safeSend(chatId,
          `📋 *Заявка ${mdEscape(order.order_number)}*\n\n` +
          `Статус: ${statusLabel}\n` +
          `Мероприятие: ${EVENT_TYPES[order.event_type] || order.event_type}\n` +
          (order.event_date ? `Дата: ${mdEscape(order.event_date)}\n` : '') +
          (order.model_name ? `Модель: ${mdEscape(order.model_name)}\n` : ''),
          { parse_mode: 'Markdown' }
        );
      } catch (e) {
        console.error('[Bot] status_ref callback error:', e.message);
      }
      return;
    }

    // ── contact_agency
    if (data === 'contact_agency') {
      return safeSend(chatId,
        `📞 *Контакты Nevesty Models:*\n\n` +
        `Телефон: ${process.env.AGENCY_PHONE || '+7 (800) 555-00-00'}\n` +
        `Email: ${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}\n` +
        `Сайт: ${SITE_URL}`,
        { parse_mode: 'Markdown' }
      );
    }

    // ── Admin-only callbacks
    if (!isAdmin(chatId)) return;

    const [action, orderId] = data.split('_order_');
    if (!orderId) return;

    let order;
    try {
      order = await get('SELECT * FROM orders WHERE id = ?', [orderId]);
    } catch (e) {
      console.error('[Bot] callback get order error:', e.message);
      return;
    }
    if (!order) return safeSend(chatId, '❌ Заявка не найдена.');

    if (action === 'confirm') {
      try {
        const r = await run(
          "UPDATE orders SET status='confirmed', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",
          [orderId]
        );
        if (r.changes === 0) {
          await safeEdit(`⚠️ Заявка ${order.order_number} уже обработана.`, {
            chat_id: chatId, message_id: q.message.message_id
          });
          return;
        }
      } catch (e) {
        console.error('[Bot] confirm update error:', e.message);
        return;
      }
      await safeEdit(`✅ Заявка *${mdEscape(order.order_number)}* подтверждена.`, {
        chat_id: chatId, message_id: q.message.message_id, parse_mode: 'Markdown'
      });
      if (order.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, 'confirmed');
      return;
    }

    if (action === 'reject') {
      try {
        const r = await run(
          "UPDATE orders SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('completed','cancelled')",
          [orderId]
        );
        if (r.changes === 0) {
          await safeEdit(`⚠️ Заявка ${order.order_number} уже обработана.`, {
            chat_id: chatId, message_id: q.message.message_id
          });
          return;
        }
      } catch (e) {
        console.error('[Bot] reject update error:', e.message);
        return;
      }
      await safeEdit(`❌ Заявка *${mdEscape(order.order_number)}* отклонена.`, {
        chat_id: chatId, message_id: q.message.message_id, parse_mode: 'Markdown'
      });
      if (order.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, 'cancelled');
      return;
    }

    if (action === 'review') {
      try {
        const r = await run(
          "UPDATE orders SET status='reviewing', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('confirmed','cancelled','completed')",
          [orderId]
        );
        if (r.changes === 0) {
          await safeEdit(`⚠️ Заявка ${order.order_number} уже обработана.`, {
            chat_id: chatId, message_id: q.message.message_id
          });
          return;
        }
      } catch (e) {
        console.error('[Bot] review update error:', e.message);
        return;
      }
      await safeEdit(`🔍 Заявка *${mdEscape(order.order_number)}* взята в работу.`, {
        chat_id: chatId, message_id: q.message.message_id, parse_mode: 'Markdown'
      });
      return;
    }

    if (action === 'contact') {
      try {
        await run(
          `INSERT OR REPLACE INTO telegram_sessions (chat_id, state, order_id, data, updated_at)
           VALUES (?, 'replying', ?, '{}', CURRENT_TIMESTAMP)`,
          [String(chatId), orderId]
        );
      } catch (e) { console.error('[Bot] contact session error:', e.message); }
      return safeSend(chatId,
        `💬 Введите сообщение для клиента *${order.client_name}* (заявка ${order.order_number}):\n\n` +
        `_(Отправьте /cancel для отмены)_`,
        { parse_mode: 'Markdown' }
      );
    }
  });

  // ─── Text messages ────────────────────────────────────────────────────
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId = msg.chat.id;

    let session = null;
    try {
      session = await get('SELECT * FROM telegram_sessions WHERE chat_id = ?', [String(chatId)]);
    } catch (e) {
      console.error('[Bot] session get error:', e.message);
    }

    // ── Admin replying to client
    if (isAdmin(chatId) && session?.state === 'replying' && session.order_id) {
      let order = null;
      try {
        order = await get('SELECT * FROM orders WHERE id = ?', [session.order_id]);
      } catch (e) { console.error('[Bot] message get order error:', e.message); }

      if (order) {
        try {
          const admin = await get('SELECT username FROM admins WHERE telegram_id = ?', [String(chatId)]);
          const adminName = admin?.username || 'Менеджер';
          await run(
            'INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)',
            [session.order_id, 'admin', adminName, msg.text]
          );
        } catch (e) { console.error('[Bot] insert message error:', e.message); }

        if (order.client_chat_id) {
          await safeSend(order.client_chat_id,
            `💬 *Сообщение от менеджера* (${order.order_number}):\n\n${msg.text}`,
            { parse_mode: 'Markdown' }
          );
        }

        try {
          await run("UPDATE telegram_sessions SET state='idle', order_id=NULL WHERE chat_id=?", [String(chatId)]);
        } catch (e) { console.error('[Bot] session reset error:', e.message); }

        return safeSend(chatId, `✅ Сообщение отправлено клиенту ${order.client_name}.`);
      }

      // Order not found — reset session
      try {
        await run("UPDATE telegram_sessions SET state='idle', order_id=NULL WHERE chat_id=?", [String(chatId)]);
      } catch {}
      return safeSend(chatId, '❌ Заявка не найдена. Состояние сброшено.');
    }

    // ── Client message → forward to all admins
    if (!isAdmin(chatId)) {
      const clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      const username = msg.from.username ? `@${msg.from.username}` : 'нет username';

      // Find client's order
      let order = null;
      try {
        order = await get(
          'SELECT * FROM orders WHERE client_chat_id = ? ORDER BY created_at DESC LIMIT 1',
          [String(chatId)]
        );
      } catch (e) { console.error('[Bot] client order lookup error:', e.message); }

      // Save message
      if (order) {
        try {
          await run(
            'INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)',
            [order.id, 'client', clientName, msg.text]
          );
        } catch (e) { console.error('[Bot] client message insert error:', e.message); }
      }

      const adminIds = await getAdminChatIds();
      const header = order
        ? `📩 *Сообщение от клиента*\nЗаявка: *${order.order_number}*\nКлиент: ${clientName} (${username})\n\n`
        : `📩 *Новое сообщение от клиента*\n${clientName} (${username})\n\n`;

      for (const adminId of adminIds) {
        await safeSend(adminId, header + msg.text, {
          parse_mode: 'Markdown',
          reply_markup: order ? {
            inline_keyboard: [[
              { text: '💬 Ответить', callback_data: `contact_order_${order.id}` },
              { text: '📋 Заявка', url: `${SITE_URL}/admin/#orders/${order.id}` }
            ]]
          } : undefined
        });
      }

      return safeSend(chatId, '✅ Ваше сообщение передано менеджеру. Мы ответим вам в ближайшее время!');
    }
  });

  return {
    notifyAdmin,
    notifyNewOrder,
    notifyStatusChange,
    sendMessageToClient,
    instance: bot
  };
}

// ─── notifyAdmin ─────────────────────────────────────────────────────────────
// Send a message to all admin chat IDs — called from routes for real-time feed
async function notifyAdmin(text, opts = {}) {
  if (!bot) return;
  const adminIds = await getAdminChatIds();
  for (const id of adminIds) {
    await safeSend(id, text, { parse_mode: 'Markdown', ...opts });
  }
}

// ─── notifyNewOrder ───────────────────────────────────────────────────────────
async function notifyNewOrder(order) {
  if (!bot) return;

  let modelInfo = null;
  if (order.model_id) {
    try {
      modelInfo = await get('SELECT name FROM models WHERE id = ?', [order.model_id]);
    } catch (e) {
      console.error('[Bot] notifyNewOrder model lookup error:', e.message);
    }
  }

  const text =
    `🆕 *Новая заявка!*\n\n` +
    `📋 Номер: *${order.order_number}*\n` +
    `👤 Клиент: ${order.client_name}\n` +
    `📞 Телефон: ${order.client_phone}\n` +
    (order.client_email ? `📧 Email: ${order.client_email}\n` : '') +
    (order.client_telegram ? `💬 Telegram: @${order.client_telegram.replace('@', '')}\n` : '') +
    `\n🎭 Мероприятие: ${EVENT_TYPES[order.event_type] || order.event_type}\n` +
    (order.event_date ? `📅 Дата: ${order.event_date}\n` : '') +
    (order.location ? `📍 Место: ${order.location}\n` : '') +
    (order.budget ? `💰 Бюджет: ${order.budget}\n` : '') +
    (modelInfo ? `💃 Модель: ${modelInfo.name}\n` : '') +
    (order.comments ? `\n💬 Комментарий:\n${order.comments}` : '');

  const adminIds = await getAdminChatIds();
  for (const adminId of adminIds) {
    await safeSend(adminId, text, {
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
            { text: '🔗 Открыть заявку', url: `${SITE_URL}/admin/#orders/${order.id}` }
          ]
        ]
      }
    });
  }
}

// ─── notifyStatusChange ───────────────────────────────────────────────────────
async function notifyStatusChange(clientChatId, orderNumber, newStatus) {
  if (!bot || !clientChatId) return;
  const statusMessages = {
    confirmed: `✅ *Ваша заявка ${orderNumber} подтверждена!*\n\nМенеджер свяжется с вами в ближайшее время для уточнения деталей.`,
    reviewing: `🔍 *Ваша заявка ${orderNumber} на рассмотрении.*\n\nМы изучаем ваш запрос и скоро дадим ответ.`,
    in_progress: `▶️ *Заявка ${orderNumber} в процессе выполнения.*\n\nВсё идёт по плану!`,
    completed: `🏁 *Заявка ${orderNumber} завершена!*\n\nСпасибо, что выбрали Nevesty Models. Будем рады видеть вас снова! 💎`,
    cancelled: `❌ *Заявка ${orderNumber} отклонена.*\n\nЕсли у вас есть вопросы — свяжитесь с нами.`
  };
  const text = statusMessages[newStatus];
  if (text) {
    await safeSend(clientChatId, text, { parse_mode: 'Markdown' });
  }
}

// ─── sendMessageToClient ──────────────────────────────────────────────────────
async function sendMessageToClient(clientChatId, orderNumber, text) {
  if (!bot || !clientChatId) return;
  await safeSend(clientChatId,
    `💬 *Сообщение от менеджера* (${orderNumber}):\n\n${text}`,
    { parse_mode: 'Markdown' }
  );
}

module.exports = { initBot, notifyAdmin, notifyNewOrder, notifyStatusChange, sendMessageToClient };
