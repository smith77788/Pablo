/** 🚑 Error Recovery — Immune Response | try/catch покриття, graceful failures */
const { Agent, readFile, BOT_PATH, SRV_PATH } = require('./lib/base');

class ErrorRecovery extends Agent {
  constructor() {
    super({ id:'15', name:'Error Recovery', organ:'Immune Response', emoji:'🚑',
      focus:'try/catch coverage, safeSend usage, no unhandled rejections' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. safeSend використовується замість прямого bot.sendMessage
    const directSend = (src.match(/bot\.sendMessage\(/g)||[]).length;
    const safeSends  = (src.match(/safeSend\(/g)||[]).length;
    if (directSend > 5) this.addFinding('HIGH',`bot.sendMessage() викликається ${directSend} разів напряму — використовуй safeSend() для стійкості`);
    else this.addFinding('OK',`safeSend() використовується ${safeSends} разів, прямий виклик: ${directSend}`);

    // 2. try/catch у кожному обробнику callback
    const asyncHandlers = (src.match(/async\s*\(/g)||[]).length;
    const tryCatches    = (src.match(/try\s*\{/g)||[]).length;
    const ratio = asyncHandlers > 0 ? (tryCatches/asyncHandlers*100).toFixed(0) : 0;
    if (tryCatches < asyncHandlers * 0.5) this.addFinding('MEDIUM',`try/catch покриває лише ~${ratio}% async функцій (${tryCatches}/${asyncHandlers})`);
    else this.addFinding('OK',`try/catch покриття: ~${ratio}% (${tryCatches}/${asyncHandlers})`);

    // 3. polling_error обробляється
    if (!src.includes('polling_error')) this.addFinding('HIGH','polling_error не обробляється — бот впаде при мережевій помилці');
    else this.addFinding('OK','polling_error обробляється');

    // 4. console.error у catch блоках
    const catchBlocks = src.match(/catch\s*\([^)]+\)\s*\{[^}]*\}/g) || [];
    const emptyOrSilent = catchBlocks.filter(cb => !cb.includes('console') && !cb.includes('log'));
    if (emptyOrSilent.length > 3) this.addFinding('LOW',`${emptyOrSilent.length} catch блоків без логування — помилки будуть мовчати`);
    else this.addFinding('OK','Catch блоки логують помилки');

    // 5. Сервер graceful shutdown
    const srvSrc = readFile(SRV_PATH);
    if (!srvSrc.includes('SIGTERM') && !srvSrc.includes('SIGINT')) {
      this.addFinding('MEDIUM','Graceful shutdown при SIGTERM відсутній — БД може пошкодитись при перезапуску');
    } else { this.addFinding('OK','Graceful shutdown при SIGTERM/SIGINT реалізований'); }
  }
}

if (require.main === module) new ErrorRecovery().run().then(() => process.exit(0));
module.exports = ErrorRecovery;
