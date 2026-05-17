/**
 * 🎨 Creative Department — Copywriting, brand voice, storytelling
 *
 * Agents:
 *   CopywriterAI      — generates model descriptions and channel posts
 *   BrandVoiceKeeper  — checks text tone against brand guidelines
 *   StorytellingAgent — creates success-story narratives for case studies
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const { Agent, dbRun, dbGet, dbAll, logAgent } = require('../lib/base');

// ─── Brand voice guidelines ───────────────────────────────────────────────────
const BRAND_VOICE = {
  name: 'Nevesty Models',
  tone: 'профессиональный, тёплый, уверенный, без пафоса',
  language: 'русский',
  forbidden: [
    'лучшие в мире',
    'номер один',
    'уникальный',
    'эксклюзивный',
    'гарантируем 100%',
    'без проблем',
    'самый дешёвый',
  ],
  required: {
    professional: true, // formal but approachable
    concise: true, // no filler words
    ctaPresent: true, // every post ends with a call to action
  },
  maxHashtags: 5,
};

// ─── Shared Claude API helper ─────────────────────────────────────────────────
async function callClaude({ systemPrompt, userPrompt, maxTokens = 600 }) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error('ANTHROPIC_API_KEY not configured');

  const response = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: maxTokens,
      system: systemPrompt,
      messages: [{ role: 'user', content: userPrompt }],
    }),
  });

  if (!response.ok) {
    const err = await response.text();
    throw new Error(`Claude API error ${response.status}: ${err.slice(0, 200)}`);
  }

  const data = await response.json();
  return data.content?.[0]?.text ?? '';
}

/** Write result to agent_logs + console */
async function factoryLog(agentName, message) {
  console.log(`[${agentName}] ${message}`);
  await logAgent(agentName, message);
}

// ═════════════════════════════════════════════════════════════════════════════
// 1. CopywriterAI
// ═════════════════════════════════════════════════════════════════════════════
class CopywriterAI extends Agent {
  constructor() {
    super({
      id: 'creative-01',
      name: 'CopywriterAI',
      organ: 'Creative Department',
      emoji: '✏️',
      focus: 'Generate model bios and channel posts via Claude API',
    });
  }

  /**
   * Generate a bio for a model that has an empty or very short bio.
   */
  async generateModelBio(model) {
    const prompt = [
      `Имя: ${model.name}`,
      model.age ? `Возраст: ${model.age} лет` : null,
      model.height ? `Рост: ${model.height} см` : null,
      model.category ? `Категория: ${model.category}` : null,
      model.hair_color ? `Цвет волос: ${model.hair_color}` : null,
      model.eye_color ? `Цвет глаз: ${model.eye_color}` : null,
    ]
      .filter(Boolean)
      .join('\n');

    return callClaude({
      systemPrompt: [
        `Ты — копирайтер модельного агентства ${BRAND_VOICE.name}.`,
        `Тон: ${BRAND_VOICE.tone}.`,
        'Напиши краткое профессиональное описание модели (80-120 слов) на русском языке.',
        'Подчеркни её сильные стороны, профессионализм и универсальность.',
        'Не используй клише вроде "уникальная" или "лучшая".',
        'Заканчивай призывом к действию: записаться, связаться с агентством.',
      ].join(' '),
      userPrompt: prompt,
      maxTokens: 250,
    });
  }

  /**
   * Generate a promotional post for a Telegram / Instagram channel.
   * @param {'new_model'|'promo'|'tip'} postType
   * @param {object} [context] — optional extra data (model info, promo text, etc.)
   */
  async generateChannelPost(postType, context = {}) {
    const typePrompts = {
      new_model: `Напиши пост "Знакомьтесь с нашей моделью" для Telegram-канала. Данные модели:\n${JSON.stringify(context)}`,
      promo: `Напиши промо-пост о специальном предложении от агентства:\n${JSON.stringify(context)}`,
      tip: `Напиши полезный совет для клиентов о работе с моделями на мероприятиях. Тема: ${context.topic || 'общие советы'}.`,
    };

    return callClaude({
      systemPrompt: [
        `Ты — SMM-копирайтер агентства ${BRAND_VOICE.name}.`,
        `Тон: ${BRAND_VOICE.tone}.`,
        'Пост для Telegram. Формат: 1-2 абзаца + 3-5 хэштегов.',
        'Заканчивай призывом к действию (написать в бота, оставить заявку).',
        `Запрещённые слова: ${BRAND_VOICE.forbidden.join(', ')}.`,
      ].join(' '),
      userPrompt: typePrompts[postType] || typePrompts.tip,
      maxTokens: 300,
    });
  }

