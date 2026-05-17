'use strict';
const { Agent, dbAll, _dbGet, logAgent } = require('./lib/base');

/**
 * ✍️ ContentGenerator — генерирует контент для агентства через Claude API:
 * - Посты для Telegram-канала (на основе топ-моделей недели)
 * - Улучшенные описания моделей (если bio пустой или < 50 символов)
 * - FAQ ответы для часто задаваемых вопросов
 */
class ContentGenerator extends Agent {
  constructor() {
    super({
      id: '29',
      name: 'Content Generator',
      organ: 'AI Factory',
      emoji: '✍️',
      focus: 'Generate Telegram posts, model descriptions, and FAQ answers via Claude API',
    });
  }

  async analyze() {
    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      this.addFinding('LOW', 'ContentGenerator: ANTHROPIC_API_KEY не настроен — пропуск генерации контента');
      return;
    }

    await this.generateTelegramPost(apiKey);
    await this.improveModelBios(apiKey);
    await this.generateFaqAnswers(apiKey);
  }

  async generateTelegramPost(apiKey) {
    // Find top models by orders this week
    let topModels;
    try {
      topModels = await dbAll(
        `SELECT m.name, m.category, m.height, m.age, COUNT(o.id) as cnt
         FROM models m
         LEFT JOIN orders o ON o.model_id = m.id AND o.created_at >= datetime('now', '-7 days')
         WHERE m.archived = 0 OR m.archived IS NULL
         GROUP BY m.id
         ORDER BY cnt DESC
         LIMIT 3`
      );
    } catch {
      topModels = [];
    }

    if (!topModels.length) return;

    const modelsList = topModels
      .map(
        (m, i) => `${i + 1}. ${m.name} — ${m.category || 'универсальная'}, ${m.height || '?'} см, ${m.age || '?'} лет`
      )
      .join('\n');

    try {
      const response = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'content-type': 'application/json',
        },
        body: JSON.stringify({
          model: 'claude-haiku-4-5',
          max_tokens: 400,
          system:
            'Ты — SMM менеджер модельного агентства Nevesty Models. Пишешь посты для Telegram-канала. Тон: профессиональный, стильный, не кричащий. Без хэштегов. Без emoji-спама (max 3). Длина 3-4 предложения.',
          messages: [
            {
              role: 'user',
              content: `Напиши пост для Telegram-канала агентства о популярных моделях этой недели:\n${modelsList}\n\nПост должен вызывать интерес к бронированию.`,
            },
          ],
        }),
      });
      const data = await response.json();
      const post = data.content?.[0]?.text || '';
      if (post) {
        this.addFinding('INFO', `📱 Пост для Telegram-канала:\n${post.slice(0, 300)}`);
        await logAgent('ContentGenerator', `Generated Telegram post (${post.length} chars)`);
      }
    } catch (e) {
      await logAgent('ContentGenerator', `Post generation error: ${e.message}`);
    }
  }

  async improveModelBios(apiKey) {
    // Find models with empty or short bio
    let models;
    try {
      models = await dbAll(
        `SELECT id, name, category, height, age, bio
         FROM models
         WHERE (bio IS NULL OR LENGTH(TRIM(bio)) < 50)
           AND (archived = 0 OR archived IS NULL)
         LIMIT 3`
      );
    } catch {
      return;
    }

    if (!models.length) {
      this.addFinding('OK', 'Все модели имеют достаточно длинное bio');
      return;
    }

    for (const model of models) {
      try {
        const response = await fetch('https://api.anthropic.com/v1/messages', {
          method: 'POST',
          headers: {
            'x-api-key': apiKey,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
          },
          body: JSON.stringify({
            model: 'claude-haiku-4-5',
            max_tokens: 200,
            system:
              'Ты — копирайтер модельного агентства. Пишешь короткое профессиональное описание модели (2-3 предложения, 80-120 слов). Тон: элегантный, без преувеличений.',
            messages: [
              {
                role: 'user',
                content: `Напиши описание для модели:\nИмя: ${model.name}\nКатегория: ${model.category || 'не указана'}\nРост: ${model.height || '?'} см\nВозраст: ${model.age || '?'} лет`,
              },
            ],
          }),
        });
        const data = await response.json();
        const bio = data.content?.[0]?.text?.trim() || '';
        if (bio && bio.length > 30) {
          this.addFinding('MEDIUM', `✍️ Предлагаемое bio для "${model.name}": ${bio.slice(0, 150)}...`);
        }
        await new Promise(r => setTimeout(r, 500)); // rate limit
      } catch (e) {
        await logAgent('ContentGenerator', `Bio generation error for ${model.name}: ${e.message}`);
      }
    }
  }

  async generateFaqAnswers(apiKey) {
    // Find FAQ questions without answers or with short answers
    let faqs;
    try {
      faqs = await dbAll(
        `SELECT id, question, answer FROM faq
         WHERE answer IS NULL OR LENGTH(TRIM(answer)) < 20
         LIMIT 3`
      );
    } catch {
      return;
    }

    if (!faqs.length) return;

    for (const faq of faqs) {
      try {
        const response = await fetch('https://api.anthropic.com/v1/messages', {
          method: 'POST',
          headers: {
            'x-api-key': apiKey,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
          },
          body: JSON.stringify({
            model: 'claude-haiku-4-5',
            max_tokens: 200,
            system:
              'Ты — менеджер модельного агентства Nevesty Models. Отвечаешь на вопросы клиентов. Ответы короткие (2-3 предложения), дружелюбные, информативные.',
            messages: [
              {
                role: 'user',
                content: `Вопрос клиента: "${faq.question}"\n\nДай чёткий ответ от имени агентства.`,
              },
            ],
          }),
        });
        const data = await response.json();
        const answer = data.content?.[0]?.text?.trim() || '';
        if (answer && answer.length > 15) {
          this.addFinding('LOW', `❓ FAQ "${faq.question.slice(0, 50)}": ${answer.slice(0, 100)}...`);
        }
        await new Promise(r => setTimeout(r, 500));
      } catch (e) {
        await logAgent('ContentGenerator', `FAQ generation error: ${e.message}`);
      }
    }
  }
}

if (require.main === module) new ContentGenerator().run().then(() => process.exit(0));
module.exports = ContentGenerator;
