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
  errorSend: '❌ Ошибка при отправке\\. Попробуйте позже\\.',
  errorNotFound: '❌ Запись не найдена\\.',
  errorAccessDenied: '⛔ Нет доступа\\.',
  errorAccessDeniedShort: '⛔ Нет доступа',
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
  errorModelNotFoundPlain: '❌ Модель не найдена.',
  errorOrderNotFound: '❌ Заявка не найдена\\.',

  // ─── Booking validation errors ─────────────────────────────────────────────
  bookingErrorName: '❌ Введите имя \\(минимум 2 символа\\):',
  bookingErrorNameLong: '❌ Имя слишком длинное \\(максимум 100 символов\\):',
  bookingErrorPhone: '❌ Введите корректный номер телефона:',
  bookingErrorEmail:
    '❌ Неверный формат email\\. Пример: name@mail\\.ru\n\nВведите корректный email или нажмите «Пропустить»\\.',
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
  wishlistEmpty:
    '❤️ *Ваш список избранного пуст*\n\nОткройте карточку модели и нажмите ❤️, чтобы добавить её в избранное\\.',
  wishlistUnavailable: '❤️ Список избранного временно недоступен\\.',

  // ─── Notifications ─────────────────────────────────────────────────────────
  notifNewOrder: '🆕 *Новая заявка\\!*',
  notifStatusChanged: '📋 Статус заявки изменён',
  notifNewMessage: '📩 *Новое сообщение*',

  // ─── Search strings ────────────────────────────────────────────────────────
  searchNoResults:
    '🔍 *Поиск моделей*\n\nПо вашему запросу ничего не найдено\\.\n\n_Попробуйте изменить или сбросить фильтры_',
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
  btnBackToSettings: '← Настройки',
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
  aiChatWaiting: '⏳ Думаю\\.\\.\\.',
  aiChatError: '❌ Не удалось получить ответ\\. Попробуйте позже\\.',
  forecastInsufficient: 'Недостаточно данных для прогноза',

  // ─── Language / settings ──────────────────────────────────────────────────
  langNotAvailable: '🌐 Мультиязычность пока недоступна\\.\n\nСледите за обновлениями\\!',
  langChoose: '🌐 *Язык интерфейса*\n\nВыберите язык:',
  deleteAccountError: '⚠️ Не удалось удалить аккаунт\\. Попробуйте позже\\.',

  // ─── Admin-for-model command guard ────────────────────────────────────────
  warnAdminIsNotModel: '⚠️ Эта команда предназначена для моделей, а не для администраторов\\.',

  // ─── Wishlist (extended) ───────────────────────────────────────────────────
  wishlistTitle: '❤️ *Избранное*',

  // ─── Profile ───────────────────────────────────────────────────────────────
  profileTitle: '👤 *Ваш профиль*',
  profileNoOrders: '_Заявок пока нет_',
  profileEditSuccess: '✅ Данные обновлены',

  // ─── Reviews (extended) ────────────────────────────────────────────────────
  reviewsTitle: '⭐ *Отзывы клиентов*',
  reviewSubmitSuccess: '✅ Спасибо за отзыв\\! Он будет опубликован после проверки\\.',
  reviewThanks: '🙏 Спасибо за оценку\\!',

  // ─── Search (extended) ─────────────────────────────────────────────────────
  searchTitle: '🔍 *Поиск моделей*',
  searchResults: n => `Найдено: *${n}* моделей`,

  // ─── Orders (extended) ─────────────────────────────────────────────────────
  ordersTitle: '📄 *Мои заявки*',
  ordersEmpty: '_У вас ещё нет заявок_\\.\n\nВоспользуйтесь каталогом для бронирования моделей\\.',
  orderCancelled: '✅ Заявка отменена',
  orderCancelConfirm: '❓ Вы уверены, что хотите отменить заявку?',

  // ─── FAQ ───────────────────────────────────────────────────────────────────
  faqTitle: '❓ *Часто задаваемые вопросы*',
  faqEmpty: '_FAQ пока пуст_',

  // ─── Cancel action ─────────────────────────────────────────────────────────
  cancelActionDone: '✅ Действие отменено\\. Возвращаю в главное меню\\.',
  cancelActionNone: 'ℹ️ Активного действия нет\\. Вы уже в главном меню\\.',

  // ─── Contextual error messages (with hints) ────────────────────────────────
  errorSaveRetry: '❌ Не удалось сохранить\\. Попробуйте через минуту или напишите /start',
  errorUpdateData: '⚠️ Не удалось обновить данные\\. Попробуйте ещё раз\\.',
  errorSessionLost: '❌ Данные сессии утеряны\\. Начните заново — /start',
  errorLoadRetry: '⚠️ Не удалось загрузить данные\\. Попробуйте ещё раз\\.',
  errorReviewLoad: '⚠️ Не удалось загрузить отзыв\\. Попробуйте ещё раз\\.',
  errorAITasksLoad: '⚠️ Не удалось загрузить AI\\-задачи\\. Попробуйте ещё раз или вернитесь в меню\\.',

  // ─── Errors (extended) ─────────────────────────────────────────────────────
  errorTooManyRequests: '⏳ Слишком много запросов\\. Подождите немного\\.',

  // ─── Admin (extended) ──────────────────────────────────────────────────────
  adminModelUpdated: '✅ Модель обновлена',
  adminModelDeleted: '✅ Модель удалена',
  adminSettingsSaved: '✅ Настройки сохранены',
  adminOrderUpdated: status => `✅ Статус изменён на: *${status}*`,

  // ─── Booking step headers (step title arguments for stepHeader) ────────────
  bookingStepSelectModel: 'Выберите модель',
  bookingStepEventDetails: 'Детали мероприятия',
  bookingStepContacts: 'Ваши контакты',

  // ─── Booking step body prompts ─────────────────────────────────────────────
  bookingSelectModelHint: 'Выберите из списка или нажмите «Менеджер подберёт»:',
  bookingSelectEventType: 'Выберите тип мероприятия:',
  bookingSelectDuration: 'Выберите продолжительность мероприятия:',
  bookingAskLocation:
    'Введите место проведения \\(город, адрес\\):\n_Примеры: Москва МКАД, ул\\. Арбат 15, студия в Москве_\n\n_/cancel — отменить_',
  bookingAskLocationShort: '❌ Введите место проведения:',
  bookingAskBudgetFull:
    'Укажите бюджет \\(необязательно\\):\n💡 Укажите бюджет в рублях\n_Примеры: 15000, 25000\\-40000, «от 30000»_',
  bookingAskComments: 'Дополнительные пожелания \\(необязательно\\):',
  bookingAskTelegram: 'Введите Telegram username для связи \\(необязательно\\):\n_Пример: @username_',
  bookingManagerLabel: 'Менеджер подберёт',

  // ─── Broadcast segments ────────────────────────────────────────────────────
  segmentAll: '👥 Все клиенты',
  segmentCompleted: '✅ Завершившие заявку',
  segmentActive: '▶️ Активные клиенты',

  // ─── Order status flow messages ────────────────────────────────────────────
  orderStatusReviewing: orderNum => `🔍 *Заявка ${orderNum} принята в работу\\.*\n\nМы изучаем ваш запрос\\.`,
  orderStatusInProgress: orderNum => `▶️ *Заявка ${orderNum} выполняется\\.*`,

  // ─── Review filter labels ──────────────────────────────────────────────────
  reviewFilterPending: 'ожидающих одобрения',
  reviewFilterApproved: 'одобренных',
  reviewFilterAll: 'отзывов',
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

