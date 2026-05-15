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

    // Top-5 models by view count
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

    if (topViewed.length) {
      text += `\n*👁 Топ\\-5 по просмотрам:*\n`;
      topViewed.forEach((m, i) => {
        text += `  ${i+1}\\. ${esc(m.name)} — 👁 ${esc(String(m.view_count || 0))} просм\\., 📋 ${esc(String(m.order_count || 0))} заявок\n`;
      });
    }

    // Top-3 cities by order count
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

    if (topCities.length) {
      text += `\n*🏙 Топ\\-3 города:*\n`;
      topCities.forEach((c, i) => {
        text += `  ${medals[i] || (i+1+'.')} ${esc(c.city)} — ${esc(String(c.cnt))} заявок\n`;
      });
    }

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

    if (avgCycleDays !== null) {
      text += `*⏱ Средний цикл сделки:* ${esc(String(avgCycleDays))} дн\\.\n`;
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
        ? query('SELECT o.*,m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id=m.id WHERE o.status=? ORDER BY o.created_at DESC LIMIT 8 OFFSET ?', [safe, page*8])
        : query('SELECT o.*,m.name as model_name FROM orders o LEFT JOIN models m ON o.model_id=m.id ORDER BY o.created_at DESC LIMIT 8 OFFSET ?', [page*8])
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
      text += `${icon} *${o.order_number}* — ${esc(o.client_name)}\n`;
      const row = [{ text: `${o.order_number}  ·  ${o.client_name}`, callback_data: `adm_order_${o.id}` }];
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
    ];

    return safeSend(chatId, text, {
      parse_mode: 'MarkdownV2',
      reply_markup: { inline_keyboard: [
        filterRow1,
        filterRow2,
        ...btns,
        ...(nav.length ? [nav] : []),
        [{ text: '📦 Все новые → В работу', callback_data: 'adm_bulk_new_to_review' }],
        [{ text: '🔍 Найти заявку', callback_data: 'adm_search_order' },
         { text: '← Меню',         callback_data: 'admin_menu'        }],
      ]}
    });
  } catch (e) { console.error('[Bot] showAdminOrders:', e.message); }
}

module.exports = { init, showAdminStats, showAdminModels, showAdminOrders };
