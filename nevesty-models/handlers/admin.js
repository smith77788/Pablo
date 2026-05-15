'use strict';

/**
 * Admin handlers module — extracted from bot.js
 * Functions: showAdminStats, showAdminModels, showAdminOrders
 *
 * Usage in bot.js:
 *   const adminHandlers = require('./handlers/admin');
 *   adminHandlers.init({ safeSend, isAdmin, esc, query, get });
 */

const { STATUS_LABELS, VALID_STATUSES } = require('../utils/constants');
const { query, get } = require('../database');

let safeSend, isAdmin, esc;

function init(deps) {
  ({ safeSend, isAdmin, esc } = deps);
}

async function showAdminStats(chatId) {
  if (!isAdmin(chatId)) return;
  try {
    const [
      total, todayOrders, weekOrders, monthOrders,
      active,
      done, canc,
      newClients, newClientsMonth,
      totalNew, confirmed,
    ] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE date(created_at) = date('now')"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now','-7 days')"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now','-30 days')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('new','reviewing','confirmed','in_progress')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='completed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='cancelled'"),
      get("SELECT COUNT(DISTINCT client_chat_id) as n FROM orders WHERE date(created_at) = date('now') AND client_chat_id IS NOT NULL"),
      get("SELECT COUNT(DISTINCT client_chat_id) as n FROM orders WHERE created_at >= datetime('now','-30 days') AND client_chat_id IS NOT NULL"),
      get("SELECT COUNT(*) as n FROM orders WHERE status != 'cancelled'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('confirmed','completed')"),
    ]);

    // Conversion: new→confirmed ratio
    const conversion = (totalNew.n || 0) > 0 ? Math.round(((confirmed.n || 0) / totalNew.n) * 100) : 0;

    // Revenue: sum of budgets for confirmed+completed orders
    let revenue = { total: 0, month: 0, week: 0 };
    try {
      const [revTotal, revMonth, revWeek] = await Promise.all([
        get(`SELECT SUM(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)) as s FROM orders WHERE status IN ('confirmed','completed') AND budget GLOB '[0-9]*'`),
        get(`SELECT SUM(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)) as s FROM orders WHERE status IN ('confirmed','completed') AND budget GLOB '[0-9]*' AND created_at >= datetime('now','-30 days')`),
        get(`SELECT SUM(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)) as s FROM orders WHERE status IN ('confirmed','completed') AND budget GLOB '[0-9]*' AND created_at >= datetime('now','-7 days')`),
      ]);
      revenue = {
        total: Math.round(revTotal?.s || 0),
        month: Math.round(revMonth?.s || 0),
        week: Math.round(revWeek?.s || 0),
      };
    } catch {}

    // Average deal ("средний чек") for confirmed+completed
    let avgCheck = null;
    try {
      const checkRow = await get(
        `SELECT AVG(CAST(REPLACE(REPLACE(REPLACE(budget,'₽',''),' ',''),',','') AS REAL)) as avg
         FROM orders WHERE status IN ('confirmed','completed') AND budget IS NOT NULL AND budget != '' AND budget GLOB '[0-9]*'`
      );
      if (checkRow && checkRow.avg) avgCheck = Math.round(checkRow.avg);
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

    // Top-3 cities by order count (via model city)
    let topCities = [];
    try {
      topCities = await query(
        `SELECT m.city, COUNT(o.id) as cnt
         FROM models m
         JOIN orders o ON o.model_id = m.id
         WHERE m.city IS NOT NULL AND m.city != ''
         GROUP BY m.city
         ORDER BY cnt DESC
         LIMIT 3`
      );
    } catch {}

    // Top-5 models by view count (bonus insight)
    let topViewed = [];
    try {
      topViewed = await query(`
        SELECT name, view_count,
          (SELECT COUNT(*) FROM orders WHERE model_id=models.id AND status NOT IN ('cancelled')) as order_count
        FROM models
        ORDER BY view_count DESC
        LIMIT 5
      `);
    } catch {}

    // Average deal cycle (days from new to completed)
    let avgCycleDays = null;
    try {
      const cycleRow = await get(
        `SELECT AVG(
           CAST(julianday(updated_at) - julianday(created_at) AS INTEGER)
         ) as avg_days
         FROM orders
         WHERE status='completed' AND updated_at IS NOT NULL AND created_at IS NOT NULL`
      );
      if (cycleRow && cycleRow.avg_days) avgCycleDays = Math.round(cycleRow.avg_days);
    } catch {}

    // Repeat clients (ordered more than once)
    let repeatClients = 0;
    try {
      const rc = await get(`SELECT COUNT(*) as n FROM (SELECT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL GROUP BY client_chat_id HAVING COUNT(*) > 1)`);
      repeatClients = rc?.n || 0;
    } catch {}

    const medals = ['🥇','🥈','🥉'];
    const fmt = n => esc(n.toLocaleString('ru'));

    let text = `*📊 Статистика агентства*\n\n`;

    // Daily / weekly / monthly / total
    text += `📅 *Сегодня:* ${esc(String(todayOrders.n))} заявок \\| ${esc(String(newClients.n))} новых клиентов\n`;
    text += `📅 *Неделя:* ${esc(String(weekOrders.n))} заявок`;
    if (revenue.week > 0) text += ` \\| ${fmt(revenue.week)} руб\\.`;
    text += `\n`;
    text += `📅 *Месяц:* ${esc(String(monthOrders.n))} заявок`;
    if (revenue.month > 0) text += ` \\| ${fmt(revenue.month)} руб\\.`;
    text += `\n`;
    text += `📅 *Всего:* ${esc(String(total.n))} заявок\n`;

    // Revenue total
    if (revenue.total > 0) {
      text += `\n💰 *Выручка за всё время:* ${fmt(revenue.total)} руб\\. \\(_подтверждённые/завершённые_\\)\n`;
    }

    // Top-3 models
    if (topModels.length) {
      text += `\n🏆 *Топ\\-3 модели по заявкам:*\n`;
      topModels.forEach((m, i) => {
        text += `  ${medals[i] || `${i+1}\\.`} ${esc(m.name)} — ${esc(String(m.cnt))} заявок\n`;
      });
    }

    // Top-3 cities
    if (topCities.length) {
      text += `\n🏙 *Топ\\-3 города:*\n`;
      topCities.forEach((c, i) => {
        text += `  ${medals[i] || `${i+1}\\.`} ${esc(c.city)} — ${esc(String(c.cnt))} заявок\n`;
      });
    }

    // Top-5 by views (bonus)
    if (topViewed.length) {
      text += `\n👁 *Топ\\-5 по просмотрам:*\n`;
      topViewed.forEach((m, i) => {
        text += `  ${i+1}\\. ${esc(m.name)} — 👁 ${esc(String(m.view_count || 0))} просм\\., 📋 ${esc(String(m.order_count || 0))} заявок\n`;
      });
    }

    // Conversion & avg check
    text += `\n📊 *Конверсия:* ${esc(String(conversion))}% \\(_new→confirmed_\\)\n`;
    if (avgCheck) text += `💳 *Средний чек:* ${fmt(avgCheck)} руб\\.\n`;

    // Active & new clients
    text += `\n🔄 *Активных заявок сейчас:* ${esc(String(active.n))}\n`;
    text += `⭐ *Новых клиентов за месяц:* ${esc(String(newClientsMonth.n))}\n`;

    // Additional metrics
    text += `✅ *Завершено:* ${esc(String(done.n))}  ❌ *Отклонено:* ${esc(String(canc.n))}\n`;
    text += `🔁 *Повторные клиенты:* ${esc(String(repeatClients))}\n`;
    if (avgCycleDays !== null) {
      text += `⏱ *Средний цикл сделки:* ${esc(String(avgCycleDays))} дн\\.\n`;
    }

    // Broadcast stats
    const bcastRow = await get(`SELECT COUNT(*) as total, SUM(sent_count) as sent FROM scheduled_broadcasts WHERE status='sent'`).catch(() => null);
    if (bcastRow?.total > 0) {
      text += `📢 *Рассылки:* ${esc(String(bcastRow.total))} отправлено, ${esc(String(bcastRow.sent || 0))} доставлено\n`;
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        [{ text: '🔄 Обновить', callback_data: 'adm_stats_refresh' }, { text: '← Меню', callback_data: 'admin_menu' }],
        [{ text: '📋 Все заявки', callback_data: 'adm_orders__0' }],
        [{ text: '📊 Аналитика (сайт)', url: 'https://nevesty-models.ru/admin/analytics.html' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminStats:', e.message); }
}

async function showAdminModels(chatId, page, opts = {}) {
  try {
    const showArchived = opts.archived || false;
    const sort = opts.sort || 'name';
    const sortMap = { orders: 'order_count DESC', views: 'view_count DESC', name: 'name ASC' };
    const orderBy = sortMap[sort] || 'name ASC';
    const archiveFilter = showArchived ? 'archived=1' : 'archived=0';

    const perPage = 8;
    const offset  = page * perPage;
    const [countRow, slice] = await Promise.all([
      get(`SELECT COUNT(*) as n FROM models WHERE ${archiveFilter}`),
      query(`SELECT * FROM models WHERE ${archiveFilter} ORDER BY ${orderBy} LIMIT ? OFFSET ?`, [perPage, offset]),
    ]);
    const total = countRow?.n || 0;

    const title = showArchived ? '📦 *Архив моделей*' : '💃 *Модели агентства*';
    let text = `${title} \\(всего: ${total}\\)\n\n`;
    const btns = slice.map(m => {
      text += `${m.available ? '🟢' : '🔴'}${m.archived ? ' 📦' : ''} *${esc(m.name)}* — ${m.height}см, ${esc(m.category)}\n`;
      return [
        { text: `${m.name}`, callback_data: `adm_model_${m.id}` },
        { text: m.available ? '🟢' : '🔴', callback_data: `adm_toggle_avail_${m.id}` },
      ];
    });

    const nav = [];
    if (page > 0)              nav.push({ text: '◀️', callback_data: `adm_models_p_${page-1}_${sort}_${showArchived?1:0}` });
    if ((page+1)*perPage < total) nav.push({ text: '▶️', callback_data: `adm_models_p_${page+1}_${sort}_${showArchived?1:0}` });

    const sortRow = [
      { text: `${sort==='name'?'✅ ':''}🔤 Алфавит`,   callback_data: `adm_models_p_0_name_${showArchived?1:0}` },
      { text: `${sort==='orders'?'✅ ':''}📊 Заказы`,   callback_data: `adm_models_p_0_orders_${showArchived?1:0}` },
      { text: `${sort==='views'?'✅ ':''}👁 Просмотры`, callback_data: `adm_models_p_0_views_${showArchived?1:0}` },
    ];
    const archiveToggle = showArchived
      ? [{ text: '💃 Активные модели', callback_data: 'adm_models_p_0_name_0' }]
      : [{ text: '📦 Архив',           callback_data: 'adm_models_p_0_name_1' }];

    return safeSend(chatId, text || `${title}\n_Список пуст\\._`, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...btns,
        ...(nav.length ? [nav] : []),
        sortRow,
        [{ text: '🔍 Поиск по имени', callback_data: 'adm_search_model' }, ...archiveToggle],
        [{ text: '← Меню', callback_data: 'admin_menu' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminModels:', e.message); }
}

async function showAdminOrders(chatId, statusFilter, page = 0) {
  try {
    const safe = VALID_STATUSES.includes(statusFilter) ? statusFilter : null;
    page = parseInt(page) || 0;
    const [total, orders] = await Promise.all([
      safe
        ? get('SELECT COUNT(*) as n FROM orders WHERE status=?', [safe])
        : get('SELECT COUNT(*) as n FROM orders'),
      safe
        ? query('SELECT o.*,m.name as model_name, (SELECT COUNT(*) FROM order_notes WHERE order_id=o.id) as note_count FROM orders o LEFT JOIN models m ON o.model_id=m.id WHERE o.status=? ORDER BY o.created_at DESC LIMIT 8 OFFSET ?', [safe, page*8])
        : query('SELECT o.*,m.name as model_name, (SELECT COUNT(*) FROM order_notes WHERE order_id=o.id) as note_count FROM orders o LEFT JOIN models m ON o.model_id=m.id ORDER BY o.created_at DESC LIMIT 8 OFFSET ?', [page*8])
    ]);

    if (!orders.length) {
      return safeSend(chatId, '📭 Заявок нет.', {
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] }
      });
    }

    const activeFilter = safe || '';
    const filterLabel = safe ? (STATUS_LABELS[safe]||safe) : 'Все';
    let text = `📋 *Заявки — ${filterLabel}* \\(${total.n}\\)\n\n`;

    const btns = orders.map(o => {
      const icon = STATUS_LABELS[o.status]?.split(' ')[0]||'';
      const noteBadge = o.note_count > 0 ? ` \\(📝 ${esc(String(o.note_count))}\\)` : '';
      text += `${icon} *${esc(o.order_number)}* — ${esc(o.client_name)}${noteBadge}\n`;
      const noteLabel = o.note_count > 0 ? ` (📝 ${o.note_count})` : '';
      const row = [{ text: `${o.order_number}  ·  ${o.client_name}${noteLabel}`, callback_data: `adm_order_${o.id}` }];
      if (o.status === 'new')       row.push({ text: '✅ Принять',   callback_data: `adm_quick_confirm_${o.id}` });
      if (o.status === 'confirmed') row.push({ text: '🏁 Завершить', callback_data: `adm_quick_complete_${o.id}` });
      return row;
    });

    const nav = [];
    if (page > 0)             nav.push({ text: '◀️', callback_data: `adm_orders_${activeFilter}_${page-1}` });
    if ((page+1)*8 < total.n) nav.push({ text: '▶️', callback_data: `adm_orders_${activeFilter}_${page+1}` });
    const filterRow1 = [
      { text: (activeFilter === '') ? '📋 Все ✓' : '📋 Все',             callback_data: 'adm_orders__0'         },
      { text: (activeFilter === 'new') ? '🆕 Новые ✓' : '🆕 Новые',       callback_data: 'adm_orders_new_0'      },
      { text: (activeFilter === 'confirmed') ? '✅ Подтвержд. ✓' : '✅ Подтвержд.', callback_data: 'adm_orders_confirmed_0' },
    ];
    const filterRow2 = [
      { text: (activeFilter === 'cancelled') ? '❌ Отменённые ✓' : '❌ Отменённые', callback_data: 'adm_orders_cancelled_0' },
      { text: (activeFilter === 'completed') ? '🏁 Завершённые ✓' : '🏁 Завершённые', callback_data: 'adm_orders_completed_0' },
      { text: '📅 Сегодня', callback_data: 'adm_orders_today' },
    ];

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        filterRow1,
        filterRow2,
        ...btns,
        ...(nav.length ? [nav] : []),
        [{ text: '📦 Все новые → В работу', callback_data: 'adm_bulk_new_to_review' }],
        [{ text: '🔍 Поиск по №',           callback_data: 'adm_order_search'        },
         { text: '🔽 По модели',             callback_data: 'adm_orders_filter_model' }],
        [{ text: '🔍 Найти заявку', callback_data: 'adm_search_order' },
         { text: '← Меню',         callback_data: 'admin_menu'        }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminOrders:', e.message); }
}

async function showAdminOrdersToday(chatId) {
  try {
    const orders = await query(
      `SELECT o.*, m.name as model_name,
        (SELECT COUNT(*) FROM order_notes WHERE order_id=o.id) as note_count
       FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE DATE(o.created_at)=DATE('now','localtime')
       ORDER BY o.created_at DESC LIMIT 20`
    );

    if (!orders.length) {
      return safeSend(chatId, '📭 Сегодня заявок нет\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← К заявкам', callback_data: 'adm_orders__0' }]] }
      });
    }

    let text = `📅 *Заявки за сегодня* \\(${orders.length}\\)\n\n`;
    const btns = orders.map(o => {
      const icon = STATUS_LABELS[o.status]?.split(' ')[0] || '';
      const noteBadge = o.note_count > 0 ? ` \\(📝 ${esc(String(o.note_count))}\\)` : '';
      text += `${icon} *${esc(o.order_number)}* — ${esc(o.client_name)}${noteBadge}\n`;
      const noteLabel = o.note_count > 0 ? ` (📝 ${o.note_count})` : '';
      const row = [{ text: `${o.order_number}  ·  ${o.client_name}${noteLabel}`, callback_data: `adm_order_${o.id}` }];
      if (o.status === 'new')       row.push({ text: '✅ Принять',   callback_data: `adm_quick_confirm_${o.id}` });
      if (o.status === 'confirmed') row.push({ text: '🏁 Завершить', callback_data: `adm_quick_complete_${o.id}` });
      return row;
    });

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        ...btns,
        [{ text: '← К заявкам', callback_data: 'adm_orders__0' }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminOrdersToday:', e.message); }
}

module.exports = { init, showAdminStats, showAdminModels, showAdminOrders, showAdminOrdersToday };
