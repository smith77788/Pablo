'use strict';
/**
 * Bot message strings — centralized for future localization.
 * Currently: Russian (RU). To add EN: export { RU, EN }.
 */
const RU = {
  // ─── Main Menu ─────────────────────────────────────────────────────────────
  MAIN_MENU_TITLE: '🏠 *Главное меню*',
  MAIN_MENU_SUBTITLE: 'Выберите действие:',

  // ─── Catalog ───────────────────────────────────────────────────────────────
  CATALOG_EMPTY: '😔 *Каталог пуст*\n\nМодели не найдены по вашему запросу\\.',
  CATALOG_TITLE: '💃 *Каталог моделей*',
  CATALOG_PAGE: (page, total) => `Страница ${page + 1} из ${Math.ceil(total / 5)}`,

  // ─── Model card ────────────────────────────────────────────────────────────
  MODEL_NOT_FOUND: '❌ Модель не найдена или недоступна\\.',
  MODEL_TOP_BADGE: '⭐ Топ\\-модель',
  MODEL_BOOK_BTN: '📋 Забронировать',
  MODEL_BACK_BTN: '← Назад в каталог',

  // ─── Booking ───────────────────────────────────────────────────────────────
  BOOKING_START: '📋 *Оформление заявки*\n\nШаг 1 из 4\\. Введите ваше имя:',
  BOOKING_NAME_PROMPT: 'Введите имя \\(минимум 2 символа\\):',
  BOOKING_PHONE_PROMPT: '📞 Введите номер телефона:',
  BOOKING_EMAIL_PROMPT: '📧 Введите email \\(или пропустите\\):',
  BOOKING_DATE_PROMPT: '📅 Введите желаемую дату мероприятия:',
  BOOKING_CANCEL_BTN: '❌ Отмена',
  BOOKING_SKIP_BTN: '⏩ Пропустить',
  BOOKING_BACK_BTN: '← Назад',
  BOOKING_SUCCESS: (id) => `✅ *Заявка \\#${id} успешно создана\\!*\n\nМенеджер свяжется с вами в ближайшее время\\.`,
  BOOKING_ERROR: '⚠️ Ошибка при создании заявки\\. Пожалуйста, попробуйте позже\\.',

  // ─── Quick booking ─────────────────────────────────────────────────────────
  QUICK_BOOKING_TITLE: '⚡ *Быстрая заявка*',
  QUICK_BOOKING_NAME_PROMPT: 'Введите ваше имя:',
  QUICK_BOOKING_PHONE_PROMPT: 'Введите номер телефона:',

  // ─── My orders ─────────────────────────────────────────────────────────────
  MY_ORDERS_EMPTY: '📋 *Мои заявки*\n\nУ вас пока нет заявок\\.',
  MY_ORDERS_TITLE: '📋 *Мои заявки*',

  // ─── Wishlist ──────────────────────────────────────────────────────────────
  WISHLIST_EMPTY: '❤️ *Список избранного пуст*\n\nДобавляйте понравившихся моделей кнопкой ❤️ в их профиле\\.',
  WISHLIST_TITLE: '❤️ *Избранные модели*',
  WISHLIST_ADDED: '❤️ Добавлено в избранное!',
  WISHLIST_REMOVED: '💔 Убрано из избранного',

  // ─── Reviews ───────────────────────────────────────────────────────────────
  REVIEWS_EMPTY: '⭐ *Отзывы*\n\nОтзывов пока нет\\.',
  REVIEWS_TITLE: '⭐ *Отзывы клиентов*',

  // ─── Profile ───────────────────────────────────────────────────────────────
  PROFILE_TITLE: '👤 *Ваш профиль*',
  PROFILE_ORDERS_TOTAL: (n) => `📋 Заявок всего: *${n}*`,
  PROFILE_ORDERS_COMPLETED: (n) => `✅ Завершено: *${n}*`,
  PROFILE_ORDERS_ACTIVE: (n) => `🔄 Активных: *${n}*`,

  // ─── Admin ─────────────────────────────────────────────────────────────────
  ADMIN_MENU_TITLE: '🔐 *Панель администратора*',
  ADMIN_NEW_ORDER: (id, name) => `📋 *Новая заявка \\#${id}*\nОт: ${name}`,
  ADMIN_STATUS_CHANGED: (id, status) => `📊 Заявка \\#${id}: статус изменён на *${status}*`,

  // ─── Errors ────────────────────────────────────────────────────────────────
  ERROR_GENERAL: '⚠️ Произошла ошибка\\. Попробуйте позже\\.',
  ERROR_PERMISSION: '⛔ У вас нет доступа к этой функции\\.',
  ERROR_NOT_FOUND: '❌ Запись не найдена\\.',

  // ─── Navigation ────────────────────────────────────────────────────────────
  BTN_MAIN_MENU: '🏠 Главная',
  BTN_CATALOG: '💃 Каталог',
  BTN_BACK: '← Назад',
  BTN_NEXT: 'Далее →',
  BTN_PREV: '← Предыдущая',
  BTN_CANCEL: '❌ Отмена',
  BTN_SKIP: '⏩ Пропустить',

  // ─── Status labels (for user-facing display) ───────────────────────────────
  STATUS: {
    new: '🆕 Новая',
    reviewing: '🔍 На рассмотрении',
    confirmed: '✅ Подтверждена',
    in_progress: '🔄 В процессе',
    completed: '🏁 Завершена',
    cancelled: '❌ Отменена',
  },

  // ─── Event type labels ─────────────────────────────────────────────────────
  EVENT_TYPE: {
    fashion_show: 'Показ мод',
    photo_shoot: 'Фотосессия',
    event: 'Мероприятие',
    commercial: 'Коммерческая съёмка',
    runway: 'Подиум',
    other: 'Другое',
  },
};

module.exports = { RU, strings: RU };
