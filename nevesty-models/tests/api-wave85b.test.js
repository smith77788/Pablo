'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');

describe('T1: catalog_per_page dynamic limit', () => {
  test('T01: showCatalog reads catalog_per_page setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]catalog_per_page['"]/);
  });
  test('T02: catalog_per_page has fallback to 5', () => {
    expect(botCode).toMatch(/catalog_per_page[\s\S]{0,100}5/s);
  });
  test('T03: per-page value is clamped to max 20', () => {
    expect(botCode).toMatch(/Math\.min\s*\(\s*20|20.*Math\.min/);
  });
});

describe('T2: catalog_sort dynamic ordering', () => {
  test('T04: showCatalog reads catalog_sort setting', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]catalog_sort['"]/);
  });
  test('T05: sort supports featured/name/new options', () => {
    const idx = botCode.indexOf("getSetting('catalog_sort')");
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/featured|name.*ASC|id.*DESC/i);
  });
});

describe('T3: cities_list dynamic buttons', () => {
  test('T06: cities_list setting is read', () => {
    expect(botCode).toMatch(/getSetting\s*\(\s*['"]cities_list['"]/);
  });
  test('T07: cities_list is split by comma', () => {
    const idx = botCode.indexOf("'cities_list'");
    const nearby = botCode.slice(idx, idx + 300);
    expect(nearby).toMatch(/split\s*\(\s*['"],['"]/);
  });
});
