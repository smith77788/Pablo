require('dotenv').config();
const TelegramBot = require('node-telegram-bot-api');
const { query, run, get } = require('./database');

const ADMIN_IDS = (process.env.ADMIN_TELEGRAM_IDS || '').split(',').map(s => s.trim()).filter(Boolean);
const SITE_URL = process.env.SITE_URL || 'http://localhost:3000';

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
  const admins = await query("SELECT telegram_id FROM admins WHERE telegram_id IS NOT NULL AND telegram_id != ''");
  const dbIds = admins.map(a => a.telegram_id).filter(Boolean);
  return [...new Set([...ADMIN_IDS, ...dbIds])];
}

function initBot() {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token || token === 'your_bot_token_here') {
    console.warn('⚠️  TELEGRAM_BOT_TOKEN not set – bot disabled');
    return null;
  }

  bot = new TelegramBot(token, { polling: true });
  console.log('🤖 Telegram bot started');

  // ─── /start ───────────────────────────────────────────────────────────
  bot.onText(/\/start(.*)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const firstName = msg.from.first_name || 'Гость';

    // Save/update session
    await run(`INSERT OR REPLACE INTO telegram_sessions (chat_id, state, data, updated_at)
               VALUES (?, 'idle', '{}', CURRENT_TIMESTAMP)`, [String(chatId)]);

    if (isAdmin(chatId)) {
      return bot.sendMessage(chatId,
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
      const order = await get('SELECT * FROM orders WHERE order_number = ?', [orderRef]);
      if (order) {
        await run('UPDATE orders SET client_chat_id = ? WHERE order_number = ?', [String(chatId), orderRef]);
        return bot.sendMessage(chatId,
          `✅ *Ваша заявка ${orderRef} привязана к этому чату.*\n\n` +
          `Теперь вы будете получать уведомления о статусе заявки.\n` +
          `Вы можете писать сообщения прямо сюда — менеджер ответит вам.`,
          { parse_mode: 'Markdown' }
        );
      }
    }

    bot.sendMessage(chatId,
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

  // ─── /status ──────────────────────────────────────────────────────────
  bot.onText(/\/status (.+)/, async (msg, match) => {
    const chatId = msg.chat.id;
    const orderNumber = match[1].trim().toUpperCase();
    const order = await get(
      `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id WHERE o.order_number = ?`,
      [orderNumber]
    );
    if (!order) {
      return bot.sendMessage(chatId, `❌ Заявка *${orderNumber}* не найдена.`, { parse_mode: 'Markdown' });
    }
    const statusLabel = STATUS_LABELS[order.status] || order.status;
    bot.sendMessage(chatId,
      `📋 *Заявка ${order.order_number}*\n\n` +
      `Клиент: ${order.client_name}\n` +
      `Статус: ${statusLabel}\n` +
      `Мероприятие: ${EVENT_TYPES[order.event_type] || order.event_type}\n` +
      (order.event_date ? `Дата: ${order.event_date}\n` : '') +
      (order.model_name ? `Модель: ${order.model_name}\n` : ''),
      { parse_mode: 'Markdown' }
    );
  });

  // ─── /orders (admin) ──────────────────────────────────────────────────
  bot.onText(/\/orders/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    const orders = await query(
      `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id
       ORDER BY o.created_at DESC LIMIT 10`
    );
    if (!orders.length) return bot.sendMessage(msg.chat.id, '📭 Нет заявок.');
    let text = `📋 *Последние заявки:*\n\n`;
    for (const o of orders) {
      text += `${STATUS_LABELS[o.status] || o.status} *${o.order_number}*\n`;
      text += `  ${o.client_name} · ${EVENT_TYPES[o.event_type] || o.event_type}\n\n`;
    }
    bot.sendMessage(msg.chat.id, text, {
      parse_mode: 'Markdown',
      reply_markup: {
        inline_keyboard: [[{ text: '🔗 Открыть панель управления', url: `${SITE_URL}/admin/` }]]
      }
    });
  });

  // ─── /new_orders (admin) ──────────────────────────────────────────────
  bot.onText(/\/new_orders/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    const orders = await query(
      `SELECT o.*, m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id = m.id
       WHERE o.status = 'new' ORDER BY o.created_at DESC`
    );
    if (!orders.length) return bot.sendMessage(msg.chat.id, '✅ Новых заявок нет.');
    let text = `🆕 *Новые заявки (${orders.length}):*\n\n`;
    for (const o of orders) {
      text += `*${o.order_number}* — ${o.client_name}\n`;
      text += `📞 ${o.client_phone} · ${EVENT_TYPES[o.event_type] || o.event_type}\n\n`;
    }
    bot.sendMessage(msg.chat.id, text, { parse_mode: 'Markdown' });
  });

  // ─── /models (admin) ──────────────────────────────────────────────────
  bot.onText(/\/models/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    const models = await query('SELECT name, height, category, available FROM models ORDER BY id DESC');
    let text = `💃 *Модели агентства (${models.length}):*\n\n`;
    for (const m of models) {
      text += `${m.available ? '🟢' : '🔴'} *${m.name}* — ${m.height}см · ${m.category}\n`;
    }
    bot.sendMessage(msg.chat.id, text, { parse_mode: 'Markdown' });
  });

  // ─── /stats (admin) ───────────────────────────────────────────────────
  bot.onText(/\/stats/, async (msg) => {
    if (!isAdmin(msg.chat.id)) return;
    const [total, newO, confirmed, completed, models] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE status = 'new'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status = 'confirmed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status = 'completed'"),
      get('SELECT COUNT(*) as n FROM models WHERE available = 1'),
    ]);
    bot.sendMessage(msg.chat.id,
      `📊 *Статистика Nevesty Models*\n\n` +
      `📋 Всего заявок: *${total.n}*\n` +
      `🆕 Новых: *${newO.n}*\n` +
      `✅ Подтверждено: *${confirmed.n}*\n` +
      `🏁 Завершено: *${completed.n}*\n` +
      `💃 Доступно моделей: *${models.n}*`,
      { parse_mode: 'Markdown' }
    );
  });

  // ─── /help ────────────────────────────────────────────────────────────
  bot.onText(/\/help/, (msg) => {
    const chatId = msg.chat.id;
    if (isAdmin(chatId)) {
      return bot.sendMessage(chatId,
        `📖 *Справка для администратора:*\n\n` +
        `/orders — последние 10 заявок\n` +
        `/new_orders — только новые заявки\n` +
        `/models — список моделей\n` +
        `/stats — статистика агентства\n\n` +
        `*Управление заявками:*\n` +
        `Нажмите кнопки под уведомлением о заявке для быстрых действий.\n\n` +
        `*Ответ клиенту:*\n` +
        `Ответьте на пересланное сообщение клиента — оно будет отправлено ему.`,
        { parse_mode: 'Markdown' }
      );
    }
    bot.sendMessage(chatId,
      `📖 *Справка:*\n\n` +
      `/status НОМЕР — статус заявки\n\n` +
      `Для связи с менеджером просто напишите сообщение.\n` +
      `Оформить заявку: ${SITE_URL}/booking.html`,
      { parse_mode: 'Markdown' }
    );
  });

  // ─── Callback queries ─────────────────────────────────────────────────
  bot.on('callback_query', async (q) => {
    const chatId = q.message.chat.id;
    const data = q.data;
    bot.answerCallbackQuery(q.id);

    if (data === 'contact_agency') {
      return bot.sendMessage(chatId,
        `📞 *Контакты Nevesty Models:*\n\n` +
        `Телефон: ${process.env.AGENCY_PHONE || '+7 (800) 555-00-00'}\n` +
        `Email: ${process.env.AGENCY_EMAIL || 'info@nevesty-models.ru'}\n` +
        `Сайт: ${SITE_URL}`,
        { parse_mode: 'Markdown' }
      );
    }

    if (!isAdmin(chatId)) return;

    const [action, orderId] = data.split('_order_');
    if (!orderId) return;

    const order = await get('SELECT * FROM orders WHERE id = ?', [orderId]);
    if (!order) return bot.sendMessage(chatId, '❌ Заявка не найдена.');

    if (action === 'confirm') {
      await run("UPDATE orders SET status='confirmed', updated_at=CURRENT_TIMESTAMP WHERE id=?", [orderId]);
      bot.editMessageText(`✅ Заявка *${order.order_number}* подтверждена.`, {
        chat_id: chatId, message_id: q.message.message_id, parse_mode: 'Markdown'
      });
      if (order.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, 'confirmed');
    }

    if (action === 'reject') {
      await run("UPDATE orders SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?", [orderId]);
      bot.editMessageText(`❌ Заявка *${order.order_number}* отклонена.`, {
        chat_id: chatId, message_id: q.message.message_id, parse_mode: 'Markdown'
      });
      if (order.client_chat_id) notifyStatusChange(order.client_chat_id, order.order_number, 'cancelled');
    }

    if (action === 'review') {
      await run("UPDATE orders SET status='reviewing', updated_at=CURRENT_TIMESTAMP WHERE id=?", [orderId]);
      bot.editMessageText(`🔍 Заявка *${order.order_number}* взята в работу.`, {
        chat_id: chatId, message_id: q.message.message_id, parse_mode: 'Markdown'
      });
    }

    if (action === 'contact') {
      await run(`INSERT OR REPLACE INTO telegram_sessions (chat_id, state, order_id, data, updated_at)
                 VALUES (?, 'replying', ?, '{}', CURRENT_TIMESTAMP)`, [String(chatId), orderId]);
      bot.sendMessage(chatId,
        `💬 Введите сообщение для клиента *${order.client_name}* (заявка ${order.order_number}):\n\n_(Отправьте /cancel для отмены)_`,
        { parse_mode: 'Markdown' }
      );
    }
  });

  // ─── Text messages ────────────────────────────────────────────────────
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId = msg.chat.id;
    const session = await get('SELECT * FROM telegram_sessions WHERE chat_id = ?', [String(chatId)]);

    // Admin is replying to client
    if (isAdmin(chatId) && session?.state === 'replying' && session.order_id) {
      if (msg.text === '/cancel') {
        await run("UPDATE telegram_sessions SET state='idle', order_id=NULL WHERE chat_id=?", [String(chatId)]);
        return bot.sendMessage(chatId, '❌ Отправка отменена.');
      }
      const order = await get('SELECT * FROM orders WHERE id = ?', [session.order_id]);
      if (order) {
        const admin = await get('SELECT username FROM admins WHERE telegram_id = ?', [String(chatId)]);
        const adminName = admin?.username || 'Менеджер';
        await run('INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)', [session.order_id, 'admin', adminName, msg.text]);
        if (order.client_chat_id) {
          bot.sendMessage(order.client_chat_id,
            `💬 *Сообщение от менеджера* (${order.order_number}):\n\n${msg.text}`,
            { parse_mode: 'Markdown' }
          );
        }
        await run("UPDATE telegram_sessions SET state='idle', order_id=NULL WHERE chat_id=?", [String(chatId)]);
        bot.sendMessage(chatId, `✅ Сообщение отправлено клиенту ${order.client_name}.`);
      }
      return;
    }

    // Client message → forward to all admins
    if (!isAdmin(chatId)) {
      const clientName = [msg.from.first_name, msg.from.last_name].filter(Boolean).join(' ') || 'Клиент';
      const username = msg.from.username ? `@${msg.from.username}` : 'нет username';

      // Find client's order
      const order = await get('SELECT * FROM orders WHERE client_chat_id = ? ORDER BY created_at DESC LIMIT 1', [String(chatId)]);

      // Save message
      if (order) {
        await run('INSERT INTO messages (order_id, sender_type, sender_name, content) VALUES (?,?,?,?)', [order.id, 'client', clientName, msg.text]);
      }

      const adminIds = await getAdminChatIds();
      const header = order
        ? `📩 *Сообщение от клиента*\nЗаявка: *${order.order_number}*\nКлиент: ${clientName} (${username})\n\n`
        : `📩 *Новое сообщение от клиента*\n${clientName} (${username})\n\n`;

      for (const adminId of adminIds) {
        try {
          await bot.sendMessage(adminId, header + msg.text, {
            parse_mode: 'Markdown',
            reply_markup: order ? {
              inline_keyboard: [[
                { text: '💬 Ответить', callback_data: `contact_order_${order.id}` },
                { text: '📋 Заявка', url: `${SITE_URL}/admin/#orders/${order.id}` }
              ]]
            } : undefined
          });
        } catch {}
      }
      bot.sendMessage(chatId, '✅ Ваше сообщение передано менеджеру. Мы ответим вам в ближайшее время!');
    }
  });

  const publicBot = {
    notifyNewOrder,
    notifyStatusChange,
    sendMessageToClient,
    instance: bot
  };

  return publicBot;
}

async function notifyNewOrder(order) {
  if (!bot) return;
  const modelInfo = order.model_id
    ? await get('SELECT name FROM models WHERE id = ?', [order.model_id])
    : null;

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
    try {
      await bot.sendMessage(adminId, text, {
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
    } catch (e) {
      console.error('Bot notify error:', e.message);
    }
  }
}

function notifyStatusChange(clientChatId, orderNumber, newStatus) {
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
    bot.sendMessage(clientChatId, text, { parse_mode: 'Markdown' }).catch(() => {});
  }
}

function sendMessageToClient(clientChatId, orderNumber, text, managerName) {
  if (!bot || !clientChatId) return;
  bot.sendMessage(clientChatId,
    `💬 *Сообщение от менеджера* (${orderNumber}):\n\n${text}`,
    { parse_mode: 'Markdown' }
  ).catch(() => {});
}

module.exports = { initBot, notifyNewOrder, notifyStatusChange, sendMessageToClient };
