'use strict';
const fs = require('fs');
const path = require('path');

describe('Security: Helmet, Rate Limiting, Sanitization', () => {
  let serverSrc, apiSrc, packageJson;
  beforeAll(() => {
    serverSrc = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');
    apiSrc = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
  });

  describe('Helmet.js headers', () => {
    test('helmet is in package.json dependencies', () => {
      expect(packageJson.dependencies.helmet || packageJson.devDependencies?.helmet).toBeTruthy();
    });
    test('server.js requires helmet', () => {
      expect(serverSrc).toMatch(/require\(['"]helmet['"]\)/);
    });
    test('server.js uses app.use(helmet', () => {
      expect(serverSrc).toMatch(/app\.use\(helmet/);
    });
  });

  describe('Rate limiting', () => {
    test('express-rate-limit is in package.json dependencies', () => {
      const deps = { ...packageJson.dependencies, ...packageJson.devDependencies };
      expect(deps['express-rate-limit']).toBeTruthy();
    });
    test('server.js requires express-rate-limit', () => {
      expect(serverSrc).toMatch(/require\(['"]express-rate-limit['"]\)/);
    });
    test('server.js creates apiLimiter for /api/', () => {
      expect(serverSrc).toMatch(/rateLimit\s*\(/);
    });
    test('server.js applies rate limiter to /api/ routes', () => {
      expect(serverSrc).toMatch(/app\.use\(['"]\/api\//);
    });
    test('auth endpoints have stricter rate limit', () => {
      expect(serverSrc).toMatch(/\/api\/auth\/|authLimiter/);
    });
  });

  describe('Input sanitization', () => {
    test('api.js has sanitizeInput middleware function', () => {
      expect(apiSrc).toMatch(/function sanitizeInput|sanitizeInput\s*=/);
    });
    test('api.js applies sanitizeInput via router.use', () => {
      expect(apiSrc).toMatch(/router\.use\(sanitizeInput\)|router\.use\(.*sanitize/);
    });
    test('sanitizeInput strips dangerous content (null bytes or script tags)', () => {
      // The sanitizeInput middleware removes null bytes (\0) or strips dangerous HTML
      const hasNullByteStrip = /\\\\0|\\u0000|null.byte|replace.*\\0/.test(apiSrc);
      const hasScriptStrip = /<script|script.*replace/i.test(apiSrc);
      const hasSanitizeBody = /sanitize.*body|body.*sanitize|clean.*val|val.*replace/i.test(apiSrc);
      expect(hasNullByteStrip || hasScriptStrip || hasSanitizeBody).toBe(true);
    });
  });
});
