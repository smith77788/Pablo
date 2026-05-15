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

  // ─── Extended errors ───────────────────────────────────────────────────────
  errorGeneric: '❌ Произошла ошибка\\. Попробуйте позже\\.',
  errorUnauthorized: '❌ Нет доступа\\.',
  errorPhotoTooLarge: '❌ Фото слишком большое\\. Максимум 10 МБ\\.',
  errorInvalidDate: '❌ Неверный формат даты\\. Используйте ДД\\.ММ\\.ГГГГ',
  errorModelNotFound: '❌ Модель не найдена\\.',
  errorOrderNotFound: '❌ Заявка не найдена\\.',

  // ─── Booking validation errors ─────────────────────────────────────────────
  bookingErrorName: '❌ Введите имя \\(минимум 2 символа\\):',
  bookingErrorNameLong: '❌ Имя слишком длинное \\(максимум 100 символов\\):',
  bookingErrorPhone: '❌ Введите корректный номер телефона:',
  bookingErrorEmail: '❌ Неверный формат email\\. Пример: name@mail\\.ru\n\nВведите корректный email или нажмите «Пропустить»\\.',
  bookingErrorBudget: '❌ Введите корректный бюджет цифрами\\.',
  bookingErrorDatePast: '❌ Дата не может быть в прошлом\\.',

  // ─── Review strings ────────────────────────────────────────────────────────
  reviewThankYou: '🙏 Спасибо за ваш отзыв\\! Он будет опубликован после проверки\\.',
  reviewPrompt: '⭐ Как вы оцениваете работу с нами? Выберите рейтинг:',
  reviewTextPrompt: '📝 Напишите несколько слов о вашем опыте работы с нами:',
  reviewAlreadyLeft: '✅ Вы уже оставили отзыв\\.',
  reviewAlreadyLeftForOrder: '✅ Вы уже оставили отзыв для этой заявки\\.',

  // ─── Wishlist strings ──────────────────────────────────────────────────────
  wishlistAdded: '❤️ Добавлено в избранное!',
  wishlistRemoved: '💔 Убрано из избранного',
  wishlistEmpty: '❤️ *Ваш список избранного пуст*\n\nОткройте карточку модели и нажмите ❤️, чтобы добавить её в избранное\\.',
  wishlistUnavailable: '❤️ Список избранного временно недоступен\\.',

  // ─── Notifications ─────────────────────────────────────────────────────────
  notifNewOrder: '🆕 *Новая заявка\\!*',
  notifStatusChanged: '📋 Статус заявки изменён',
  notifNewMessage: '📩 *Новое сообщение*',

  // ─── Search strings ────────────────────────────────────────────────────────
  searchNoResults: '🔍 *Поиск моделей*\n\nПо вашему запросу ничего не найдено\\.\n\n_Попробуйте изменить или сбросить фильтры_',
  searchPrompt: '🔍 Введите имя или часть имени модели:',

  // ─── Admin notifications ───────────────────────────────────────────────────
  adminNewOrderAlert: '🆕 *Новая заявка\\!*',
  adminOrderClient: '👤 Клиент:',

  // ─── Broadcast strings ─────────────────────────────────────────────────────
  broadcastSending: '📤 Начинаю рассылку для *{count}* получателей\\.\\.\\.',
  broadcastDone: '📊 *Рассылка завершена\\!*',
  broadcastNoRecipients: '⚠️ Нет получателей для этого сегмента\\.',
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
    str = str.replace(new RegExp(`\\{${k}\\}`, 'g'), v);
  });
  return str;
}

module.exports = STRINGS;
module.exports.getString = getString;
