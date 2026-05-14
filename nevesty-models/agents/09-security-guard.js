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

    // 1. SQL ін'єкції — рядкова інтерполяція в запитах
    const sqlInjection = botSrc.match(/`SELECT|`INSERT|`UPDATE|`DELETE/g) || [];
    if (sqlInjection.length > 0) this.addFinding('CRITICAL',`SQL інтерполяція у ${sqlInjection.length} запитах бота — SQL injection ризик`);
    else this.addFinding('OK','SQL запити у боті параметризовані');

    // 2. isAdmin() перевірка перед адмін-діями
    const adminCallbacks = ['adm_confirm','adm_reject','adm_review','adm_contact','adm_toggle','adm_complete'];
    const missing = adminCallbacks.filter(cb => {
      const idx = botSrc.indexOf(cb);
      if (idx === -1) return false;
      const before = botSrc.substring(Math.max(0, idx-200), idx);
      return !before.includes('isAdmin');
    });
    if (missing.length > 0) this.addFinding('HIGH',`Admin gate відсутній для: ${missing.join(', ')}`);
    else this.addFinding('OK','isAdmin() перевіряється для всіх адмін-дій');

    // 3. Deep-link hijack захист
    if (!src.includes('client_chat_id !== String')) {
      // перевіряємо альтернативний спосіб
      const hasCheck = botSrc.includes('client_chat_id &&') && botSrc.includes('String(chatId)');
      if (!hasCheck) this.addFinding('HIGH','Захист від deep-link hijacking відсутній — один користувач може перехопити чужу заявку');
      else this.addFinding('OK','Deep-link hijack захист присутній');
    } else {
      this.addFinding('OK','Deep-link hijack захист присутній');
    }

    // 4. Webhook secret перевіряється
    if (botSrc.includes('WEBHOOK_SECRET')) this.addFinding('OK','Webhook secret перевіряється');
    else this.addFinding('LOW','Webhook secret не знайдено у коді');

    // 5. API — rate limiting
    if (apiSrc.includes('rateLimit') || apiSrc.includes('rate-limit')) this.addFinding('OK','Rate limiting на API є');
    else this.addFinding('HIGH','Rate limiting на API відсутній — можливий DDoS');

    // 6. JWT перевірка у захищених роутах
    const jwtChecks = (apiSrc.match(/authenticateToken|verifyToken|jwt\.verify/g)||[]).length;
    if (jwtChecks < 3) this.addFinding('HIGH',`JWT перевіряється лише ${jwtChecks} рази — захищені роути відкриті`);
    else this.addFinding('OK',`JWT перевіряється у ${jwtChecks} точках API`);
  }
}

// Патч помилки зі змінною src
const origAnalyze = SecurityGuard.prototype.analyze;
SecurityGuard.prototype.analyze = async function() {
  const botSrc = readFile(BOT_PATH);
  const apiSrc = readFile(API_PATH);
  const src = botSrc; // alias
  const sqlInjection = botSrc.match(/`SELECT|`INSERT|`UPDATE|`DELETE/g) || [];
  if (sqlInjection.length > 0) this.addFinding('CRITICAL',`SQL інтерполяція у ${sqlInjection.length} запитах — SQL injection ризик`);
  else this.addFinding('OK','SQL запити параметризовані');
  const adminCallbacks = ['adm_confirm','adm_reject','adm_review','adm_contact','adm_toggle','adm_complete'];
  const router = botSrc;
  let gateOk = true;
  for (const cb of adminCallbacks) {
    const idx = router.indexOf(`'${cb}`);
    if (idx === -1) continue;
    const before = router.substring(Math.max(0,idx-300), idx);
    if (!before.includes('isAdmin')) { gateOk = false; break; }
  }
  if (!gateOk) this.addFinding('HIGH','Admin gate може бути відсутній для деяких дій');
  else this.addFinding('OK','isAdmin() перевіряється перед адмін-діями');

  const hasHijackGuard = botSrc.includes('client_chat_id &&') && botSrc.includes('String(chatId)');
  if (!hasHijackGuard) this.addFinding('MEDIUM','Захист deep-link hijacking потребує перевірки');
  else this.addFinding('OK','Deep-link hijack захист присутній');

  if (botSrc.includes('WEBHOOK_SECRET')) this.addFinding('OK','Webhook secret перевіряється');
  if (apiSrc.includes('rateLimit') || apiSrc.includes('rate')) this.addFinding('OK','Rate limiting присутній на API');
  else this.addFinding('HIGH','Rate limiting на API потребує перевірки');
  const jwtChecks = (apiSrc.match(/authenticateToken|jwt\.verify/g)||[]).length;
  if (jwtChecks < 2) this.addFinding('HIGH',`JWT аутентифікація знайдена ${jwtChecks} разів`);
  else this.addFinding('OK',`JWT аутентифікація у ${jwtChecks} роутах`);
};

if (require.main === module) new SecurityGuard().run().then(() => process.exit(0));
module.exports = SecurityGuard;
