# BASIC.FOOD — вынос в отдельный репозиторий

Дата: 2026-09-04.

**BASIC.FOOD — не часть Infragram.** Это другой продукт: ИИ-агенты магазина
зоотоваров (Supabase edge functions, оркестратор агентов, почтовый агент).
С Infragram он не связан ни кодом, ни данными, ни историей git — у веток
**нет общего предка**, это две независимые истории, случайно оказавшиеся в
одном репозитории.

Именно из-за этого соседства агенты путались в том, над чем работают.

## Где он сейчас

| | |
|---|---|
| Ветка | `claude/ai-agents-business-LCLnI` |
| HEAD | `fb84949b9b6c7f3ba53596e368a78fd4bd52f202` |
| Коммитов | 31 |
| Файлов | 147 |
| Общий предок со стволом Infragram | **отсутствует** |

Состав ветки: `agents/`, `orchestrator.py`, `main.py`, `src/`, `tools/`,
`supabase/`, `database/`, `basicfood-fixes/`, `docs/`, `requirements.txt`,
`.env.example` (Supabase + почтовый агент).

## Что уже сделано

* Из ствола Infragram убран весь код BASIC.FOOD (2026-09-04, см.
  `docs/adr/0001-one-kernel.md`).
* Убраны последние следы в конфигурации ствола: корневой `.env.example`
  переписан под реальный контейнер Infragram (были мёртвые `SUPABASE_*`,
  `EMAIL_IMAP_*`, `EMAIL_FROM=BASIC.FOOD`, которые не читает ни один модуль),
  из докстрок `assistant/` убраны ссылки на бота BASIC.FOOD.
* `assistant/` остаётся спутником Infragram: это личный ИИ-бот владельца на
  OpenRouter, к базе и операциям продукта он не ходит. К BASIC.FOOD отношения
  не имеет — только историческое упоминание в докстроке, теперь снято.

## Что осталось сделать (нужны права владельца)

Создать репозиторий из этой сессии не вышло: GitHub вернул
`403 Resource not accessible by integration` — интеграции не разрешено
создавать репозитории. Те же права не дали удалить ветки (см.
`docs/BRANCH_RESTORE_POINTS.md`).

Шаги для владельца — по порядку.

### 1. Создать пустой репозиторий

На GitHub: **New repository** → имя `basic-food`, приватный,
**без** README/`.gitignore`/лицензии (иначе понадобится `--force` на первом пуше).

### 2. Перелить историю

```bash
git clone --single-branch -b claude/ai-agents-business-LCLnI \
    https://github.com/smith77788/Pablo.git basic-food
cd basic-food
git branch -m claude/ai-agents-business-LCLnI main
git remote set-url origin https://github.com/smith77788/basic-food.git
git push -u origin main
```

История переезжает целиком, все 31 коммит: ветка ни с чем не сливалась,
переписывать нечего.

### 3. Проверить, что доехало

```bash
git -C basic-food log --oneline -1     # ждём fb84949b
git -C basic-food ls-files | wc -l     # ждём 147
```

### 4. Удалить ветку из Pablo

Только после успешной проверки:

```bash
git push origin --delete claude/ai-agents-business-LCLnI
```

Точка восстановления остаётся записанной здесь и в
`docs/BRANCH_RESTORE_POINTS.md`; вернуть ветку можно в любой момент:

```bash
git push origin fb84949b9b6c7f3ba53596e368a78fd4bd52f202:refs/heads/claude/ai-agents-business-LCLnI
```

## Резервная копия истории

Если ветку удалят раньше, чем перельют, история не потеряется: в чат сессии
отдан `basic-food.bundle` (497 КБ) — полный git-бандл ветки. Он клонируется
как обычный репозиторий:

```bash
git clone basic-food.bundle basic-food -b basicfood-main
cd basic-food && git branch -m basicfood-main main
```

`git bundle verify basic-food.bundle` подтверждает: `The bundle records a
complete history` — бандл самодостаточен, донор ему не нужен.

## После переезда

В Infragram ничего чинить не придётся: код BASIC.FOOD уже не в стволе, тестов
на него нет, деплой его не собирает. Достаточно убрать из `CLAUDE.md`,
`README.md` и `docs/BRANCH_RESTORE_POINTS.md` строки «живёт в ветке
`claude/ai-agents-business-LCLnI`» и указать новый репозиторий.