// ─── Multilingual API ─────────────────────────────────────────────────────────

/**
 * Multilingual string dictionary.
 * Keys follow snake_case convention; values may contain {placeholder} tokens.
 * Usage: t('booking_success', 'ru', { number: '42' })
 */
const I18N = {
  ru: {
    // ── Navigation ────────────────────────────────────────────────────────────
    main_menu: '🏠 Главное меню',
    admin_menu: '⚙️ Меню администратора',
    back: '← Назад',
    back_to_catalog: '← Назад в каталог',
    back_to_menu: '← Меню',
    cancel: '❌ Отмена',
    continue: '▶️ Продолжить',
    next: '▶️',
    prev: '◀️',

    // ── Common errors ─────────────────────────────────────────────────────────
    error_generic: '❌ Произошла ошибка. Попробуйте позже.',
    error_not_found: '❌ Не найдено.',
    error_access_denied: '⛔ Нет доступа',
    error_session_expired:
      '⏰ Сессия истекла. Действие отменено.\n\nНапишите /start или нажмите кнопку ниже, чтобы продолжить.',
    error_booking_failed: '❌ Не удалось создать заявку. Попробуйте позже или напишите менеджеру.',
    error_save: '❌ Ошибка сохранения: {message}',
    error_export: '❌ Ошибка экспорта: {message}',
    error_no_access: '⛔ Нет доступа',
    error_superadmin_delete: '❌ Нельзя удалить суперадмина.',
    error_ai_failed: '❌ Не удалось запустить AI: {message}',
    error_load_points: '❌ Не удалось загрузить баллы. Попробуйте позже.',
    error_ai_estimate: '⚠️ Ошибка AI-оценки. Попробуйте позже или воспользуйтесь калькулятором.',
    error_no_answer: '❌ Не удалось получить ответ. Попробуйте позже или напишите менеджеру.',
    error_no_models_with_photo: '❌ Нет доступных моделей с фото.',
    error_no_cities: '❌ Нет городов для выбора. Добавьте города в настройках или добавьте моделям города.',

    // ── Waiting / loading ─────────────────────────────────────────────────────
    loading_ai: '🤖 Генерирую AI описание... Подождите 10-30 секунд.',
    loading_thinking: '⏳ Думаю...',
    loading_csv: '⏳ Формирую CSV...',

    // ── Greeting / start ──────────────────────────────────────────────────────
    app_name: '💎 Nevesty Models',
    app_tagline: 'Агентство профессиональных моделей — Fashion, Commercial, Events',
    menu_activated: '💎 Nevesty Models — меню активировано',
    menu_choose_action: 'Выберите действие:',
    choose_language: 'Выберите язык / Choose language / Оберіть мову:',

    // ── Catalog ───────────────────────────────────────────────────────────────
    catalog_title: '📋 Каталог моделей',
    catalog_empty: '😔 Каталог пуст. Попробуйте позже.',
    catalog_page: 'Страница {page} из {total}',
    catalog_found: 'Найдено: {count}',
    model_not_found: '❌ Модель не найдена.',
    model_not_found_deleted: '❌ Модель не найдена. Возможно, она была удалена.',
    model_not_found_or_deleted: '❌ Модель не найдена или удалена.',
    compare_empty: '⚖️ Список сравнения пуст. Добавьте модели из каталога.',
    compare_added: '✅ {name} добавлена в сравнение ({count}/3)',
    compare_already_added: '⚖️ Эта модель уже в списке сравнения.',
    compare_max_reached: '⚖️ Можно сравнивать не более 3 моделей.',
    model_available: '✅ Доступна',
    model_busy: '⛔ Занята на этой неделе',

    // ── Booking ───────────────────────────────────────────────────────────────
    booking_start: '📝 Новая заявка',
    booking_submit: '✅ Отправить заявку',
    booking_success_auto:
      '🎉 Заявка подтверждена!\n\nНомер: {number}\n\nВаша заявка автоматически подтверждена. Менеджер свяжется с вами для уточнения деталей.',
    booking_success_pending:
      '🎉 Заявка принята!\n\nНомер: {number}\n\nМенеджер свяжется с вами в течение 1 часа для подтверждения.\n\nСохраните номер — по нему можно проверить статус в любое время.',
    booking_accepted: '✅ Ваша заявка принята! Менеджер свяжется с вами в ближайшее время.',
    booking_cancelled: '❌ Бронирование отменено.',
    booking_order_not_found: '❌ Заявка {number} не найдена. Проверьте номер.',
    booking_order_not_found_id: '❌ Заявка #{id} не найдена.',
    booking_order_already_paid: '✅ Эта заявка уже оплачена.',
    booking_invalid_number: '❌ Неверный номер заявки. Введите положительное целое число.',
    booking_session_inactive:
      '⏰ Сессия бронирования неактивна более 30 минут.\n\nПродолжить с того же места или начать заново?',
    booking_resume: '✅ Продолжаем бронирование!\n\nОтвечайте на последний вопрос.',
    booking_time_extended: '✅ Хорошо! Время сессии продлено. Продолжайте заполнение.',
    booking_continue: '✅ Хорошо, продолжаем с того места где остановились.',
    booking_model_selected: '✅ Модель выбрана: {name}',
    booking_duration_set: '✅ Длительность: {duration} ч. (из калькулятора)',
    my_orders_empty: 'Ваши заявки\n\nУ вас пока нет заявок. Оформите первую прямо сейчас!',
    order_status_check_prompt: '🔍 Проверка статуса заявки\n\nВведите номер вашей заявки (например: NM-2025-ABCDEF):',
    order_status_current: 'ℹ️ Статус заявки уже: {status}',
    promo_question:
      '🏷 У вас есть промокод?\n\nНеобязательно. Введите промокод, чтобы получить скидку, или пропустите этот шаг.',
    promo_applied: '✅ Применён промокод: {code}',
    promo_enter: '🏷 Введите промокод\n\nВведите код и нажмите отправить.',
    order_linked_other_chat: '❌ Эта заявка уже привязана к другому чату.',

    // ── Reviews ───────────────────────────────────────────────────────────────
    review_submitted: '✅ Спасибо за отзыв! Он появится на сайте после проверки.',
    review_approved: '✅ Ваш отзыв одобрен и опубликован. Спасибо!',
    review_thanks: '👍 Спасибо за отзыв! Рады помочь.',
    review_new_admin: '⭐ Новый отзыв от {name}!\nОценка: {rating}⭐\n\nПерейдите в раздел отзывов для модерации.',
    review_already_left: '✅ Вы уже оставили отзыв для этой заявки.',

    // ── Admin — orders ────────────────────────────────────────────────────────
    admin_order_status_updated: 'Статус обновлён: {label}',
    admin_note_added: '✅ Заметка добавлена.',
    admin_reply_sent: '✅ Ответ отправлен клиенту.',
    admin_manager_assigned: '✅ Менеджер {name} назначен на заявку.',
    admin_manager_unassigned: '✅ Назначение менеджера снято.',
    admin_order_bulk_reviewing: '✅ Переведено {count} заявок в статус «На рассмотрении»',

    // ── Admin — models ────────────────────────────────────────────────────────
    admin_model_added: '✅ Модель «{name}» добавлена!\n\nID: {id}',
    admin_model_updated: '✅ Данные обновлены',
    admin_model_category_updated: '✅ Категория обновлена!',
    admin_model_archived: '✅ Модель перемещена в архив.',
    admin_model_restored: '✅ Модель восстановлена.',
    admin_model_copied: '✅ Модель скопирована. ID новой карточки: {id}',
    admin_model_deleted: '✅ Модель «{name}» удалена.',

    // ── Admin — broadcast ─────────────────────────────────────────────────────
    admin_broadcast_sent:
      '📊 Итог рассылки:\n\n✅ Доставлено: {sent}\n❌ Ошибки: {failed}\n📬 Всего: {total}\n🎯 Аудитория: {segment}\n⏱ Время: {duration}с',
    admin_broadcast_no_clients: '❌ Нет клиентов для рассылки.',
    admin_broadcast_text_received: '✅ Текст получен!\n\nДобавить фото к рассылке?',

    // ── Admin — managers ──────────────────────────────────────────────────────
    admin_manager_removed: '✅ Менеджер {username} удалён из системы.',
    admin_manager_not_found: '❌ Менеджер не найден.',
    admin_manager_added: '✅ Менеджер добавлен',

    // ── Admin — templates / FAQ ───────────────────────────────────────────────
    admin_template_updated: '✅ Шаблон обновлён!',
    admin_faq_added: '✅ FAQ добавлен!\n\n{question}\n{category}',

    // ── Admin — Instagram / social ────────────────────────────────────────────
    admin_post_created: '✅ Пост создан!',
    admin_post_published_ig: '✅ Пост #{id} опубликован в Instagram!',
    admin_ig_disconnected: '✅ Instagram API отключён. Данные очищены.',
    admin_post_error: '❌ Ошибка при создании поста.',
    admin_ai_error: '❌ Ошибка генерации AI описания.',

    // ── Settings ──────────────────────────────────────────────────────────────
    setting_saved: '✅ Настройка сохранена',
    multilang_unavailable: '🌐 Мультиязычность пока недоступна.\n\nСледите за обновлениями!',

    // ── Contact / support ─────────────────────────────────────────────────────
    contact_manager: '💬 Написать менеджеру',
    contact_manager_full: '💬 Связаться с менеджером',
    support_prompt: '💬 Поддержка\n\nНапишите ваш вопрос и мы ответим в течение 15 минут.',
    manager_hours_default: 'Пн-Пт: 10:00-20:00',
    invoice_sent: '✅ Счёт выставлен клиенту. Ссылка отправлена.',

    // ── Payment / points ──────────────────────────────────────────────────────
    payment_status_waiting: '⏳ Статус оплаты: ожидаем.',
    payment_confirmed: '💰 Оплата зафиксирована.',

    // ── Model registration ────────────────────────────────────────────────────
    model_register_phone:
      '📱 Регистрация модели\n\nВведите ваш номер телефона в формате +7XXXXXXXXXX или 8XXXXXXXXXX:\n\nНомер должен совпадать с тем, который зарегистрирован в базе агентства.',

    // ── Session / timeout ─────────────────────────────────────────────────────
    session_timeout: '⏰ Время ожидания истекло.\n\nДействие сброшено. Напишите /start чтобы вернуться в меню.',
    session_restart: '🔄 Начать заново',
    session_expired_reconnect: '❌ Сессия истекла. Начните подключение заново.',
    session_booking_timeout:
      '⏰ Сессия бронирования неактивна более 30 минут.\n\nПродолжить с того же места или начать заново?',

    // ── Status labels ─────────────────────────────────────────────────────────
    status_new: '🆕 Новая',
    status_reviewing: '🔍 На рассмотрении',
    status_confirmed: '✅ Подтверждена',
    status_in_progress: '▶️ В процессе',
    status_completed: '🏁 Завершена',
    status_cancelled: '❌ Отменена',

    // ── Client management ─────────────────────────────────────────────────────
    client_blocked: '⛔ Клиент заблокирован',
    client_unblocked: '✅ Клиент разблокирован',
    client_not_found: '❌ Клиент не найден или нет заявок.',
    order_message_sent: '✅ Отправлено клиенту {name}.',

    // ── Wishlist ──────────────────────────────────────────────────────────────
    wishlist_added: '❤️ Добавлено в избранное!',
    wishlist_removed: '💔 Убрано из избранного',
    wishlist_empty:
      '❤️ Ваш список избранного пуст\n\nОткройте карточку модели и нажмите ❤️, чтобы добавить её в избранное.',

    // ── Search ────────────────────────────────────────────────────────────────
    search_prompt: '🔍 Введите имя или часть имени модели:',
    search_no_results:
      '🔍 Поиск моделей\n\nПо вашему запросу ничего не найдено.\n\nПопробуйте изменить или сбросить фильтры',

    // ── Export / CSV ──────────────────────────────────────────────────────────
    export_loading: '⏳ Формирую CSV...',
    export_error: '❌ Ошибка экспорта: {message}',
  },

  en: {
    // ── Navigation ────────────────────────────────────────────────────────────
    main_menu: '🏠 Main Menu',
    admin_menu: '⚙️ Admin Menu',
    back: '← Back',
    back_to_catalog: '← Back to Catalog',
    back_to_menu: '← Menu',
    cancel: '❌ Cancel',
    continue: '▶️ Continue',
    next: '▶️',
    prev: '◀️',

    // ── Common errors ─────────────────────────────────────────────────────────
    error_generic: '❌ An error occurred. Please try again.',
    error_not_found: '❌ Not found.',
    error_access_denied: '⛔ Access denied',
    error_session_expired:
      '⏰ Session expired. Action cancelled.\n\nType /start or press the button below to continue.',
    error_booking_failed: '❌ Failed to create booking. Please try again or contact the manager.',
    error_save: '❌ Save error: {message}',
    error_export: '❌ Export error: {message}',
    error_no_access: '⛔ Access denied',
    error_superadmin_delete: '❌ Cannot delete superadmin.',
    error_ai_failed: '❌ Failed to start AI: {message}',
    error_load_points: '❌ Failed to load points. Please try again.',
    error_ai_estimate: '⚠️ AI estimation error. Try again or use the calculator.',
    error_no_answer: '❌ Failed to get a response. Try again or contact the manager.',
    error_no_models_with_photo: '❌ No models with photos available.',
    error_no_cities: '❌ No cities available. Add cities in settings or assign cities to models.',

    // ── Waiting / loading ─────────────────────────────────────────────────────
    loading_ai: '🤖 Generating AI description... Please wait 10-30 seconds.',
    loading_thinking: '⏳ Thinking...',
    loading_csv: '⏳ Generating CSV...',

    // ── Greeting / start ──────────────────────────────────────────────────────
    app_name: '💎 Nevesty Models',
    app_tagline: 'Professional modeling agency — Fashion, Commercial, Events',
    menu_activated: '💎 Nevesty Models — menu activated',
    menu_choose_action: 'Choose an action:',
    choose_language: 'Выберите язык / Choose language / Оберіть мову:',

    // ── Catalog ───────────────────────────────────────────────────────────────
    catalog_title: '📋 Model Catalog',
    catalog_empty: '😔 Catalog is empty. Please try again later.',
    catalog_page: 'Page {page} of {total}',
    catalog_found: 'Found: {count}',
    model_not_found: '❌ Model not found.',
    model_not_found_deleted: '❌ Model not found. It may have been deleted.',
    model_not_found_or_deleted: '❌ Model not found or deleted.',
    compare_empty: '⚖️ Comparison list is empty. Add models from the catalog.',
    compare_added: '✅ {name} added to comparison ({count}/3)',
    compare_already_added: '⚖️ This model is already in the comparison list.',
    compare_max_reached: '⚖️ You can compare at most 3 models.',
    model_available: '✅ Available',
    model_busy: '⛔ Busy this week',

    // ── Booking ───────────────────────────────────────────────────────────────
    booking_start: '📝 New Booking',
    booking_submit: '✅ Submit Booking',
    booking_success_auto:
      '🎉 Booking confirmed!\n\nNumber: {number}\n\nYour booking has been automatically confirmed. The manager will contact you to clarify details.',
    booking_success_pending:
      '🎉 Booking accepted!\n\nNumber: {number}\n\nA manager will contact you within 1 hour to confirm.\n\nSave this number — you can check the status anytime.',
    booking_accepted: '✅ Your booking is accepted! A manager will contact you shortly.',
    booking_cancelled: '❌ Booking cancelled.',
    booking_order_not_found: '❌ Booking {number} not found. Please check the number.',
    booking_order_not_found_id: '❌ Booking #{id} not found.',
    booking_order_already_paid: '✅ This booking is already paid.',
    booking_invalid_number: '❌ Invalid booking number. Enter a positive integer.',
    booking_session_inactive:
      '⏰ Booking session has been inactive for over 30 minutes.\n\nContinue from where you left off or start over?',
    booking_resume: '✅ Continuing booking!\n\nAnswer the last question.',
    booking_time_extended: '✅ Great! Session extended. Continue filling out the form.',
    booking_continue: '✅ OK, continuing from where we left off.',
    booking_model_selected: '✅ Model selected: {name}',
    booking_duration_set: '✅ Duration: {duration} h. (from calculator)',
    my_orders_empty: 'Your Bookings\n\nYou have no bookings yet. Make your first one now!',
    order_status_check_prompt: '🔍 Check Booking Status\n\nEnter your booking number (e.g. NM-2025-ABCDEF):',
    order_status_current: 'ℹ️ Booking status is already: {status}',
    promo_question: '🏷 Do you have a promo code?\n\nOptional. Enter a promo code for a discount, or skip this step.',
    promo_applied: '✅ Promo code applied: {code}',
    promo_enter: '🏷 Enter promo code\n\nEnter the code and press send.',
    order_linked_other_chat: '❌ This booking is already linked to another chat.',

    // ── Reviews ───────────────────────────────────────────────────────────────
    review_submitted: '✅ Thank you for your review! It will appear on the site after moderation.',
    review_approved: '✅ Your review has been approved and published. Thank you!',
    review_thanks: '👍 Thank you for your review! Happy to help.',
    review_new_admin: '⭐ New review from {name}!\nRating: {rating}⭐\n\nGo to reviews section to moderate.',
    review_already_left: '✅ You have already left a review for this booking.',

    // ── Admin — orders ────────────────────────────────────────────────────────
    admin_order_status_updated: 'Status updated: {label}',
    admin_note_added: '✅ Note added.',
    admin_reply_sent: '✅ Reply sent to client.',
    admin_manager_assigned: '✅ Manager {name} assigned to booking.',
    admin_manager_unassigned: '✅ Manager assignment removed.',
    admin_order_bulk_reviewing: '✅ {count} bookings moved to "Under Review"',

    // ── Admin — models ────────────────────────────────────────────────────────
    admin_model_added: '✅ Model "{name}" added!\n\nID: {id}',
    admin_model_updated: '✅ Data updated',
    admin_model_category_updated: '✅ Category updated!',
    admin_model_archived: '✅ Model moved to archive.',
    admin_model_restored: '✅ Model restored.',
    admin_model_copied: '✅ Model copied. New card ID: {id}',
    admin_model_deleted: '✅ Model "{name}" deleted.',

    // ── Admin — broadcast ─────────────────────────────────────────────────────
    admin_broadcast_sent:
      '📊 Broadcast results:\n\n✅ Delivered: {sent}\n❌ Errors: {failed}\n📬 Total: {total}\n🎯 Audience: {segment}\n⏱ Time: {duration}s',
    admin_broadcast_no_clients: '❌ No clients for broadcast.',
    admin_broadcast_text_received: '✅ Text received!\n\nAdd a photo to the broadcast?',

    // ── Admin — managers ──────────────────────────────────────────────────────
    admin_manager_removed: '✅ Manager {username} removed from the system.',
    admin_manager_not_found: '❌ Manager not found.',
    admin_manager_added: '✅ Manager added',

    // ── Admin — templates / FAQ ───────────────────────────────────────────────
    admin_template_updated: '✅ Template updated!',
    admin_faq_added: '✅ FAQ added!\n\n{question}\n{category}',

    // ── Admin — Instagram / social ────────────────────────────────────────────
    admin_post_created: '✅ Post created!',
    admin_post_published_ig: '✅ Post #{id} published to Instagram!',
    admin_ig_disconnected: '✅ Instagram API disconnected. Data cleared.',
    admin_post_error: '❌ Error creating post.',
    admin_ai_error: '❌ Error generating AI description.',

    // ── Settings ──────────────────────────────────────────────────────────────
    setting_saved: '✅ Setting saved',
    multilang_unavailable: '🌐 Multilingual support is not yet available.\n\nStay tuned!',

    // ── Contact / support ─────────────────────────────────────────────────────
    contact_manager: '💬 Contact Manager',
    contact_manager_full: '💬 Contact Manager',
    support_prompt: '💬 Support\n\nWrite your question and we will reply within 15 minutes.',
    manager_hours_default: 'Mon-Fri: 10:00-20:00',
    invoice_sent: '✅ Invoice sent to client. Link delivered.',

    // ── Payment / points ──────────────────────────────────────────────────────
    payment_status_waiting: '⏳ Payment status: pending.',
    payment_confirmed: '💰 Payment confirmed.',

    // ── Model registration ────────────────────────────────────────────────────
    model_register_phone:
      '📱 Model Registration\n\nEnter your phone number in the format +7XXXXXXXXXX:\n\nThe number must match the one registered in the agency database.',

    // ── Session / timeout ─────────────────────────────────────────────────────
    session_timeout: '⏰ Wait time expired.\n\nAction reset. Type /start to return to the menu.',
    session_restart: '🔄 Start Over',
    session_expired_reconnect: '❌ Session expired. Please reconnect.',
    session_booking_timeout:
      '⏰ Booking session has been inactive for over 30 minutes.\n\nContinue from where you left off or start over?',

    // ── Status labels ─────────────────────────────────────────────────────────
    status_new: '🆕 New',
    status_reviewing: '🔍 Under Review',
    status_confirmed: '✅ Confirmed',
    status_in_progress: '▶️ In Progress',
    status_completed: '🏁 Completed',
    status_cancelled: '❌ Cancelled',

    // ── Client management ─────────────────────────────────────────────────────
    client_blocked: '⛔ Client blocked',
    client_unblocked: '✅ Client unblocked',
    client_not_found: '❌ Client not found or has no bookings.',
    order_message_sent: '✅ Sent to client {name}.',

    // ── Wishlist ──────────────────────────────────────────────────────────────
    wishlist_added: '❤️ Added to favourites!',
    wishlist_removed: '💔 Removed from favourites',
    wishlist_empty: '❤️ Your favourites list is empty\n\nOpen a model card and press ❤️ to add it to favourites.',

    // ── Search ────────────────────────────────────────────────────────────────
    search_prompt: '🔍 Enter a model name or part of a name:',
    search_no_results: '🔍 Model Search\n\nNo results found for your query.\n\nTry changing or resetting filters',

    // ── Export / CSV ──────────────────────────────────────────────────────────
    export_loading: '⏳ Generating CSV...',
    export_error: '❌ Export error: {message}',
  },
};

