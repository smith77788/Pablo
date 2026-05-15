'use strict';

// Order statuses
const ORDER_STATUSES = {
  new: '🆕 Новая',
  reviewing: '🔍 На рассмотрении',
  confirmed: '✅ Подтверждена',
  in_progress: '🔄 В работе',
  completed: '🏁 Завершена',
  cancelled: '❌ Отменена'
};

// Event types
const EVENT_TYPES = {
  wedding: '💒 Свадьба',
  corporate: '🏢 Корпоратив',
  fashion: '👗 Показ мод',
  commercial: '📸 Коммерческая съёмка',
  other: '📋 Другое'
};

// Model categories
const MODEL_CATEGORIES = {
  fashion: 'Fashion',
  commercial: 'Commercial',
  events: 'Events'
};

// Session timeout (milliseconds)
const SESSION_TIMEOUT_MS = 30 * 60 * 1000;

// Pagination
const CATALOG_PAGE_SIZE = 5;
const ORDERS_PAGE_SIZE = 5;
const REVIEWS_PAGE_SIZE = 5;

// Caption limits
const CAPTION_MAX_LENGTH = 1024;
const MESSAGE_MAX_LENGTH = 4096;
const BIO_PREVIEW_LENGTH = 180;

module.exports = {
  ORDER_STATUSES,
  EVENT_TYPES,
  MODEL_CATEGORIES,
  SESSION_TIMEOUT_MS,
  CATALOG_PAGE_SIZE,
  ORDERS_PAGE_SIZE,
  REVIEWS_PAGE_SIZE,
  CAPTION_MAX_LENGTH,
  MESSAGE_MAX_LENGTH,
  BIO_PREVIEW_LENGTH
};
