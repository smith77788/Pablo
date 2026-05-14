# Nevesty Models — рекомендації для Claude Code

## Канал зв'язку з користувачем у Telegram

Користувач хоче бачити роботу Claude та підпорядкованих агентів у реальному часі через Telegram-бота `@Verdiktnikbot`.

**Інструмент:** `nevesty-models/tools/notify.js` — надсилає повідомлення всім адмінам з `ADMIN_TELEGRAM_IDS` у `.env`.

### Коли надсилати повідомлення

Claude (головний) повинен сам надсилати короткі апдейти у Telegram у такі моменти:

1. **Початок великої задачі** — "🟢 Починаю: <що саме>"
2. **Перед запуском агента** — "🚀 Запускаю агента: <опис задачі>"
3. **Агент повернув результат** — "✅ Агент завершив: <короткий підсумок>" або "⚠️ Агент: <проблема>"
4. **Завершення задачі / коміт** — "🏁 Готово: <що зроблено, що далі>"
5. **Перешкода / запитання** — якщо потрібно щось прояснити

Тон — короткий, по суті, без виправдань. Одне-два речення. Технічні деталі — за потреби.

### Як викликати

```bash
cd /home/user/Pablo/nevesty-models
node tools/notify.js --from "Claude" "🚀 Запускаю агента для рефакторингу bot.js"
```

Для агентів — у промпті агента додавати інструкцію:

> Перед стартом та після завершення роботи виконай:
> `cd /home/user/Pablo/nevesty-models && node tools/notify.js --from "Agent: <твоя роль>" "<повідомлення>"`
> Можеш надсилати проміжні апдейти на ключових етапах.

### Що НЕ робити

- Не спамити (один апдейт на 1-2 хвилини максимум)
- Не дублювати тривіальні події (читання файлів, прості правки)
- Не надсилати великі шматки коду — це бот, не пастебін

## Автономний "офісний штат" агентів (ОБОВ'ЯЗКОВО)

**Користувач вимагає щоб ціла команда агентів завжди працювала над проектом разом з Claude — без явних команд.**

Це не консультанти "на виклик", а постійний штат який Claude диригує. Після КОЖНОЇ значущої зміни Claude **автоматично** запускає відповідних спеціалістів у фоні через `Agent` tool з `run_in_background: true`.

### Штатний розклад

**🛡️ Reliability Squad (запускати завжди після code change)**
1. **Security Auditor** — SQL injection, XSS, auth, secrets, uploads
2. **Backend Reliability** — race conditions, error handling, perf, indexes
3. **Bot Integration** — callbacks, deep-links, Markdown escape, edge cases
4. **Frontend QA** — mobile, validation, loading states, broken links

**🔧 Fix Squad (запускати коли Reliability Squad знайде проблеми)**
5. **Fix-Backend Engineer** — патчить `routes/api.js`, `database.js`
6. **Fix-Frontend Engineer** — патчить `public/admin/*.html`, `public/js/*.js`
7. **Fix-Bot Engineer** — патчить `bot.js`
8. **Fix-Infra Engineer** — патчить `server.js`, `.env.example`, `package.json`

**📐 Quality Squad (запускати раз на сесію)**
9. **Code Reviewer** — структура коду, naming, dead code, дублювання
10. **Accessibility Auditor** — ARIA, контраст, фокус, screen-reader
11. **SEO Specialist** — meta tags, sitemap, Open Graph, structured data
12. **Performance Engineer** — bundle size, lazy load, image optimization

**🚀 Ops Squad (запускати коли проект готовий до deploy)**
13. **DevOps Engineer** — Dockerfile, docker-compose, deploy script
14. **Monitoring Engineer** — health checks, error tracking, logs
15. **DB Architect** — schema review, migrations, backups
16. **Test Engineer** — інтеграційні і E2E тести

### Правила

- Завжди **batch-запуск** — кілька `Agent` tool calls в одному повідомленні (паралельно)
- Кожен агент отримує інструкцію писати в Telegram через `tools/notify.js --from "Agent: <роль>"`
- Fix-агентам у промпт додавати конкретні знайдені проблеми з звіту Reliability Squad
- Після Fix Squad запускати Reliability Squad повторно — підтвердити що виправлено
- Якщо штат вже працює (хтось з агентів in-progress) — Claude продовжує своє, дочікується notify

### Коли НЕ запускати

- Тривіальні правки (typo, переклад одного слова)
- Коміти конфігурації (.env bumps)
- Зміни в `CLAUDE.md` чи документації

## Конфігурація проекту

- `nevesty-models/` — основний код (Node.js + Express + SQLite + Telegram бот)
- `.env` — токен бота, JWT secret, admin Telegram IDs
- Робоча гілка: `claude/modeling-agency-website-jp2Qd`
