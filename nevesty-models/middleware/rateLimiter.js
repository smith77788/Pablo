'use strict';
/**
 * Named rate limiters for specific API endpoints (БЛОК 6.4).
 * Each limiter is exported individually so routes can import only what they need.
 *
 * Fallback no-ops are used when express-rate-limit is unavailable (e.g. tests
 * that mock the module) so that routes still function correctly.
 */

const noop = (req, res, next) => next();

let authLimiter = noop;
let aiBudgetLimiter = noop;
let contactLimiter = noop;
let apiLimiter = noop;

try {
  const rateLimit = require('express-rate-limit');

  // Auth endpoints: 5 failed attempts per 15 minutes per IP (brute-force protection).
  // skipSuccessfulRequests: true — a successful login doesn't consume an attempt,
  // so a legitimate user is never locked out by their own correct credentials.
  // Window aligned with server.js authLimiter (15 min) for consistent UX.
  authLimiter = rateLimit({
    windowMs: 15 * 60 * 1000, // 15 minutes
    max: 5,
    skipSuccessfulRequests: true,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много попыток входа. Попробуйте через 15 минут.' },
  });

  // AI budget estimation: 5 requests per minute per IP (paid API calls)
  aiBudgetLimiter = rateLimit({
    windowMs: 60 * 1000, // 1 minute
    max: 5,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много запросов к AI. Попробуйте через минуту.' },
  });

  // Contact form: 3 submissions per hour per IP (spam protection)
  contactLimiter = rateLimit({
    windowMs: 60 * 60 * 1000, // 1 hour
    max: 3,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много сообщений. Попробуйте через час.' },
  });

  // General API: 100 requests per minute per IP
  apiLimiter = rateLimit({
    windowMs: 60 * 1000, // 1 minute
    max: 100,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: 'Слишком много запросов. Попробуйте позже.' },
  });
} catch {
  /* express-rate-limit unavailable — all limiters remain no-ops */
}

exports.authLimiter = authLimiter;
exports.aiBudgetLimiter = aiBudgetLimiter;
exports.contactLimiter = contactLimiter;
exports.apiLimiter = apiLimiter;
