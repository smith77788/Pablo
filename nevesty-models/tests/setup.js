/**
 * Test server setup helper for Nevesty Models API integration tests.
 * Sets env vars BEFORE any require() of server/database modules.
 */

'use strict';

// Must be set before any app modules are loaded
process.env.PORT = '3001';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';  // disable bot in tests
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

// Load .env so database path etc. are set, but our overrides above win
require('dotenv').config();
// Re-apply critical overrides in case dotenv overwrote them
process.env.PORT = '3001';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
