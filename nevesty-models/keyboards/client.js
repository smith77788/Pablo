'use strict';
/**
 * Client-facing Telegram keyboard builders.
 * These functions build inline_keyboard arrays for client flows.
 * Currently these live in bot.js; this module is the future extraction target.
 */

// Standard "back to main menu" button
const BTN_MAIN_MENU = { text: '🏠 Главное меню', callback_data: 'main_menu' };
const BTN_BACK = { text: '◀️ Назад', callback_data: 'back' };
const BTN_CANCEL = { text: '❌ Отмена', callback_data: 'cancel' };

// Quick-access buttons used across flows
const BTN_CONTACT_MANAGER = { text: '📞 Написать менеджеру', callback_data: 'msg_manager_start' };
const BTN_CATALOG = { text: '📋 Каталог моделей', callback_data: 'cat_main' };
const BTN_MY_ORDERS = { text: '📂 Мои заявки', callback_data: 'my_orders' };

/**
 * Star rating keyboard for reviews.
 * @param {number} orderId - The order ID being reviewed
 * @returns {object} Telegram inline_keyboard
 */
function ratingKeyboard(orderId) {
  return {
    inline_keyboard: [
      [1, 2, 3, 4, 5].map(n => ({
        text: '⭐'.repeat(n),
        callback_data: `rev_rate_${n}_${orderId}`,
      })),
      [BTN_CANCEL],
    ],
  };
}

/**
 * Pagination keyboard for lists.
 * @param {number} page - Current page (0-indexed)
 * @param {number} total - Total items
 * @param {number} perPage - Items per page
 * @param {string} prefix - callback_data prefix (e.g., 'cat_page')
 * @param {Array} extraButtons - Additional button rows to append
 */
function paginationKeyboard(page, total, perPage, prefix, extraButtons = []) {
  const nav = [];
  if (page > 0) nav.push({ text: '◀️', callback_data: `${prefix}_${page - 1}` });
  if ((page + 1) * perPage < total) nav.push({ text: '▶️', callback_data: `${prefix}_${page + 1}` });
  const rows = [...extraButtons];
  if (nav.length) rows.push(nav);
  rows.push([BTN_MAIN_MENU]);
  return { inline_keyboard: rows };
}

/**
 * Booking event type selection keyboard.
 */
function eventTypeKeyboard() {
  return {
    inline_keyboard: [
      [
        { text: '📸 Фотосессия', callback_data: 'bk_et_photo_shoot' },
        { text: '🎬 Видеосъемка', callback_data: 'bk_et_video_shoot' },
      ],
      [
        { text: '🏢 Мероприятие', callback_data: 'bk_et_event' },
        { text: '🎤 Промо', callback_data: 'bk_et_promo' },
      ],
      [
        { text: '👔 Показ мод', callback_data: 'bk_et_fashion_show' },
        { text: '💼 Другое', callback_data: 'bk_et_other' },
      ],
      [BTN_BACK],
    ],
  };
}

module.exports = {
  BTN_MAIN_MENU,
  BTN_BACK,
  BTN_CANCEL,
  BTN_CONTACT_MANAGER,
  BTN_CATALOG,
  BTN_MY_ORDERS,
  ratingKeyboard,
  paginationKeyboard,
  eventTypeKeyboard,
};
