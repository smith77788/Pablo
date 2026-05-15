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
  profileEditName: '✏️ Введите ваше имя:\n💡 _Например: Алексей Смирнов \\(минимум 2 символа\\)_',
  profileEditPhone: '📱 Введите номер телефона:\n💡 _Формат: \\+7 999 123\\-45\\-67_',
  profileEditEmail: '📧 Введите новый email:\n💡 _Например: name@mail\\.ru_',

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
  reviewsHeader: '⭐ *Отзывы клиентов*\n\n',
  reviewsEmpty: 'Пока нет отзывов\\.',
  reviewAskRating: '⭐ Оцените работу с нами от 1 до 5:',
  reviewAskText: 'Напишите ваш отзыв \\(или /skip\\):',
  reviewSaved: '✅ Спасибо за отзыв\\! Он будет опубликован после проверки\\.',

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
  broadcastNoClients: '❌ Нет клиентов для рассылки.',
  broadcastConfirm: '─────\n📤 Отправить рассылку?',
  broadcastCancelled: '❌ Рассылка отменена',

  // ─── Navigation button labels ──────────────────────────────────────────────
  btnMenu: '🏠 Меню',
  btnBackToMenu: '← Главное меню',
  btnBackToOrders: '← К заявкам',
  btnBackToOrder: '← К заявке',
  btnBackToCatalog: '← Назад в каталог',
  btnBackToModel: '← К карточке модели',
  btnBackToSearch: '← Назад к поиску',
  btnBackToProfile: '← Профиль',
  btnBackAdmin: '← Меню',
  btnConfirm: '✅ Подтвердить',
  btnClose: '❌ Закрыть',
  btnDelete: '🗑 Удалить',
  btnEdit: '✏️ Редактировать',
  btnForward: 'Вперёд →',
  btnPrev: '← Назад',
  btnNext: 'Вперёд ▶️',
  btnApply: '✅ Применить',
  btnReject: '❌ Отклонить',
  btnApprove: '✅ Одобрить',
  btnSend: '✅ Отправить сейчас',
  btnSendOrder: '✅ Отправить заявку',
  btnSchedule: '🕐 Запланировать',
  btnBookModel: '📋 Забронировать',
  btnQuickBook: '⚡ Быстрая заявка',
  btnManagerBook: '✨ Менеджер подберёт',
  btnRepeatOrder: '🔁 Повторить заявку',
  btnResetFilters: '✖️ Сбросить фильтры',
  btnNewSearch: '🔍 Новый поиск',
  btnCompare: '⚖️ Сравнить',
  btnShowComparison: '⚖️ Показать сравнение',
  btnClearList: '🗑 Очистить список',
  btnAddToFav: '❤️ В избранное',
  btnRemoveFromFav: '💔 Убрать из избранного',
  btnLeaveReview: '✍️ Оставить отзыв',
  btnPayNow: '💳 Перейти к оплате',
  btnArchiveModel: '📦 В архив',
  btnRestoreModel: '↩️ Восстановить',
  btnDeleteModel: '🗑 Удалить модель',
  btnAddModel: '✅ Добавить модель',
  btnGenerateAI: '🤖 AI описание',
  btnSkipCaption: '⏭ Пропустить подпись',
  btnSendNow: '▶ Отправить без фото',
  btnContinue: '▶ Продолжить',
  btnRestart: '🔄 Начать заново',
  btnMyProfile: '👤 Мой профиль',
  btnMyPoints: '💫 Мои баллы',
  btnMyAchievements: '🏆 Мои достижения',
  btnInviteFriend: '📤 Пригласить друга',
  btnDeleteAccount: '🗑 Удалить аккаунт',

  // ─── Empty states ──────────────────────────────────────────────────────────
  emptyClients: '👥 Клиентов пока нет\\.',
  emptyOrders: '📋 Заявок пока нет\\.',
  emptyModels: '📭 Моделей по выбранному фильтру нет\\.',
  emptyManagers: '👥 *Менеджеры*\n\nНет менеджеров в системе\\.',
  emptyAgentFeed: '🤖 Фид агентов пуст.',
  emptyActionLog: '📋 Журнал действий пуст\\.',
  emptyNoCities: '❌ Нет городов для выбора\\. Добавьте города в настройках или добавьте моделям города.',
  emptyNoPoints: '💫 У вас пока нет баллов лояльности\\.\n\nЗаработайте баллы, оформив первую заявку\\!',

  // ─── Comparison feature ────────────────────────────────────────────────────
  compareAlreadyAdded: '⚖️ Эта модель уже в списке сравнения.',
  compareMaxReached: '⚖️ Можно сравнивать не более 3 моделей.',
  compareEmpty: '⚖️ Список сравнения пуст\\. Добавьте модели из каталога\\.',
  compareEmptyShort: '⚖️ Список сравнения пуст\\.',

  // ─── Payment messages ──────────────────────────────────────────────────────
  paymentAlreadyPaid: '✅ Эта заявка уже оплачена\\.',
  paymentError: '❌ Ошибка при создании платежа\\. Обратитесь к менеджеру\\.',
  paymentInvoiceSent: '✅ Счёт выставлен клиенту',
  paymentConfirmed: '💰 Оплата зафиксирована\\.',
  paymentPending: '⏳ Статус оплаты: ожидаем\\.',

  // ─── Admin model management ────────────────────────────────────────────────
  adminModelAdded: '✅ Модель «{name}» добавлена!\n\nID: {id}',
  adminModelSaveError: '❌ Ошибка сохранения: {error}',
  adminModelArchived: '✅ Модель перемещена в архив\\.',
  adminModelRestored: '✅ Модель восстановлена\\.',
  adminModelCopied: '✅ Модель скопирована\\. ID новой карточки: *{id}*',
  adminAIGenerating: '🤖 Генерирую AI описание\\.\\.\\. Подождите 10\\-30 секунд\\.',
  adminAIError: '❌ Ошибка генерации AI описания\\.',
  adminCategoryUpdated: '✅ Категория обновлена!',
  adminExportError: '❌ Ошибка экспорта: {error}',
  adminTaskDone: '✅ Отмечено как выполнено.',
  adminTaskSkipped: 'Задача пропущена.',
  adminNoAdmins: '❌ Нет администраторов в базе.',
  adminNoModels: '❌ Нет доступных моделей в базе\\.',

  // ─── Client / order messages ───────────────────────────────────────────────
  clientNotFound: '❌ Клиент не найден или нет заявок\\.',
  clientBlocked: '⛔ Клиент заблокирован',
  clientUnblocked: '✅ Клиент разблокирован',
  orderActiveLimitReached: '⚠️ *Превышен лимит активных заявок*\\.\nПожалуйста, дождитесь завершения текущих заявок\\.',
  orderAlreadyLinked: '❌ Эта заявка уже привязана к другому чату.',
  orderAlreadyProcessed: '⚠️ Заявка уже обработана.',
  orderDataIncomplete: '❌ Данные неполные. Начните заново — /start',
  orderMessageSent: '✅ Отправлено клиенту {name}.',
  orderMessageSaved: '⚠️ Сообщение сохранено, но клиент ещё не подключил бот.',
  orderThankYou: '✅ Спасибо, что воспользовались нашими услугами\\!',

  // ─── AI / agent messages ───────────────────────────────────────────────────
  aiTasksLoadError: 'Ошибка загрузки AI-задач.',
  aiFactoryStarting: '🔄 Запускаю цикл AI Factory...\n\nРезультат придёт через 1-2 минуты.',
  aiHealthCheck: '🌿 Запускаю проверку организма...\n\nРезультаты придут через 1-2 минуты.',

  // ─── Language / settings ──────────────────────────────────────────────────
  langNotAvailable: '🌐 Мультиязычность пока недоступна\\.\n\nСледите за обновлениями\\!',
  langChoose: '🌐 *Язык интерфейса*\n\nВыберите язык:',
  deleteAccountError: '⚠️ Не удалось удалить аккаунт\\. Попробуйте позже\\.',

  // ─── Admin-for-model command guard ────────────────────────────────────────
  warnAdminIsNotModel: '⚠️ Эта команда предназначена для моделей, а не для администраторов\\.',
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
