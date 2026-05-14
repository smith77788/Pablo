/** 📝 Markdown Safety — Protective Membrane | MarkdownV2 escaping для всіх user data */
const { Agent, readFile, BOT_PATH } = require('./lib/base');

class MarkdownSafety extends Agent {
  constructor() {
    super({ id:'14', name:'Markdown Safety', organ:'Protective Membrane', emoji:'📝',
      focus:'MarkdownV2 escape coverage, mixed parse_mode detection' });
  }
  async analyze() {
    const src = readFile(BOT_PATH);

    // 1. Функція esc() визначена
    if (!src.includes('function esc(')) this.addFinding('CRITICAL','Функція esc() для MarkdownV2 escaping відсутня!');
    else this.addFinding('OK','Функція esc() визначена');

    // 2. esc() застосовується до user-controlled полів
    const userFields = ['client_name','client_phone','order_number','model_name','o.location','o.budget'];
    const escaped = userFields.filter(f => src.includes(`esc(${f})`) || src.includes(`esc(o.${f.replace('o.','')})`));
    const notEscaped = userFields.filter(f => !escaped.includes(f) && src.includes(f));
    if (notEscaped.length > 2) this.addFinding('HIGH',`Поля без esc(): ${notEscaped.join(', ')} — можливий Markdown injection`);
    else this.addFinding('OK',`Пользувацькі поля захищені esc()`);

    // 3. Змішування Markdown і MarkdownV2
    const mdV2Count = (src.match(/parse_mode:\s*['"]MarkdownV2['"]/g)||[]).length;
    const mdCount   = (src.match(/parse_mode:\s*['"]Markdown['"]/g)||[]).length;
    if (mdV2Count > 0 && mdCount > 0) {
      this.addFinding('MEDIUM',`Змішується Markdown (${mdCount}) і MarkdownV2 (${mdV2Count}) — різні правила escaping`);
    } else if (mdV2Count > 0) {
      this.addFinding('INFO',`Використовується MarkdownV2: ${mdV2Count} повідомлень`);
    } else {
      this.addFinding('INFO',`Використовується Markdown: ${mdCount} повідомлень`);
    }

    // 4. Fallback на plain text при помилці parse
    if (!src.includes('parse_mode') || !src.includes('parse entities')) {
      const hasFallback = src.includes('parse_mode: undefined') || src.includes('parse_mode &&');
      if (!hasFallback) this.addFinding('MEDIUM','Fallback на plain text при помилці Markdown відсутній');
      else this.addFinding('OK','Fallback на plain text при помилці парсингу є');
    } else { this.addFinding('OK','Fallback на plain text при помилці парсингу є'); }

    // 5. Спеціальні символи у статичних рядках MarkdownV2
    const v2Msgs = src.match(/parse_mode:\s*'MarkdownV2'[\s\S]{0,500}/g) || [];
    const danglers = v2Msgs.filter(m => /[()!.|]/.test(m.replace(/\\[()!.|]/g,'')));
    if (danglers.length > 0) this.addFinding('LOW',`${danglers.length} MarkdownV2 повідомлень можуть мати неекрановані спецсимволи`);
    else this.addFinding('OK','Спецсимволи у MarkdownV2 рядках екрановані');
  }
}

if (require.main === module) new MarkdownSafety().run().then(() => process.exit(0));
module.exports = MarkdownSafety;
