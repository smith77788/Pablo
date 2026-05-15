'use strict';
/**
 * Admin Telegram keyboard builders.
 * These functions build inline_keyboard arrays for admin flows.
 */

const BTN_ADMIN_MENU = { text: '🔙 Меню', callback_data: 'admin_menu' };

/**
 * Confirmation keyboard for destructive actions.
 * @param {string} confirmCallback - callback for "Yes"
 * @param {string} cancelCallback - callback for "No"
 */
function confirmKeyboard(confirmCallback, cancelCallback = 'admin_menu') {
  return {
    inline_keyboard: [
      [
        { text: '✅ Да, подтверждаю', callback_data: confirmCallback },
        { text: '❌ Отмена', callback_data: cancelCallback },
      ],
    ],
  };
}

/**
 * Status change keyboard for orders.
 * @param {number} orderId
 */
function orderStatusKeyboard(orderId) {
  return {
    inline_keyboard: [
      [
        { text: '✅ Подтвердить', callback_data: `adm_status_${orderId}_confirmed` },
        { text: '✔️ Завершить', callback_data: `adm_status_${orderId}_completed` },
      ],
      [
        { text: '❌ Отменить', callback_data: `adm_status_${orderId}_cancelled` },
        { text: '💬 Сообщение', callback_data: `adm_msg_${orderId}` },
      ],
      [BTN_ADMIN_MENU],
    ],
  };
}

module.exports = {
  BTN_ADMIN_MENU,
  confirmKeyboard,
  orderStatusKeyboard,
};
