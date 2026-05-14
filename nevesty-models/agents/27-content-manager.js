/**
 * 📝 Content Manager — Keeps bot content fresh and effective
 * Автоматически обновляет FAQ, About, контакты — следит за качеством контента.
 */
'use strict';
const { Agent, dbRun, dbAll, dbGet, readFile, BOT_PATH } = require('./lib/base');

class ContentManager extends Agent {
  constructor() {
    super({ id: '27', name: 'Content Manager', organ: 'Brand Voice', emoji: '📝',
      focus: 'FAQ quality, About text, contacts completeness, pricing accuracy' });
  }

  async analyze() {
    // ── 1. FAQ — есть ли актуальные вопросы ─────────────────────────────────
    const src = readFile(BOT_PATH);
    const faqCount = (src.match(/Q:|❓|Вопрос:/g) || []).length;
    if (faqCount < 5) {
      this.addFinding('MEDIUM', `FAQ содержит только ${faqCount} вопросов — добавь больше для снижения обращений`);
      await this.expandFAQ(src);
    } else {
      this.addFinding('OK', `FAQ: ${faqCount} вопросов`);
    }

    // ── 2. Контакты заполнены ────────────────────────────────────────────────
    try {
      const phone = await dbGet("SELECT value FROM bot_settings WHERE key='contacts_phone'");
      const email = await dbGet("SELECT value FROM bot_settings WHERE key='contacts_email'");
      const insta = await dbGet("SELECT value FROM bot_settings WHERE key='contacts_insta'");

      const placeholders = ['+7 (900) 000-00-00', 'info@nevesty-models.ru', '@nevesty_models'];
      const unfilled = [
        phone?.value === placeholders[0] && 'телефон',
        email?.value === placeholders[1] && 'email',
        insta?.value  === placeholders[2] && 'instagram',
      ].filter(Boolean);

      if (unfilled.length > 0) {
        this.addFinding('LOW', `Контакты-заглушки не заменены: ${unfilled.join(', ')} — замени на реальные`);
      } else {
        this.addFinding('OK', 'Контакты заполнены');
      }
    } catch {}

    // ── 3. About text не заглушка ────────────────────────────────────────────
    try {
      const about = await dbGet("SELECT value FROM bot_settings WHERE key='about'");
      const isDefault = about?.value?.includes('2018') && about?.value?.includes('200 моделей');
      if (isDefault) {
        this.addFinding('LOW', 'Текст «О нас» содержит демо-данные — обнови с реальной информацией об агентстве');
      } else {
        this.addFinding('OK', 'Текст «О нас» кастомизирован');
      }
    } catch {}

    // ── 4. Описания моделей заполнены ────────────────────────────────────────
    try {
      const noBio = await dbGet(
        "SELECT COUNT(*) as n FROM models WHERE available=1 AND (bio IS NULL OR bio='' OR LENGTH(bio) < 30)"
      );
      if (noBio?.n > 0) {
        this.addFinding('MEDIUM', `${noBio.n} моделей с пустым/кратким описанием — заполни bio для лучшей конверсии`);
      } else {
        this.addFinding('OK', 'Все модели имеют описания');
      }
    } catch {}

    // ── 5. Модели без фото ────────────────────────────────────────────────────
    try {
      const noPhoto = await dbGet(
        "SELECT COUNT(*) as n FROM models WHERE available=1 AND (photo_main IS NULL OR photo_main='')"
      );
      if (noPhoto?.n > 0) {
        this.addFinding('HIGH', `${noPhoto.n} моделей без главного фото — они не будут хорошо выглядеть в каталоге`);
      } else {
        this.addFinding('OK', 'Все модели имеют главное фото');
      }
    } catch {}

    // ── 6. Категории моделей распределены ────────────────────────────────────
    try {
      const byCategory = await dbAll(
        "SELECT category, COUNT(*) as n FROM models WHERE available=1 GROUP BY category"
      );
      const cats = byCategory.map(r => `${r.category}:${r.n}`).join(', ');
      this.addFinding('INFO', `Распределение по категориям: ${cats}`);

      const uncategorized = byCategory.find(r => !r.category || r.category === '');
      if (uncategorized?.n > 0) {
        this.addFinding('LOW', `${uncategorized.n} моделей без категории — назначь категорию для фильтрации`);
      }
    } catch {}
  }

  async expandFAQ(src) {
    // FAQ расширяется через bot_settings — если там есть faq ключ
    try {
      const existing = await dbGet("SELECT value FROM bot_settings WHERE key='faq_extra'");
      if (!existing) {
        const faqExtra = [
          'Как быстро вы можете предоставить модель? — Обычно в течение 24-48 часов после подтверждения заявки.',
          'Работаете ли вы в других городах? — Да, работаем по всей России.',
          'Можно ли выбрать модель самому? — Конечно! Используйте раздел Каталог чтобы выбрать понравившуюся модель.',
          'Что входит в стоимость? — Работа модели по согласованному тарифу. Дорога, визаж — по договорённости.',
        ].join('\n\n');
        await dbRun(
          "INSERT INTO bot_settings (key, value, updated_at) VALUES ('faq_extra', ?, CURRENT_TIMESTAMP)",
          [faqExtra]
        );
        this.addFixed('Добавлены дополнительные FAQ-ответы в настройки');
      }
    } catch {}
  }
}

if (require.main === module) new ContentManager().run().then(() => process.exit(0));
module.exports = ContentManager;
