'use strict';
const fs = require('fs');
const path = require('path');

describe('Wave 39 фиксы: мобильная навигация', () => {
  test('reviews.html использует канонический navbar класс', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/reviews.html'), 'utf8');
    expect(src).toContain('class="navbar"');
    expect(src).toContain('nav-burger');
  });
  test('reviews.html не использует нестандартный .header класс вместо navbar', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/reviews.html'), 'utf8');
    // old non-standard pattern
    expect(src).not.toContain('<header class="header">');
  });
  test('model-cabinet.html имеет skip-link для доступности', () => {
    const src = fs.readFileSync(path.join(__dirname, '../public/model-cabinet.html'), 'utf8');
    expect(src).toContain('skip-link');
  });
});

describe('Wave 39 фиксы: Docker и логирование', () => {
  test('Dockerfile HEALTHCHECK ищет status ok', () => {
    const src = fs.readFileSync(path.join(__dirname, '../Dockerfile'), 'utf8');
    expect(src).toContain("j.status==='ok'");
    expect(src).not.toContain("j.status==='healthy'");
  });
  test('routes/api.js sharp логи обёрнуты в NODE_ENV guard', () => {
    const src = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    // At least one sharp log must be on same line as the NODE_ENV guard (one-liner style)
    expect(src).toContain("process.env.NODE_ENV !== 'production') console.log('[sharp]");
    // Every [sharp] console.log must have NODE_ENV guard on same or immediately preceding line
    const lines = src.split('\n');
    const bareSharpLogs = lines.filter((l, i) => {
      if (!l.includes("console.log('[sharp]")) return false;
      const onSameLine = l.includes('NODE_ENV');
      const prevLine = i > 0 ? lines[i - 1] : '';
      const onPrevLine = prevLine.includes('NODE_ENV');
      return !onSameLine && !onPrevLine;
    });
    expect(bareSharpLogs).toHaveLength(0);
  });
  test('routes/api.js OTP SMS ошибка логируется через console.error', () => {
    const src = fs.readFileSync(path.join(__dirname, '../routes/api.js'), 'utf8');
    expect(src).toMatch(/console\.error\('\[OTP\] SMS send skipped/);
  });
});

describe('Wave 38 CRITICAL фикс: AI сессии', () => {
  test('bot.js использует правильный вызов setSession для ai_chat_input', () => {
    const src = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    expect(src).toContain("setSession(chatId, 'ai_chat_input'");
    expect(src).not.toContain("setSession(chatId, 'state', 'ai_chat_input'");
  });
  test('bot.js использует правильный вызов setSession для ai_budget_desc', () => {
    const src = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    expect(src).toContain("setSession(chatId, 'ai_budget_desc'");
    expect(src).not.toContain("setSession(chatId, 'state', 'ai_budget_desc'");
  });
  test('bot.js использует правильный вызов setSession для ai_match_desc', () => {
    const src = fs.readFileSync(path.join(__dirname, '../bot.js'), 'utf8');
    expect(src).toContain("setSession(chatId, 'ai_match_desc'");
    expect(src).not.toContain("setSession(chatId, 'state', 'ai_match_desc'");
  });
});
