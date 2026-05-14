/** 🔄 Consistency Checker — Homeostasis | Согласованность констант, статусов, ключей */
const { Agent, readFile, dbAll, BOT_PATH, API_PATH } = require('./lib/base');

class ConsistencyChecker extends Agent {
  constructor() {
    super({ id:'25', name:'Consistency Checker', organ:'Homeostasis', emoji:'🔄',
      focus:'STATUS_LABELS vs DB values, callback prefixes, field name consistency' });
  }
  async analyze() {
    const botSrc = readFile(BOT_PATH);
    const apiSrc = readFile(API_PATH);

    // 1. STATUS_LABELS соответствует реальным статусам в DB
    const expectedStatuses = ['new','in_review','confirmed','in_progress','completed','rejected'];
    const labelBlock = botSrc.match(/STATUS_LABELS\s*=\s*\{([^}]+)\}/)?.[1] || '';
    const definedStatuses = (labelBlock.match(/'([^']+)':/g)||[]).map(s=>s.replace(/[':]/g,''));

    const missingInLabels = expectedStatuses.filter(s => !definedStatuses.includes(s));
    const extraInLabels   = definedStatuses.filter(s => !expectedStatuses.includes(s));

    if (missingInLabels.length > 0) {
      this.addFinding('HIGH', `STATUS_LABELS не содержит: ${missingInLabels.join(', ')}`);
    } else {
      this.addFinding('OK', `STATUS_LABELS содержит все ${expectedStatuses.length} статусов`);
    }
    if (extraInLabels.length > 0) {
      this.addFinding('LOW', `STATUS_LABELS содержит лишние статусы: ${extraInLabels.join(', ')}`);
    }

    // 2. VALID_STATUSES совпадает с expectedStatuses
    const validBlock = botSrc.match(/VALID_STATUSES\s*=\s*\[([^\]]+)\]/)?.[1] || '';
    const validStatuses = (validBlock.match(/'([^']+)'/g)||[]).map(s=>s.replace(/'/g,''));
    const missingValid = expectedStatuses.filter(s => !validStatuses.includes(s));
    if (missingValid.length > 0 && validStatuses.length > 0) {
      this.addFinding('MEDIUM', `VALID_STATUSES неполный, не хватает: ${missingValid.join(', ')}`);
    } else if (validStatuses.length === 0) {
      this.addFinding('INFO', 'VALID_STATUSES не определён в коде');
    } else {
      this.addFinding('OK', `VALID_STATUSES корректный (${validStatuses.length} статусов)`);
    }

    // 3. Реальные статусы в DB
    try {
      const dbStatuses = await dbAll("SELECT DISTINCT status FROM orders LIMIT 20");
      const realStats = dbStatuses.map(r=>r.status);
      const unknownStats = realStats.filter(s => !expectedStatuses.includes(s));
      if (unknownStats.length > 0) {
        this.addFinding('MEDIUM', `В DB найдены неизвестные статусы: ${unknownStats.join(', ')}`);
      } else {
        this.addFinding('OK', `Статусы в DB корректны: ${realStats.join(', ') || 'нет данных'}`);
      }
    } catch {}

    // 4. Поля booking в боте и в DB schema совпадают
    const dbFields = ['client_name','client_phone','client_email','client_telegram',
                      'event_type','event_date','duration','location','budget','comments','model_id'];
    const missingFields = dbFields.filter(f => !botSrc.includes(f));
    if (missingFields.length > 0) {
      this.addFinding('HIGH', `Поля заказа отсутствуют в bot.js: ${missingFields.join(', ')}`);
    } else {
      this.addFinding('OK', `Все ${dbFields.length} полей заказа используются в bot.js`);
    }

    // 5. API и Bot используют одинаковые названия таблиц
    const tables = ['orders','models','telegram_sessions','agent_logs'];
    for (const tbl of tables) {
      const inBot = botSrc.includes(`FROM ${tbl}`) || botSrc.includes(`INTO ${tbl}`);
      const inApi = apiSrc.includes(`FROM ${tbl}`) || apiSrc.includes(`INTO ${tbl}`);
      if (inBot && inApi) {
        this.addFinding('OK', `Таблица '${tbl}' используется в обоих файлах — согласовано`);
      }
    }

    // 6. parse_mode согласован
    const markdownV2 = (botSrc.match(/parse_mode:\s*['"]MarkdownV2['"]/g)||[]).length;
    const markdown   = (botSrc.match(/parse_mode:\s*['"]Markdown['"]/g)||[]).length;
    const html       = (botSrc.match(/parse_mode:\s*['"]HTML['"]/g)||[]).length;
    if (markdownV2 > 0 && markdown > 0) {
      this.addFinding('MEDIUM', `Смешанные parse_mode: MarkdownV2×${markdownV2} и Markdown×${markdown} — может вызывать ошибки форматирования`);
    } else {
      this.addFinding('OK', `parse_mode единый: MarkdownV2×${markdownV2} Markdown×${markdown} HTML×${html}`);
    }
  }
}

if (require.main === module) new ConsistencyChecker().run().then(() => process.exit(0));
module.exports = ConsistencyChecker;
