'use strict';
/**
 * Shared constants for Nevesty Models bot and API.
 *
 * All core constants live in utils/constants.js.
 * This file re-exports them for convenience and adds
 * SESSION_REMINDER_MS which is used only in bot.js session logic.
 */

const {
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
} = require('./utils/constants');

/** Soft-reminder fires 15 min before the hard SESSION_TIMEOUT_MS cutoff. */
const SESSION_REMINDER_MS = 15 * 60 * 1000; // 15 minutes

module.exports = {
  // Order / booking statuses
  STATUS_LABELS,
  VALID_STATUSES,

  // Event types
  EVENT_TYPES,
  ALLOWED_EVENT_TYPES,

  // Model categories
  MODEL_CATEGORIES,
  ALLOWED_CATEGORIES,
  CATEGORIES,

  // Model attribute lists
  MODEL_HAIR_COLORS,
  MODEL_EYE_COLORS,

  // Booking durations (hours)
  DURATIONS,

  // Telegram message length limits
  MAX_MESSAGE_LENGTH,
  MAX_CAPTION_LENGTH,

  // Session timeouts
  SESSION_REMINDER_MS,
  SESSION_TIMEOUT_MS,

  // Pagination
  CATALOG_PAGE_SIZE,
  ORDERS_PAGE_SIZE,
  REVIEWS_PAGE_SIZE,
};
