'use strict';

// Status labels include emoji prefix (must match bot.js display format)
const STATUS_LABELS = {
  new:         '🆕 Новая',
  reviewing:   '🔍 На рассмотрении',
  confirmed:   '✅ Подтверждена',
  in_progress: '▶️ В процессе',
  completed:   '🏁 Завершена',
  cancelled:   '❌ Отменена',
};

const VALID_STATUSES = Object.keys(STATUS_LABELS);

// Event type key → human-readable label
const EVENT_TYPES = {
  fashion_show: 'Показ мод',
  photo_shoot:  'Фотосессия',
  event:        'Корпоратив / Мероприятие',
  commercial:   'Коммерческая съёмка',
  runway:       'Подиум',
  other:        'Другое',
};

const ALLOWED_EVENT_TYPES = Object.keys(EVENT_TYPES);

// Category key → human-readable label (without the "all" empty-key entry)
const MODEL_CATEGORIES = {
  fashion:    'Fashion',
  commercial: 'Commercial',
  events:     'Events',
};

const ALLOWED_CATEGORIES = Object.keys(MODEL_CATEGORIES);

// Category map used in bot UI (includes empty key for "All" filter)
const CATEGORIES = {
  '':         'Все',
  ...MODEL_CATEGORIES,
};

// Model attribute lists
const MODEL_HAIR_COLORS = ['Блонд', 'Тёмный блонд', 'Шатен', 'Брюнетка', 'Рыжая', 'Другой'];
const MODEL_EYE_COLORS  = ['Голубые', 'Серые', 'Зелёные', 'Карие', 'Чёрные'];

const DURATIONS = ['1', '2', '3', '4', '6', '8', '12'];

// Telegram message length limits
const MAX_MESSAGE_LENGTH = 4096;
const MAX_CAPTION_LENGTH = 1024;

// Session timeout
const SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes

// Pagination defaults
const CATALOG_PAGE_SIZE  = 5;
const ORDERS_PAGE_SIZE   = 5;
const REVIEWS_PAGE_SIZE  = 5;

module.exports = {
  STATUS_LABELS,
  VALID_STATUSES,
  EVENT_TYPES,
  ALLOWED_EVENT_TYPES,
  MODEL_CATEGORIES,
  ALLOWED_CATEGORIES,
  CATEGORIES,
  MODEL_HAIR_COLORS,
  MODEL_EYE_COLORS,
  DURATIONS,
  MAX_MESSAGE_LENGTH,
  MAX_CAPTION_LENGTH,
  SESSION_TIMEOUT_MS,
  CATALOG_PAGE_SIZE,
  ORDERS_PAGE_SIZE,
  REVIEWS_PAGE_SIZE,
};
