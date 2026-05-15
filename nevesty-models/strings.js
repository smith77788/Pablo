'use strict';

/**
 * Centralized bot text constants — prepared for localization.
 * All strings are MarkdownV2-escaped where required.
 * Usage: const STRINGS = require('./strings');
 *        const { getString } = require('./strings');
 */

const STRINGS = {
  // ─── Main menu ─────────────────────────────────────────────────────────────
  welcome: '💎 *Nevesty Models*',
  welcomeSubtitle: '_Агентство профессиональных моделей — Fashion, Commercial, Events_',
  mainMenuChoose: 'Выберите действие:',
  mainMenuCatalog: '💃 Каталог',
  mainMenuBooking: '📋 Создать заявку',
  mainMenuOrders: '📄 Мои заявки',
  mainMenuProfile: '👤 Профиль',
  mainMenuReviews: '⭐ Отзывы',
  mainMenuWishlist: '❤️ Избранное',
  mainMenuSearch: '🔍 Поиск',
  mainMenuContact: '📞 Связаться',
  mainMenuPricing: '💰 Прайс\\-лист',

  // ─── Booking flow ──────────────────────────────────────────────────────────
  bookingAskName: 'Введите ваше имя и фамилию:\n💡 Например: Алексей Смирнов',
  bookingAskPhone: 'Введите номер телефона:\n💡 Формат: \\+7 999 123\\-45\\-67',
  bookingAskEmail: 'Введите email \\(необязательно\\):\n💡 Например: client@mail\\.ru',
  bookingAskBudget: '💰 Укажите приблизительный бюджет:\n\n💡 _В рублях, например: 150000_',
  bookingAskDate: 'Введите желаемую дату мероприятия:\n💡 Формат: ДД\\.ММ\\.ГГГГ, например: 25\\.12\\.2025',

  // ─── Quick booking ─────────────────────────────────────────────────────────
  quickBookingTitle: '⚡ *Быстрая заявка*',
  quickBookingIntro: 'Менеджер свяжется с вами и уточнит все детали\\.',
  quickBookingStep1: '📝 Шаг 1/2 — Введите ваше имя:',
  quickBookingStep2: '📝 Шаг 2/2 — Введите номер телефона:\n_Пример: \\+7\\(999\\)123\\-45\\-67_',
  quickBookingAccepted: '⚡ *Заявка принята\\!*',

  // ─── Profile edit prompts ──────────────────────────────────────────────────
  profileEditName: '✏️ Введите ваше имя:',
  profileEditPhone: '📱 Введите номер телефона:',
  profileEditEmail: '📧 Введите новый email:',

  // ─── Status labels ─────────────────────────────────────────────────────────
  statusNew: '🆕 Новая',
  statusReviewing: '🔍 На рассмотрении',
  statusConfirmed: '✅ Подтверждена',
  statusInProgress: '▶️ В процессе',
  statusCompleted: '🏁 Завершена',
  statusCancelled: '❌ Отменена',

  // ─── Errors ────────────────────────────────────────────────────────────────
  errorGeneral: '❌ Произошла ошибка\\. Попробуйте позже\\.',
  errorSend: '❌ Ошибка при отправке\\.  Попробуйте позже\\.',
  errorNotFound: '❌ Запись не найдена.',
  errorAccessDenied: '⛔ Нет доступа.',
  errorNoAccess: '⛔ У вас нет доступа к этой функции\\.',
  errorInvalidName: '❌ Введите ваше имя (минимум 2 символа):',
  errorInvalidPhone: '❌ Введите корректный номер телефона (7-20 символов):',
  errorInvalidEmail: '❌ Введите корректный email (например: name@example.com):',
  errorNameTooLong: '❌ Имя слишком длинное (максимум 50 символов):',

  // ─── Success messages ──────────────────────────────────────────────────────
  successOrderCreated: '✅ Заявка создана\\!',
  successProfileUpdated: '✅ Профиль обновлён.',
  successNameUpdated: '✅ Имя обновлено:',
  successPhoneUpdated: '✅ Телефон обновлён:',

  // ─── Navigation buttons ────────────────────────────────────────────────────
  btnMainMenu: '🏠 Главное меню',
  btnBack: '← Назад',
  btnCancel: '❌ Отменить',
  btnSkip: '⏭ Пропустить',
  btnMyOrders: '📋 Мои заявки',
};

/**
 * Get a string by key with optional variable substitution.
 * @param {string} key - Key from STRINGS
 * @param {Object} vars - Variables to substitute (e.g. { name: 'Алексей' })
 * @returns {string}
 */
function getString(key, vars = {}) {
  let str = STRINGS[key] || key;
  Object.entries(vars).forEach(([k, v]) => {
    str = str.replace(`{${k}}`, v);
  });
  return str;
}

module.exports = STRINGS;
module.exports.getString = getString;
