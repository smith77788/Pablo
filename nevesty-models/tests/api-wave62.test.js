'use strict';
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'test-secret-32-chars-minimum-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const fs = require('fs');
const path = require('path');

// ─── 1. Bot breadcrumb navigation ─────────────────────────────────────────────
describe('bot.js breadcrumb navigation', () => {
  const botPath = path.join(__dirname, '../bot.js');

  it('bot.js file exists', () => {
    expect(fs.existsSync(botPath)).toBe(true);
  });

  it('Contains showPublicReviews breadcrumb (🏠 Главная › ⭐ Отзывы)', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('🏠 Главная › ⭐ Отзывы');
  });

  it('Contains showWishlist breadcrumb (🏠 Главная › ❤️ Избранное)', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('🏠 Главная › ❤️ Избранное');
  });

  it('Contains showUserProfile breadcrumb (🏠 Главная › 👤 Профиль)', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('🏠 Главная › 👤 Профиль');
  });

  it('Contains showCatalogByCity breadcrumb (🏠 Главная › 💃 Каталог ›)', () => {
    const content = fs.readFileSync(botPath, 'utf8');
    expect(content).toContain('🏠 Главная › 💃 Каталог ›');
  });
});

// ─── 2. Catalog URL persistence ───────────────────────────────────────────────
describe('catalog.html share link button', () => {
  const catalogHtmlPath = path.join(__dirname, '../public/catalog.html');

  it('catalog.html file exists', () => {
    expect(fs.existsSync(catalogHtmlPath)).toBe(true);
  });

  it('Contains shareLinkBtn element or "Скопировать ссылку" text', () => {
    const content = fs.readFileSync(catalogHtmlPath, 'utf8');
    expect(content).toMatch(/shareLinkBtn|Скопировать ссылку/);
  });
});

describe('public/js/catalog.js URL persistence', () => {
  const catalogJsPath = path.join(__dirname, '../public/js/catalog.js');

  it('catalog.js file exists', () => {
    expect(fs.existsSync(catalogJsPath)).toBe(true);
  });

  it('Contains replaceState or pushState for URL persistence', () => {
    const content = fs.readFileSync(catalogJsPath, 'utf8');
    expect(content).toMatch(/replaceState|pushState/);
  });

  it('Contains URLSearchParams for reading URL params', () => {
    const content = fs.readFileSync(catalogJsPath, 'utf8');
    expect(content).toContain('URLSearchParams');
  });
});

// ─── 3. Render.com deploy config ──────────────────────────────────────────────
describe('render.yaml deploy config', () => {
  const renderYamlPath = path.join(__dirname, '../../render.yaml');

  it('render.yaml file exists at repo root', () => {
    expect(fs.existsSync(renderYamlPath)).toBe(true);
  });

  it('Contains "nevesty-models" service name', () => {
    const content = fs.readFileSync(renderYamlPath, 'utf8');
    expect(content).toContain('nevesty-models');
  });

  it('Contains "nevesty-factory" service name', () => {
    const content = fs.readFileSync(renderYamlPath, 'utf8');
    expect(content).toContain('nevesty-factory');
  });

  it('Contains "healthCheckPath" setting', () => {
    const content = fs.readFileSync(renderYamlPath, 'utf8');
    expect(content).toContain('healthCheckPath');
  });
});

// ─── 4. Railway deploy config ─────────────────────────────────────────────────
describe('railway.toml deploy config', () => {
  const railwayTomlPath = path.join(__dirname, '../../railway.toml');

  it('railway.toml file exists at repo root', () => {
    expect(fs.existsSync(railwayTomlPath)).toBe(true);
  });

  it('Contains "NIXPACKS" builder', () => {
    const content = fs.readFileSync(railwayTomlPath, 'utf8');
    expect(content).toContain('NIXPACKS');
  });
});

// ─── 5. .env.example WhatsApp vars ────────────────────────────────────────────
describe('.env.example WhatsApp environment variables', () => {
  const envExamplePath = path.join(__dirname, '../.env.example');

  it('.env.example file exists', () => {
    expect(fs.existsSync(envExamplePath)).toBe(true);
  });

  it('Contains WHATSAPP_TOKEN variable', () => {
    const content = fs.readFileSync(envExamplePath, 'utf8');
    expect(content).toContain('WHATSAPP_TOKEN');
  });

  it('Contains WHATSAPP_PHONE_ID variable', () => {
    const content = fs.readFileSync(envExamplePath, 'utf8');
    expect(content).toContain('WHATSAPP_PHONE_ID');
  });

  it('Contains WHATSAPP_VERIFY_TOKEN variable', () => {
    const content = fs.readFileSync(envExamplePath, 'utf8');
    expect(content).toContain('WHATSAPP_VERIFY_TOKEN');
  });
});
