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
    const budgetExpr = `CAST(REPLACE(REPLACE(REPLACE(REPLACE(budget,'₽',''),'руб',''),' ',''),',','.') AS REAL)`;

    const [
      total,
      todayOrders,
      weekOrders,
      monthOrders,
      active,
      done,
      canc,
      newClientsMonth,
      totalNew,
      confirmed,
      totalClients,
      // Models counts
      totalModels,
      activeModels,
      featuredModels,
    ] = await Promise.all([
      get('SELECT COUNT(*) as n FROM orders'),
      get("SELECT COUNT(*) as n FROM orders WHERE date(created_at) = date('now')"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now','-7 days')"),
      get("SELECT COUNT(*) as n FROM orders WHERE created_at >= datetime('now','-30 days')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('new','reviewing','confirmed','in_progress')"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='completed'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status='cancelled'"),
      // New clients: first order ever placed in last 30 days
      get(`SELECT COUNT(DISTINCT client_chat_id) as n
           FROM orders
           WHERE client_chat_id IS NOT NULL AND client_chat_id != ''
             AND CAST(client_chat_id AS INTEGER) > 0
             AND created_at >= datetime('now','-30 days')
             AND client_chat_id NOT IN (
               SELECT client_chat_id FROM orders
               WHERE client_chat_id IS NOT NULL
                 AND created_at < datetime('now','-30 days')
             )`),
      get("SELECT COUNT(*) as n FROM orders WHERE status != 'cancelled'"),
      get("SELECT COUNT(*) as n FROM orders WHERE status IN ('confirmed','completed')"),
      get(
        "SELECT COUNT(DISTINCT client_chat_id) as n FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' AND CAST(client_chat_id AS INTEGER) > 0"
      ),
      // Models section
      get('SELECT COUNT(*) as n FROM models WHERE archived=0'),
      get('SELECT COUNT(*) as n FROM models WHERE archived=0 AND available=1'),
      get('SELECT COUNT(*) as n FROM models WHERE archived=0 AND featured=1').catch(() => ({ n: 0 })),
    ]);

    // Conversion: new→confirmed ratio
    const conversion = (totalNew.n || 0) > 0 ? Math.round(((confirmed.n || 0) / totalNew.n) * 100) : 0;

    // Revenue: sum of budgets for confirmed+completed orders
    let revenue = { total: 0, month: 0 };
    let avgCheck = null;
    try {
      const [revTotal, revMonth, avgRow] = await Promise.all([
        get(
          `SELECT SUM(${budgetExpr}) as s FROM orders WHERE status IN ('confirmed','completed') AND budget IS NOT NULL AND budget != '' AND budget GLOB '[0-9]*'`
        ),
        get(
          `SELECT SUM(${budgetExpr}) as s FROM orders WHERE status IN ('confirmed','completed') AND budget IS NOT NULL AND budget != '' AND budget GLOB '[0-9]*' AND created_at >= datetime('now','-30 days')`
        ),
        get(
          `SELECT AVG(${budgetExpr}) as avg FROM orders WHERE status IN ('confirmed','completed') AND budget IS NOT NULL AND budget != '' AND budget GLOB '[0-9]*'`
        ),
      ]);
      revenue = {
        total: Math.round(revTotal?.s || 0),
        month: Math.round(revMonth?.s || 0),
      };
      if (avgRow?.avg) avgCheck = Math.round(avgRow.avg);
    } catch {}

    // Top-5 models by completed orders
    let topModels = [];
    try {
      topModels = await query(
        `SELECT m.name, COUNT(o.id) as cnt
         FROM models m
         JOIN orders o ON o.model_id = m.id AND o.status = 'completed'
         GROUP BY m.id, m.name
         ORDER BY cnt DESC
         LIMIT 5`
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
        WHERE archived=0
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

    // Returning clients (more than 1 order)
    let repeatClients = 0;
    try {
      const rc = await get(
        `SELECT COUNT(*) as n FROM (SELECT client_chat_id FROM orders WHERE client_chat_id IS NOT NULL AND client_chat_id != '' GROUP BY client_chat_id HAVING COUNT(*) > 1)`
      );
      repeatClients = rc?.n || 0;
    } catch {}

    const fmt = n => {
      if (!n || isNaN(n)) return esc('0');
      return esc(Math.round(n).toLocaleString('ru'));
    };

    let text = `📊 *Статистика агентства*\n\n`;

    // ── Заявки section ──────────────────────────────
    text += `📋 *Заявки*\n`;
    text += `• Сегодня: ${esc(String(todayOrders.n))}\n`;
    text += `• Неделя \\(7 дней\\): ${esc(String(weekOrders.n))}\n`;
    text += `• Месяц \\(30 дней\\): ${esc(String(monthOrders.n))}\n`;
    text += `• Всего: ${esc(String(total.n))}\n`;
    text += `• Активных сейчас: *${esc(String(active.n))}*\n`;
    text += `• Конверсия new→confirmed: *${esc(String(conversion))}%*\n`;

    // ── Revenue section ──────────────────────────────
    text += `\n💰 *Выручка* _\\(confirmed \\+ completed\\)_\n`;
    text += `• За месяц: *${fmt(revenue.month)} руб\\.*\n`;
    text += `• Всего: ${fmt(revenue.total)} руб\\.\n`;
    if (avgCheck) text += `• Средний чек: ${fmt(avgCheck)} руб\\.\n`;

    // ── Models section ───────────────────────────────
    text += `\n💃 *Модели*\n`;
    text += `• Всего: ${esc(String(totalModels?.n || 0))}\n`;
    text += `• Активных \\(available\\): ${esc(String(activeModels?.n || 0))}\n`;
    text += `• Топовых \\(featured\\): ${esc(String(featuredModels?.n || 0))}\n`;

    if (topModels.length) {
      text += `\n👑 *Топ\\-5 моделей по завершённым заявкам*\n`;
      topModels.forEach((m, i) => {
        text += `${i + 1}\\. ${esc(m.name)} — ${esc(String(m.cnt))} завершённых\n`;
      });
    }

    // ── Clients section ──────────────────────────────
    text += `\n👥 *Клиенты*\n`;
    text += `• Всего уникальных: ${esc(String(totalClients?.n || 0))}\n`;
    text += `• Новых за месяц: ${esc(String(newClientsMonth?.n || 0))}\n`;
    text += `• Повторных \\(>1 заявки\\): ${esc(String(repeatClients))}\n`;

    // ── Additional metrics (bonus) ───────────────────
    if (done.n > 0 || canc.n > 0) {
      text += `\n_✅ Завершено: ${esc(String(done.n))}  ❌ Отменено: ${esc(String(canc.n))}_\n`;
    }
    if (avgCycleDays !== null) {
      text += `_⏱ Средний цикл сделки: ${esc(String(avgCycleDays))} дн\\._\n`;
    }

    // Top-3 cities (bonus)
    if (topCities.length) {
      text += `\n🏙️ *Топ\\-3 города*\n`;
      topCities.forEach((c, i) => {
        text += `${i + 1}\\. ${esc(c.city)} — ${esc(String(c.cnt))} заявок\n`;
      });
    }

    // Top-5 by views (bonus)
    if (topViewed.length) {
      text += `\n👁 *Топ\\-5 по просмотрам:*\n`;
      topViewed.forEach((m, i) => {
        text += `  ${i + 1}\\. ${esc(m.name)} — 👁 ${esc(String(m.view_count || 0))} просм\\., 📋 ${esc(String(m.order_count || 0))} заявок\n`;
      });
    }

    // Broadcast stats
    const bcastRow = await get(
      `SELECT COUNT(*) as total, SUM(sent_count) as sent FROM scheduled_broadcasts WHERE status='sent'`
    ).catch(() => null);
    if (bcastRow?.total > 0) {
      text += `\n📢 *Рассылки:* ${esc(String(bcastRow.total))} отправлено, ${esc(String(bcastRow.sent || 0))} доставлено\n`;
    }

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          [
            { text: '🔄 Обновить', callback_data: 'adm_stats_refresh' },
            { text: '← Меню', callback_data: 'admin_menu' },
          ],
          [
            { text: '📤 Экспорт CSV', callback_data: 'adm_stats_csv' },
            { text: '📋 Все заявки', callback_data: 'adm_orders__0' },
          ],
          [{ text: '📊 Аналитика (сайт)', url: 'https://nevesty-models.ru/admin/analytics.html' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showAdminStats:', e.message);
  }
}

async function showAdminModels(chatId, page, opts = {}) {
  try {
    const showArchived = opts.archived || false;
    const sort = opts.sort || 'name';
    const sortMap = { orders: 'order_count DESC', views: 'view_count DESC', name: 'name ASC' };
    const orderBy = sortMap[sort] || 'name ASC';
    const archiveFilter = showArchived ? 'archived=1' : 'archived=0';

    const perPage = 8;
    const offset = page * perPage;
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
    if (page > 0) nav.push({ text: '◀️', callback_data: `adm_models_p_${page - 1}_${sort}_${showArchived ? 1 : 0}` });
    if ((page + 1) * perPage < total)
      nav.push({ text: '▶️', callback_data: `adm_models_p_${page + 1}_${sort}_${showArchived ? 1 : 0}` });

    const sortRow = [
      {
        text: `${sort === 'name' ? '✅ ' : ''}🔤 Алфавит`,
        callback_data: `adm_models_p_0_name_${showArchived ? 1 : 0}`,
      },
      {
        text: `${sort === 'orders' ? '✅ ' : ''}📊 Заказы`,
        callback_data: `adm_models_p_0_orders_${showArchived ? 1 : 0}`,
      },
      {
        text: `${sort === 'views' ? '✅ ' : ''}👁 Просмотры`,
        callback_data: `adm_models_p_0_views_${showArchived ? 1 : 0}`,
      },
    ];
    const archiveToggle = showArchived
      ? [{ text: '💃 Активные модели', callback_data: 'adm_models_p_0_name_0' }]
      : [{ text: '📦 Архив', callback_data: 'adm_models_p_0_name_1' }];

    return safeSend(chatId, text || `${title}\n_Список пуст\\._`, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          ...btns,
          ...(nav.length ? [nav] : []),
          sortRow,
          [{ text: '🔍 Поиск по имени', callback_data: 'adm_search_model' }, ...archiveToggle],
          [{ text: '← Меню', callback_data: 'admin_menu' }],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showAdminModels:', e.message);
  }
}

async function showAdminOrders(chatId, statusFilter, page = 0) {
  try {
    const safe = VALID_STATUSES.includes(statusFilter) ? statusFilter : null;
    page = parseInt(page) || 0;
    const [total, orders] = await Promise.all([
      safe ? get('SELECT COUNT(*) as n FROM orders WHERE status=?', [safe]) : get('SELECT COUNT(*) as n FROM orders'),
      safe
        ? query(
            'SELECT o.*,m.name as model_name, (SELECT COUNT(*) FROM order_notes WHERE order_id=o.id) as note_count FROM orders o LEFT JOIN models m ON o.model_id=m.id WHERE o.status=? ORDER BY o.created_at DESC LIMIT 8 OFFSET ?',
            [safe, page * 8]
          )
        : query(
            'SELECT o.*,m.name as model_name, (SELECT COUNT(*) FROM order_notes WHERE order_id=o.id) as note_count FROM orders o LEFT JOIN models m ON o.model_id=m.id ORDER BY o.created_at DESC LIMIT 8 OFFSET ?',
            [page * 8]
          ),
    ]);

    if (!orders.length) {
      return safeSend(chatId, '📭 Заявок нет.', {
        reply_markup: { inline_keyboard: [[{ text: '← Назад', callback_data: 'admin_menu' }]] },
      });
    }

    const activeFilter = safe || '';
    const filterLabel = safe ? STATUS_LABELS[safe] || safe : 'Все';
    let text = `📋 *Заявки — ${filterLabel}* \\(${total.n}\\)\n\n`;

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

    const nav = [];
    if (page > 0) nav.push({ text: '◀️', callback_data: `adm_orders_${activeFilter}_${page - 1}` });
    if ((page + 1) * 8 < total.n) nav.push({ text: '▶️', callback_data: `adm_orders_${activeFilter}_${page + 1}` });
    // БЛОК 3.3: фильтры через adm_ord_filter_* callbacks
    const filterRow1 = [
      { text: activeFilter === 'new' ? '📥 Новые ✓' : '📥 Новые', callback_data: 'adm_ord_filter_new' },
      {
        text: activeFilter === 'confirmed' ? '✅ Подтверж. ✓' : '✅ Подтверж.',
        callback_data: 'adm_ord_filter_confirmed',
      },
      {
        text: activeFilter === 'completed' ? '🏁 Завершённые ✓' : '🏁 Завершённые',
        callback_data: 'adm_ord_filter_completed',
      },
    ];
    const filterRow2 = [
      {
        text: activeFilter === 'cancelled' ? '❌ Отменённые ✓' : '❌ Отменённые',
        callback_data: 'adm_ord_filter_cancelled',
      },
      { text: activeFilter === '' ? '📋 Все заявки ✓' : '📋 Все заявки', callback_data: 'adm_ord_filter_all' },
    ];
    const filterRow3 = [
      { text: '📅 Сегодня', callback_data: 'adm_orders_today' },
      { text: '📅 Неделя', callback_data: 'adm_orders_week' },
    ];

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: {
        inline_keyboard: [
          filterRow1,
          filterRow2,
          filterRow3,
          ...btns,
          ...(nav.length ? [nav] : []),
          [{ text: '📦 Все новые → В работу', callback_data: 'adm_bulk_new_to_review' }],
          [
            { text: '🔍 Поиск по №', callback_data: 'adm_ord_search' },
            { text: '🔽 По модели', callback_data: 'adm_orders_filter_model' },
          ],
          [
            { text: '🔍 Найти заявку', callback_data: 'adm_search_order' },
            { text: '← Меню', callback_data: 'admin_menu' },
          ],
        ],
      },
    });
  } catch (e) {
    console.error('[Bot] showAdminOrders:', e.message);
  }
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
        reply_markup: { inline_keyboard: [[{ text: '← К заявкам', callback_data: 'adm_orders__0' }]] },
      });
    }

    let text = `📅 *Заявки за сегодня* \\(${orders.length}\\)\n\n`;
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
      reply_markup: { inline_keyboard: [...btns, [{ text: '← К заявкам', callback_data: 'adm_orders__0' }]] },
    });
  } catch (e) {
    console.error('[Bot] showAdminOrdersToday:', e.message);
  }
}

