/** 🔀 State Machine — Basal Ganglia | Повнота станів сесії та переходів */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class StateMachine extends Agent {
  constructor() {
    super({ id:'20', name:'State Machine', organ:'Basal Ganglia', emoji:'🔀',
      focus:'Session state completeness, transition coverage, unknown state fallback' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // Всі очікувані стани бронювання
    const bookingStates = [
      'bk_s1','bk_s2_event','bk_s2_date','bk_s2_dur','bk_s2_loc',
      'bk_s2_budget','bk_s2_comments','bk_s3_name','bk_s3_phone',
      'bk_s3_email','bk_s3_tg','bk_s4'
    ];
    const missing = bookingStates.filter(s => !src.includes(`'${s}'`));
    if (missing.length) this.addFinding('HIGH',`Відсутні стани бронювання: ${missing.join(', ')}`);
    else this.addFinding('OK',`Всі ${bookingStates.length} станів бронювання реалізовані`);

    // switch/case для текстових введень
    if (!src.includes('switch (state)') && !src.includes("switch(state)")) {
      this.addFinding('MEDIUM','switch(state) відсутній — обробка станів може бути неповною');
    } else {
      const caseCount = (src.match(/case 'bk_/g)||[]).length;
      this.addFinding('OK',`switch(state) з ${caseCount} кейсами для бронювання`);
    }

    // Fallback для невідомого стану
    if (!src.includes('default:') && !src.includes('// unknown')) {
      this.addFinding('LOW','Немає default case для невідомих станів — користувач може застрягти');
    } else { this.addFinding('OK','default/fallback для невідомих станів є'); }

    // check_status стан
    if (!src.includes("'check_status'")) this.addFinding('MEDIUM','Стан check_status відсутній — перевірка статусу не працює через введення');
    else this.addFinding('OK','Стан check_status оброблюється');

    // replying стан для адміна
    if (!src.includes("'replying'")) this.addFinding('HIGH','Стан replying відсутній');
    else this.addFinding('OK','Стан replying оброблюється');

    // idle стан як початковий
    if (!src.includes("'idle'")) this.addFinding('HIGH','Стан idle відсутній');
    else this.addFinding('OK','idle стан як початковий визначений');
  }
}

if (require.main === module) new StateMachine().run().then(() => process.exit(0));
module.exports = StateMachine;