  async analyze() {
    // ── 1. Find models with empty/short bio ──────────────────────────────────
    let modelsNeedingBio;
    try {
      modelsNeedingBio = await dbAll(
        `SELECT id, name, age, height, category, hair_color, eye_color
         FROM models
         WHERE available = 1
           AND (bio IS NULL OR bio = '' OR LENGTH(bio) < 40)
         ORDER BY id ASC
         LIMIT 3`
      );
    } catch (e) {
      this.addFinding('HIGH', `CopywriterAI: не удалось загрузить модели: ${e.message}`);
      return;
    }

    let biosWritten = 0;
    for (const model of modelsNeedingBio) {
      try {
        const bio = await this.generateModelBio(model);
        await dbRun('UPDATE models SET bio = ? WHERE id = ?', [bio, model.id]);
        await factoryLog(this.name, `Bio written for model "${model.name}" (id=${model.id})`);
        biosWritten++;
        this.addFixed(`Описание написано для модели ${model.name}`);
      } catch (e) {
        this.addFinding('MEDIUM', `Не удалось написать bio для ${model.name}: ${e.message}`);
      }
    }

    if (modelsNeedingBio.length === 0) {
      this.addFinding('OK', 'Все модели имеют описания — дополнительный копирайтинг не нужен');
    } else if (biosWritten > 0) {
      this.addFinding('INFO', `✏️ Написано ${biosWritten} описаний моделей`);
    }

    // ── 2. Generate a weekly channel post (if not generated today) ───────────
    try {
      const lastPost = await dbGet(`SELECT value, updated_at FROM bot_settings WHERE key = 'last_channel_post_date'`);
      const today = new Date().toISOString().slice(0, 10);
      const lastDate = lastPost?.updated_at?.slice(0, 10);

      if (lastDate !== today) {
        const post = await this.generateChannelPost('tip', { topic: 'как выбрать модель для фотосъёмки' });
        await dbRun(
          `INSERT INTO bot_settings (key, value, updated_at) VALUES ('last_channel_post', ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
          [post]
        );
        await dbRun(
          `INSERT INTO bot_settings (key, value, updated_at) VALUES ('last_channel_post_date', ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
          [today]
        );
        await factoryLog(this.name, `Weekly channel post generated for ${today}`);
        this.addFixed('Сгенерирован пост для Telegram-канала (сохранён в bot_settings[last_channel_post])');
      } else {
        this.addFinding('OK', `Пост для канала уже сгенерирован сегодня (${today})`);
      }
    } catch (e) {
      this.addFinding('LOW', `Не удалось сгенерировать пост для канала: ${e.message}`);
    }
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 2. BrandVoiceKeeper
// ═════════════════════════════════════════════════════════════════════════════
class BrandVoiceKeeper extends Agent {
  constructor() {
    super({
      id: 'creative-02',
      name: 'BrandVoiceKeeper',
      organ: 'Creative Department',
      emoji: '🎙️',
      focus: 'Audit model bios and settings texts for brand voice compliance',
    });
  }

  /**
   * Rule-based compliance check (fast, no API call needed).
   * Returns { passed: boolean, violations: string[], suggestions: string[] }
   */
  checkCompliance(text) {
    const violations = [];
    const suggestions = [];

    if (!text || text.trim().length === 0) {
      return { passed: false, violations: ['Текст пустой'], suggestions: ['Добавить описание'] };
    }

    // Forbidden words
    for (const word of BRAND_VOICE.forbidden) {
      if (text.toLowerCase().includes(word.toLowerCase())) {
        violations.push(`Запрещённое слово: "${word}"`);
        suggestions.push(`Заменить "${word}" на более конкретное описание`);
      }
    }

    // Too many exclamation marks (спам)
    const exclamations = (text.match(/!/g) || []).length;
    if (exclamations > 3) {
      violations.push(`Слишком много восклицательных знаков (${exclamations})`);
      suggestions.push('Оставить не более 2 восклицательных знаков');
    }

    // All-caps words (кричащий текст)
    const allCapsWords = text.match(/\b[А-ЯA-Z]{4,}\b/g) || [];
    if (allCapsWords.length > 2) {
      violations.push(`Слова полностью заглавными буквами: ${allCapsWords.slice(0, 3).join(', ')}`);
      suggestions.push('Использовать нормальный регистр');
    }

    // Too short (less than 30 chars) — not informative
    if (text.trim().length < 30) {
      violations.push('Текст слишком короткий (менее 30 символов)');
      suggestions.push('Расширить описание — минимум 80-100 слов для bio');
    }

    // Hashtag overload in channel posts
    const hashtags = (text.match(/#\w+/g) || []).length;
    if (hashtags > BRAND_VOICE.maxHashtags) {
      violations.push(`Слишком много хэштегов (${hashtags}, максимум ${BRAND_VOICE.maxHashtags})`);
      suggestions.push(`Сократить до ${BRAND_VOICE.maxHashtags} хэштегов`);
    }

    return {
      passed: violations.length === 0,
      violations,
      suggestions,
    };
  }

  /**
   * Use Claude to suggest a brand-compliant rewrite if there are violations.
   */
  async suggestRewrite(text, violations) {
    return callClaude({
      systemPrompt: [
        `Ты — редактор агентства ${BRAND_VOICE.name}.`,
        `Тон бренда: ${BRAND_VOICE.tone}.`,
        'Перепиши текст, устраняя нарушения фирменного стиля, сохраняя смысл.',
        `Нарушения: ${violations.join('; ')}.`,
        'Верни только исправленный текст, без пояснений.',
      ].join(' '),
      userPrompt: text,
      maxTokens: 400,
    });
  }

  async analyze() {
    let auditCount = 0;
    let failCount = 0;

    // ── Audit model bios ─────────────────────────────────────────────────────
    let models;
    try {
      models = await dbAll(
        `SELECT id, name, bio FROM models WHERE available = 1 AND bio IS NOT NULL AND bio != '' LIMIT 20`
      );
    } catch (e) {
      this.addFinding('HIGH', `BrandVoiceKeeper: ошибка загрузки моделей: ${e.message}`);
      return;
    }

    for (const model of models) {
      auditCount++;
      const { passed, violations, suggestions } = this.checkCompliance(model.bio);

      if (!passed) {
        failCount++;
        this.addFinding(
          violations.length > 2 ? 'MEDIUM' : 'LOW',
          `Нарушение brand voice в bio модели "${model.name}": ${violations.join('; ')}`
        );

        // Auto-rewrite if API key is available and violations are severe
        if (process.env.ANTHROPIC_API_KEY && violations.length >= 2) {
          try {
            const rewrite = await this.suggestRewrite(model.bio, violations);
            await dbRun('UPDATE models SET bio = ? WHERE id = ?', [rewrite, model.id]);
            this.addFixed(`Bio модели "${model.name}" исправлен под brand voice`);
            await factoryLog(this.name, `Rewrote bio for "${model.name}" (violations: ${violations.join(', ')})`);
          } catch (e) {
            this.addFinding('LOW', `Не удалось перезаписать bio ${model.name}: ${e.message}`);
          }
        } else {
          await factoryLog(
            this.name,
            `Brand voice violation in "${model.name}" bio — suggestions: ${suggestions.join('; ')}`
          );
        }
      }
    }

    // ── Audit channel post in bot_settings ───────────────────────────────────
    try {
      const post = await dbGet("SELECT value FROM bot_settings WHERE key = 'last_channel_post'");
      if (post?.value) {
        auditCount++;
        const { passed, violations } = this.checkCompliance(post.value);
        if (!passed) {
          failCount++;
          this.addFinding('LOW', `Нарушение brand voice в посте для канала: ${violations.join('; ')}`);
        }
      }
    } catch {}

    if (auditCount === 0) {
      this.addFinding('OK', 'Нет текстов для аудита brand voice');
    } else if (failCount === 0) {
      this.addFinding('OK', `Brand voice OK — проверено ${auditCount} текстов`);
    } else {
      this.addFinding('INFO', `Проверено: ${auditCount} текстов, нарушений: ${failCount}`);
    }
  }
}

// ═════════════════════════════════════════════════════════════════════════════
// 3. StorytellingAgent
// ═════════════════════════════════════════════════════════════════════════════
class StorytellingAgent extends Agent {
  constructor() {
    super({
      id: 'creative-03',
      name: 'StorytellingAgent',
      organ: 'Creative Department',
      emoji: '📖',
      focus: 'Create success-story case narratives from completed orders',
    });
  }

  /**
   * Build a case-study story from a completed order.
   * @param {object} order — completed order row
   * @param {object|null} model — model row (may be null)
   * @returns {string} narrative text
   */
  async buildStory(order, model) {
    const modelInfo = model
      ? `Модель: ${model.name}, ${model.age} лет, категория ${model.category}.`
      : 'Модель из каталога агентства.';

    const userPrompt = [
      `Тип мероприятия: ${order.event_type}`,
      order.event_date ? `Дата: ${order.event_date}` : null,
      order.location ? `Город: ${order.location}` : null,
      modelInfo,
      order.comments ? `Детали: ${order.comments}` : null,
    ]
      .filter(Boolean)
      .join('\n');

    return callClaude({
      systemPrompt: [
        `Ты — контент-маркетолог агентства ${BRAND_VOICE.name}.`,
        'Напиши короткую историю успеха (кейс) для сайта или соцсетей на русском языке.',
        'Структура: 1) Задача клиента, 2) Решение агентства, 3) Результат.',
        `Тон: ${BRAND_VOICE.tone}. Объём: 120-180 слов.`,
        'Не используй выдуманные цифры. Не называй клиента по имени — "наш клиент".',
      ].join(' '),
      userPrompt,
      maxTokens: 350,
    });
  }

  async analyze() {
    // Find recently completed orders that don't yet have a story
    let orders;
    try {
      orders = await dbAll(
        `SELECT id, order_number, event_type, event_date, location,
                model_id, comments, status
         FROM orders
         WHERE status IN ('confirmed', 'completed')
           AND (admin_notes IS NULL OR admin_notes NOT LIKE '%[Story]%')
         ORDER BY updated_at DESC
         LIMIT 3`
      );
    } catch (e) {
      this.addFinding('HIGH', `StorytellingAgent: ошибка загрузки заявок: ${e.message}`);
      return;
    }

    if (!orders.length) {
      this.addFinding('OK', 'Нет завершённых заявок для создания историй успеха');
      return;
    }

    let storiesWritten = 0;
    for (const order of orders) {
      // Load the model if linked
      let model = null;
      if (order.model_id) {
        try {
          model = await dbGet('SELECT name, age, category FROM models WHERE id = ?', [order.model_id]);
        } catch {}
      }

      try {
        const story = await this.buildStory(order, model);

        // Save story to admin_notes with a tag
        const storyEntry = `[Story]\n${story}`;
        await dbRun(
          `UPDATE orders
           SET admin_notes = CASE
                 WHEN admin_notes IS NULL OR admin_notes = '' THEN ?
                 ELSE admin_notes || char(10) || ?
               END,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?`,
          [storyEntry, storyEntry, order.id]
        );

        // Also persist to bot_settings as a list for easy retrieval
        const storyKey = `case_story_${order.order_number}`;
        await dbRun(
          `INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
          [storyKey, story]
        ).catch(() => {});

        await factoryLog(this.name, `Story created for order #${order.order_number} (${order.event_type})`);
        storiesWritten++;
        this.addFixed(`История успеха написана для заявки #${order.order_number}`);
      } catch (e) {
        this.addFinding('MEDIUM', `Не удалось создать историю для #${order.order_number}: ${e.message}`);
      }
    }

    if (storiesWritten > 0) {
      this.addFinding('INFO', `📖 Создано ${storiesWritten} историй успеха`);
    }
  }
}

// ─── Run all three agents when invoked directly ───────────────────────────────
async function runCreativeDepartment() {
  console.log('🎨 Creative Department — запуск...\n');

  const agents = [new CopywriterAI(), new BrandVoiceKeeper(), new StorytellingAgent()];

  for (const agent of agents) {
    console.log(`\n${agent.emoji} ${agent.name}`);
    try {
      await agent.run({ silent: true });
      agent.findings.forEach(f => console.log(`  ${f.sev} ${f.msg}`));
      agent.fixed.forEach(fx => console.log(`  🔧 ${fx}`));
    } catch (e) {
      console.error(`  ❌ Error: ${e.message}`);
    }
  }

  console.log('\n🎨 Creative Department — завершено.');
}

if (require.main === module) runCreativeDepartment().then(() => process.exit(0));

module.exports = { CopywriterAI, BrandVoiceKeeper, StorytellingAgent };
