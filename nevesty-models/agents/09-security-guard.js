/** 🛡️ Security Guard — Immune System | SQL-ін'єкції, XSS, admin gate */
const { Agent, readFile, BOT_PATH, API_PATH } = require('./lib/base');

class SecurityGuard extends Agent {
  constructor() {
    super({ id:'09', name:'Security Guard', organ:'Immune System', emoji:'🛡️',
      focus:'SQL injection, admin gates, input sanitization' });
  }
  async analyze() {
    const botSrc = readFile(BOT_PATH);
    const apiSrc = readFile(API_PATH);

    // 1. SQL injection — только реальная интерполяция пользовательских данных
    // Безопасно: SELECT ... WHERE id=? (параметры)
    // Безопасно: `UPDATE models SET ${col}=?` если col из whitelist
    // Опасно: `SELECT ... WHERE x=${req.body.x}` — прямая подстановка
    const dangerousSQL = (botSrc.match(
      /(?:run|get|query|all)\s*\(`[^`]*\$\{(?:req\.|msg\.text|text\b(?!\s*===)|chatId\b|userId\b|username\b|orderNum\b)/g
    ) || []).length;
    if (dangerousSQL > 0) {
      this.addFinding('CRITICAL', `SQL injection: ${dangerousSQL} запросов с прямой подстановкой пользовательских данных`);
    } else {
      this.addFinding('OK', 'SQL запросы используют параметризацию — инъекции не обнаружены');
    }

    // 2. Admin gate — ищем именно обработчики (if (data.startsWith/=== ...) { isAdmin })
    // Исключаем button definitions: callback_data: 'adm_...' (там нет обработки)
    const adminCallbacks = ['adm_confirm','adm_reject','adm_review','adm_contact','adm_complete','adm_toggle'];
    const unguarded = [];
    for (const cb of adminCallbacks) {
      // Только if-обработчики: if (data.startsWith|===|== ... 'adm_cb')
      const handlerRe = new RegExp(`if\\s*\\(\\s*data(?:\\.startsWith\\(|\\s*===?\\s*)[^{]*'${cb}`, 'g');
      const matches = [...botSrc.matchAll(handlerRe)];
      if (matches.length === 0) continue; // нет обработчика — не проверяем
      const allGuarded = matches.every(m => {
        const ctx = botSrc.substring(m.index, m.index + 600);
        return ctx.includes('isAdmin') || ctx.includes('!admin');
      });
      if (!allGuarded) unguarded.push(cb);
    }
    if (unguarded.length > 0) {
      this.addFinding('HIGH', `Admin gate отсутствует для: ${unguarded.join(', ')}`);
    } else {
      this.addFinding('OK', 'isAdmin() проверяется во всех admin-обработчиках');
    }

    // 3. Deep-link hijack — владелец заявки проверяется перед показом деталей
    const hasHijackGuard = botSrc.includes('client_chat_id') &&
      (botSrc.includes('client_chat_id !== String') || botSrc.includes("!== String(chatId)") ||
       (botSrc.includes('client_chat_id &&') && botSrc.includes('String(chatId)')));
    if (!hasHijackGuard) {
      this.addFinding('MEDIUM', 'Защита deep-link hijacking: client_chat_id не проверяется');
    } else {
      this.addFinding('OK', 'Deep-link hijack защита присутствует');
    }

    // 4. Webhook secret
    if (botSrc.includes('WEBHOOK_SECRET')) {
      this.addFinding('OK', 'Webhook secret проверяется');
    } else {
      this.addFinding('LOW', 'Webhook secret не найден (актуально только для webhook-режима)');
    }

    // 5. Rate limiting на API
    if (apiSrc.includes('rateLimit') || apiSrc.includes('rate-limit') || apiSrc.includes('express-rate-limit')) {
      this.addFinding('OK', 'Rate limiting на API присутствует');
    } else {
      this.addFinding('LOW', 'Rate limiting на API отсутствует (низкий приоритет для внутреннего API)');
    }

    // 6. JWT auth middleware
    const authMiddlewareUsed = (apiSrc.match(/,\s*auth\s*,/g)||[]).length;
    const jwtChecks = (apiSrc.match(/authenticateToken|jwt\.verify/g)||[]).length + authMiddlewareUsed;
    if (jwtChecks >= 2) {
      this.addFinding('OK', `JWT аутентификация: ${jwtChecks} защищённых роутов`);
    } else if (jwtChecks === 1) {
      this.addFinding('LOW', 'JWT: только 1 защищённый роут — проверь остальные');
    } else {
      this.addFinding('MEDIUM', 'JWT аутентификация не обнаружена в API');
    }

    // 7. eval() / new Function() с пользовательскими данными
    if (botSrc.includes('eval(') || botSrc.includes('new Function(')) {
      this.addFinding('CRITICAL', 'eval() или new Function() обнаружены — потенциальная RCE уязвимость');
    } else {
      this.addFinding('OK', 'Нет eval() или new Function()');
    }
  }
}

if (require.main === module) new SecurityGuard().run().then(() => process.exit(0));
module.exports = SecurityGuard;