const DEFAULT_LANG = 'ru';

/**
 * Get a localized string by key, replacing {placeholder} tokens with params.
 * Falls back to 'ru' if the requested lang or key is missing.
 * @param {string} key
 * @param {string} [lang='ru']
 * @param {Record<string, string|number>} [params]
 * @returns {string}
 */
function t(key, lang = DEFAULT_LANG, params = {}) {
  const dict = I18N[lang] || I18N[DEFAULT_LANG];
  let str = dict[key] ?? I18N[DEFAULT_LANG][key] ?? key;
  for (const [k, v] of Object.entries(params)) {
    str = str.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v));
  }
  return str;
}

/**
 * Russian plural form selector.
 * @param {number} n - The number to evaluate
 * @param {string} one  - Form for 1, 21, 31 … (модель)
 * @param {string} few  - Form for 2-4, 22-24 … (модели)
 * @param {string} many - Form for 0, 5-20, 11-19 … (моделей)
 * @returns {string}
 * @example ruPlural(3, 'модель', 'модели', 'моделей') // => 'модели'
 */
function ruPlural(n, one, few, many) {
  const mod10 = Math.abs(n) % 10;
  const mod100 = Math.abs(n) % 100;
  if (mod100 >= 11 && mod100 <= 19) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

module.exports = STRINGS;
module.exports.getString = getString;
module.exports.ruPlural = ruPlural;

// Multilingual API
module.exports.t = t;
module.exports.I18N = I18N;
module.exports.DEFAULT_LANG = DEFAULT_LANG;
