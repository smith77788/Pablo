'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
const botCode = fs.readFileSync(path.join(ROOT, 'bot.js'), 'utf8');

describe('T1: Client profile feature', () => {
  test('T01: showUserProfile function exists', () => {
    expect(botCode).toMatch(/async function showUserProfile/);
  });
  test('T02: profile callback handler exists', () => {
    expect(botCode).toMatch(/data\s*===\s*['"]profile['"]/);
  });
  test('T03: showUserProfile shows order stats', () => {
    const idx = botCode.indexOf('async function showUserProfile');
    const nearby = botCode.slice(idx, idx + 4000);
    expect(nearby).toMatch(/Статистика|activeOrders|totalOrders|COUNT/i);
  });
  test('T04: showUserProfile uses MarkdownV2', () => {
    const idx = botCode.indexOf('async function showUserProfile');
    const nearby = botCode.slice(idx, idx + 4000);
    expect(nearby).toMatch(/parse_mode.*MarkdownV2|MarkdownV2/);
  });
  test('T05: buildClientKeyboard has profile button', () => {
    const idx = botCode.indexOf('async function buildClientKeyboard');
    const nearby = botCode.slice(idx, idx + 2500);
    expect(nearby).toMatch(/profile|Профиль/);
  });
  test('T06: showUserProfile uses esc() for user data', () => {
    const idx = botCode.indexOf('async function showUserProfile');
    const nearby = botCode.slice(idx, idx + 2000);
    expect(nearby).toMatch(/esc\s*\(/);
  });
});