async function showAdminOrdersWeek(chatId) {
  try {
    const orders = await query(
      `SELECT o.*, m.name as model_name,
        (SELECT COUNT(*) FROM order_notes WHERE order_id=o.id) as note_count
       FROM orders o
       LEFT JOIN models m ON o.model_id=m.id
       WHERE DATE(o.created_at) >= DATE('now','-6 days','localtime')
       ORDER BY o.created_at DESC LIMIT 50`
    );

    if (!orders.length) {
      return safeSend(chatId, '📭 За последние 7 дней заявок нет\\.', {
        parse_mode: 'MarkdownV2',
        reply_markup: { inline_keyboard: [[{ text: '← К заявкам', callback_data: 'adm_orders__0' }]] },
      });
    }

    let text = `📅 *Заявки за неделю* \\(${orders.length}\\)\n\n`;
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
      reply_markup: { inline_keyboard: [...btns, [{ text: '← К заявкам', callback_data: 'adm_orders__0' }]] },
    });
  } catch (e) {
    console.error('[Bot] showAdminOrdersWeek:', e.message);
  }
}

// ─── Admin Reviews Management ─────────────────────────────────────────────────

/**
 * showAdminReviews(chatId, filter='pending', page=0)
 * Displays paginated list of reviews with approve/reject/delete controls.
 *
 * Filter values: 'pending' | 'approved' | 'all'
 * Reviews table: id, order_id, client_chat_id, model_id, rating, text, approved, created_at
 */
