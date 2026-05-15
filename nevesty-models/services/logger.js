'use strict';
// Logs go to stdout (Docker/PM2 handles rotation via logrotate or docker logs)

const LOG_JSON = process.env.LOG_JSON === '1' || process.env.NODE_ENV === 'production';
const LOG_LEVEL = process.env.LOG_LEVEL || 'info';

const LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
const currentLevel = LEVELS[LOG_LEVEL] ?? 2;

function log(level, msg, meta = {}) {
  if ((LEVELS[level] ?? 2) > currentLevel) return;
  if (LOG_JSON) {
    process.stdout.write(JSON.stringify({ ts: new Date().toISOString(), level, msg, ...meta }) + '\n');
  } else {
    const prefix = `[${level.toUpperCase()}]`;
    const metaStr = Object.keys(meta).length ? ' ' + JSON.stringify(meta) : '';
    console.log(`${prefix} ${msg}${metaStr}`);
  }
}

module.exports = {
  error: (msg, meta) => log('error', msg, meta),
  warn:  (msg, meta) => log('warn',  msg, meta),
  info:  (msg, meta) => log('info',  msg, meta),
  debug: (msg, meta) => log('debug', msg, meta),
};
