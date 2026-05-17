'use strict';

/**
 * keyboards/constants.js — UI-level constants used in bot keyboard builders
 * and display logic.
 *
 * These constants are defined inline in bot.js and copied here for gradual
 * migration. The originals in bot.js remain unchanged until each constant is
 * fully migrated (bot.js updated to require this file instead).
 *
 * Source: bot.js (lines noted per constant)
 */

// ─── Month abbreviations (line 1254 in bot.js) ───────────────────────────────
const MONTHS_RU = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

// ─── Message template categories (line 2108 in bot.js) ───────────────────────
const TEMPLATE_CATEGORIES = {
  general: '📌 Общие',
  booking: '📅 Бронирование',
  payment: '💳 Оплата',
  reminder: '⏰ Напоминания',
};

// ─── Quick-reply canned responses (line 4567 in bot.js) ──────────────────────
const QUICK_REPLY_TEMPLATES = [
  '✅ Ваша заявка принята! Менеджер свяжется с вами в ближайшее время.',
  '📞 Уточним детали в ближайшее время. Пожалуйста, будьте на связи.',
  '🕐 Свяжемся с вами сегодня — ждите звонка или сообщения.',
  '💃 Предложим вам подходящую модель — уже подбираем варианты!',
];

// ─── Quick note type labels (line 4592 in bot.js) ────────────────────────────
const QUICK_NOTE_TEMPLATES = {
  call: '📞 Связались с клиентом',
  budget: '💰 Уточнение бюджета',
  date: '🗓 Дата мероприятия согласована',
  logistics: '🚗 Логистика и расположение обсуждены',
};

// ─── Loyalty programme tiers (line 5783 in bot.js) ───────────────────────────
const LOYALTY_LEVELS = [
  { key: 'platinum', label: '💎 Платиновый', minEarned: 5000, discount: 15 },
  { key: 'gold', label: '🥇 Золотой', minEarned: 2000, discount: 10 },
  { key: 'silver', label: '🥈 Серебряный', minEarned: 500, discount: 5 },
  { key: 'bronze', label: '🥉 Бронзовый', minEarned: 0, discount: 0 },
];

// ─── Achievement definitions (line 5837 in bot.js) ───────────────────────────
const ACHIEVEMENTS_LIST = [
  { key: 'first_order', icon: '🥇', title: 'Первая заявка', desc: 'Оформил первую успешную заявку' },
  { key: 'loyal_client', icon: '🔥', title: 'Постоянный клиент', desc: '3+ завершённых заявки' },
  { key: 'vip_client', icon: '💎', title: 'VIP клиент', desc: '10+ завершённых заявок' },
  { key: 'first_review', icon: '⭐', title: 'Критик', desc: 'Оставил первый отзыв' },
  { key: 'talkative', icon: '💬', title: 'Общительный', desc: 'Написал менеджеру более 5 раз' },
  { key: 'precise_choice', icon: '🎯', title: 'Точный выбор', desc: 'Забронировал без изменений даты' },
  { key: 'traveler', icon: '🌍', title: 'Путешественник', desc: 'Заявки из 2+ разных городов' },
];

// ─── Price-calculator defaults (line 6180 in bot.js) ─────────────────────────
const DEFAULT_RATES = {
  base_per_hour: 10000, // per model per hour
  type_multipliers: {
    fashion_show: 1.5,
    photo_shoot: 1.2,
    event: 1.0,
    commercial: 1.4,
    runway: 1.3,
    other: 1.0,
  },
  organization_fee: 15000, // flat organization fee
};

// Tier multipliers: Эконом / Стандарт / Премиум (line 6194 in bot.js)
const CALC_TIERS = {
  econ: { label: 'Эконом', mult: 0.8 },
  standard: { label: 'Стандарт', mult: 1.0 },
  premium: { label: 'Премиум', mult: 1.35 },
};

// ─── Common callback_data strings (gradual migration from magic strings) ─────
const CB_DATA = {
  MAIN_MENU: 'main_menu',
  ADMIN_MENU: 'admin_menu',
  MY_ORDERS: 'my_orders',
  BK_START: 'bk_start',
  BK_CANCEL: 'bk_cancel',
  CAT_CAT: 'cat_cat__0',
  ADM_ORDERS: 'adm_orders__0',
  ADM_SETTINGS: 'adm_settings',
  ADM_FACTORY: 'adm_factory',
  ADM_PROMOS: 'adm_promos',
  FAV_LIST: 'fav_list',
  PROFILE: 'profile',
  SEARCH: 'cat_search',
  REFERRAL: 'referral',
};

// ─── FAQ category labels (line 13526 in bot.js) ───────────────────────────────
const FAQ_CATEGORY_LABELS = {
  pricing: '💰 Цены',
  shooting: '📸 Съёмки',
  process: '📋 Заявки',
  delivery: '🚚 Доставка',
  general: '❓ Общее',
};

module.exports = {
  MONTHS_RU,
  TEMPLATE_CATEGORIES,
  QUICK_REPLY_TEMPLATES,
  QUICK_NOTE_TEMPLATES,
  LOYALTY_LEVELS,
  ACHIEVEMENTS_LIST,
  DEFAULT_RATES,
  CALC_TIERS,
  FAQ_CATEGORY_LABELS,
  CB_DATA,
};