async function showAdminReviews(chatId, filter = 'pending', page = 0) {
  if (!isAdmin(chatId)) return;
  // Sanitize filter to known values only
  const VALID_FILTERS = ['pending', 'approved', 'all'];
  if (!VALID_FILTERS.includes(filter)) filter = 'pending';
  // Ensure page is non-negative integer
  page = Math.max(0, parseInt(page) || 0);

  const PER_PAGE = 5;

  try {
    // Build SQL based on filter — handle missing table gracefully
    let whereClause;
    if (filter === 'pending') {
      whereClause = "WHERE r.approved=0 AND (r.status IS NULL OR r.status != 'rejected')";
    } else if (filter === 'approved') {
      whereClause = 'WHERE r.approved=1';
    } else {
      whereClause = '';
    }

    // Count total for pagination
    const countSql = `SELECT COUNT(*) as n FROM reviews r ${whereClause}`;
    const countRow = await get(countSql).catch(() => ({ n: 0 }));
    const total = countRow?.n || 0;

    // Fetch page slice
    const rows = await query(
      `SELECT r.*, m.name as model_name
       FROM reviews r
       LEFT JOIN models m ON r.model_id = m.id
       ${whereClause}
       ORDER BY r.created_at DESC
       LIMIT ? OFFSET ?`,
      [PER_PAGE, page * PER_PAGE]
    ).catch(() => []);

    // Filter tab buttons (rev_filter_* callbacks, also handled by adm_rev_* aliases in bot.js)
    const filterBtns = [
      { text: filter === 'pending' ? '🕐 Ожидают ✓' : '🕐 Ожидают', callback_data: 'rev_filter_pending' },
      { text: filter === 'approved' ? '✅ Одобренные ✓' : '✅ Одобренные', callback_data: 'rev_filter_approved' },
      { text: filter === 'all' ? '📋 Все ✓' : '📋 Все', callback_data: 'rev_filter_all' },
    ];

    if (!rows.length) {
      return safeSend(chatId, 'Нет отзывов для показа', {
        reply_markup: { inline_keyboard: [filterBtns, [{ text: '← Меню', callback_data: 'admin_menu' }]] },
      });
    }

    // Build single combined message with all reviews on this page
    let text = `⭐ *Отзывы* — ${esc(filter === 'pending' ? 'ожидают' : filter === 'approved' ? 'одобренные' : 'все')} \\(${esc(String(total))}\\)\n\n`;

    const keyboard = [];
    keyboard.push(filterBtns);

    for (const r of rows) {
      const stars = '⭐'.repeat(Math.max(1, Math.min(5, r.rating || 1)));
      const preview = r.text ? r.text.slice(0, 100) + (r.text.length > 100 ? '…' : '') : '—';
      const statusIcon = r.approved ? '✅' : r.status === 'rejected' ? '❌' : '⏳';
      const statusLabel = r.approved ? 'Одобрен' : r.status === 'rejected' ? 'Отклонён' : 'Ожидает';
      const modelInfo = r.model_name ? ` \\| ${esc(r.model_name)}` : '';
      const clientName = r.client_name || '—';
      text += `${statusIcon} *\\#${esc(String(r.id))}* ${stars}${modelInfo}\n`;
      text += `👤 ${esc(clientName)} _\\(${esc(statusLabel)}\\)_\n`;
      text += `_${esc(preview)}_\n\n`;

      keyboard.push([
        { text: '✅ Одобрить', callback_data: `rev_approve_${r.id}` },
        { text: '❌ Отклонить', callback_data: `rev_reject_${r.id}` },
        { text: '🗑️ Удалить', callback_data: `rev_delete_${r.id}` },
      ]);
      keyboard.push([
        { text: '💬 Ответить', callback_data: `rev_reply_${r.id}` },
        { text: '👁 Подробнее', callback_data: `rev_view_${r.id}` },
      ]);
    }

    // Pagination nav
    const totalPages = Math.ceil(total / PER_PAGE);
    const nav = [];
    if (page > 0) nav.push({ text: '◀️ Назад', callback_data: `adm_rev_p_${filter}_${page - 1}` });
    if (page + 1 < totalPages) nav.push({ text: 'Вперёд ▶️', callback_data: `adm_rev_p_${filter}_${page + 1}` });
    if (nav.length) keyboard.push(nav);

    keyboard.push([{ text: '← Меню', callback_data: 'admin_menu' }]);

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: keyboard },
    });
  } catch (e) {
    console.error('[Bot] showAdminReviews:', e.message);
    // Gracefully handle missing reviews table
    return safeSend(chatId, 'Нет отзывов для показа', {
      reply_markup: { inline_keyboard: [[{ text: '← Меню', callback_data: 'admin_menu' }]] },
    });
  }
}

module.exports = {
  init,
  showAdminStats,
  showAdminModels,
  showAdminOrders,
  showAdminOrdersToday,
  showAdminOrdersWeek,
  showAdminReviews,
};
