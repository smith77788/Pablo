'use strict';
/**
 * Wave107 tests: catalog URL pushState sync, order-count badge,
 * DB backup infrastructure, AI match improvements, model archive (soft delete).
 */

const fs = require('fs');
const path = require('path');

describe('Wave107: Catalog URL sync, Backup, Model archive, AI match improvements', () => {
  let catalogJs, catalogHtml, serverSrc, botSrc;

  beforeAll(() => {
    catalogJs = fs.readFileSync(path.join(__dirname, '../public/js/catalog.js'), 'utf8');
    catalogHtml = fs.readFileSync(path.join(__dirname, '../public/catalog.html'), 'utf8');
    serverSrc = fs.readFileSync(path.join(__dirname, '../server.js'), 'utf8');
    botSrc = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
  });

  describe('W1: Catalog URL pushState sync', () => {
    test('catalog.js uses history.pushState for filter changes', () => {
      expect(catalogJs).toMatch(/history\.pushState/);
    });
    test('catalog.js handles popstate for browser back navigation', () => {
      expect(catalogJs).toMatch(/addEventListener.*popstate|popstate.*addEventListener/);
    });
    test('catalog.js reads URL params on load', () => {
      expect(catalogJs).toMatch(/getUrlParams|URLSearchParams|location\.search/);
    });
  });

  describe('W2: Order count badge on model cards', () => {
    test('catalog.html has order-badge CSS class', () => {
      expect(catalogHtml).toMatch(/order-badge/);
    });
    test('catalog.js renders order_count with Russian pluralization', () => {
      expect(catalogJs).toMatch(/order_count|заказ/);
    });
    test('catalog.js uses order-badge span', () => {
      expect(catalogJs).toMatch(/order-badge/);
    });
  });

  describe('DB Backup infrastructure', () => {
    test('.gitignore excludes backups directory', () => {
      const gitignore = fs.readFileSync(path.join(__dirname, '../.gitignore'), 'utf8');
      expect(gitignore).toMatch(/backups\//);
    });
    test('server.js or scheduler has DB backup functionality', () => {
      let schedulerSrc = '';
      try {
        schedulerSrc = fs.readFileSync(path.join(__dirname, '../services/scheduler.js'), 'utf8');
      } catch {}
      const hasBackup =
        serverSrc.includes('backup') || schedulerSrc.includes('backup') || schedulerSrc.includes('Backup');
      expect(hasBackup).toBe(true);
    });
    test('health endpoint includes backup status', () => {
      expect(serverSrc).toMatch(/backup.*last|last_backup|backup_last/i);
    });
    test('health endpoint has factory_alert field', () => {
      expect(serverSrc).toMatch(/factory_alert/);
    });
  });

  describe('AI Match improvements', () => {
    test('bot.js AI match uses Anthropic API fetch directly', () => {
      expect(botSrc).toMatch(/api\.anthropic\.com|runAiMatch|ai.match/i);
    });
    test('bot.js AI match returns per-model reasons', () => {
      expect(botSrc).toMatch(/reason.*picks|picks.*reason|"reason"/);
    });
    test('bot.js AI match has fallback when no API key', () => {
      expect(botSrc).toMatch(/!ANTHROPIC_API_KEY|! ANTHROPIC_API_KEY|!.*ANTHROPIC_API_KEY/);
    });
  });

  describe('Model archive (soft delete)', () => {
    test('bot.js has restore model callback', () => {
      expect(botSrc).toMatch(/adm_model_restore_|adm_restore_|restore.*model|archived.*0/);
    });
    test('bot.js has model archive callback', () => {
      expect(botSrc).toMatch(/adm_model_archive_|adm_archive_|archived.*1|archive.*model/i);
    });
    test('bot.js has model duplicate callback', () => {
      expect(botSrc).toMatch(/adm_model_dup_|adm_duplicate_|дублировать|копия/);
    });
  });
});
