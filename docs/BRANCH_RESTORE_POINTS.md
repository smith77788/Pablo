# Точки восстановления веток на момент консолидации

Снято 2026-09-04 перед приведением репозитория к одному проекту.
Любую ветку можно вернуть: `git checkout -b <имя> <SHA>`.
Удалённые из ствола деревья остаются в истории и в перечисленных ветках.

| Ветка | SHA | Последний коммит | Что в ней |
|---|---|---|---|
| `claude/telegram-bot-services-xfAh6` | `340024a84cc4204f1d75e48d3790dfa51c722cf0` | 2026-09-04 | **СТВОЛ** — единственная рабочая ветка, с неё идёт деплой |
| `claude/telegram-os-automation-zc83l4` | `06143842648d3c116e921131e9c0e5456748b1f7` | 2026-09-03 | влита в ствол полностью (PR #14) |
| `claude/budget-leak-modules-dyb7fr` | `36071f30894d5f49ea07e526dfc4020772c6ffef` | 2026-09-01 | budget_radar — ВОЗВРАЩЁН в ствол и подключён |
| `claude/infragram-production-audit-ebyltk` | `43b26a010d792d6d8097ab0a2effbd79d8554b87` | 2026-09-01 | SSRF-гард — ВОЗВРАЩЁН в ствол |
| `claude/infragram-wb-chat-automation-ywt70h` | `1785456857c789e0ed7af586a9ab8622d6182777` | 2026-08-26 | модуль wb_chat — ВОЗВРАЩЁН в ствол (не подключён, решение продуктовое) |
| `claude/docs-memory-archive` | `a1ac11937bf08d65917e1d60522f74dc9dbd95d5` | 2026-08-09 | свод правил .botmother и стандарты — ВОЗВРАЩЕНЫ в ствол |
| `claude/telegram-bot-clean-copy-ouid6x` | `13070e5810f4012ac7d4f1793592cfe926436e0d` | 2026-08-08 | старая копия проекта |
| `claude/telegram-csam-blocking-v53d9q` | `f096f3094e452e601dc51c6c2c6cc4b24774df2c` | 2026-08-07 | детская безопасность для BASIC.FOOD (agents/tools/supabase) |
| `agent-memory-system` | `11a94d3ef610d22bc541a88de0ff3f62f72a9911` | 2026-07-21 | шаблоны протокола агентов (отдельный репозиторий по смыслу) |
| `main` | `1d3d4ea35a5a425e1eeddceaa3f166e114ef06a4` | 2026-07-21 | старый снимок проекта (797 файлов против 1500 в стволе), .botmother возвращён |
| `claude/telegram-bot-setup-bh09k8` | `46ca80950af7a3b410ddcae7a26f1e427867ca07` | 2026-07-21 | старый снимок настройки |
| `claude/telegram-bot-services-review-0uqb6v` | `8d3e4b5df8181565a3d21c08e9ea92ade8a5cac1` | 2026-07-19 | старый снимок ревью |
| `claude/telegram-product-audit-yvz4yw` | `f4f1e7652e6ee61ce376efddf9defe8293ec8b07` | 2026-07-08 | старый снимок аудита |
| `claude/pablo-tg-managers-pyT3A` | `420808a040cf962ea17e411de7466b69e1cdaea9` | 2026-07-04 | старый снимок |
| `tg-manager` | `e303140d7dbd4248979a08adb96bd52ba2419c02` | 2026-06-01 | старый снимок tg-manager |
| `railway/fix-deploy-a50a71` | `4162a8cc41948c1e9f77ced914960f4b08b2c4a5` | 2026-06-01 | разовая правка деплоя Railway |
| `claude/ton-security-audit-PoYRa` | `06ebde714510f16c12a67d9018bfe7052c8c05b3` | 2026-05-31 | старый аудит TON |
| `railway/fix-deploy-081416` | `238d048c71934444cbb403ad3699debd8659d3e2` | 2026-05-31 | разовая правка деплоя Railway |
| `railway/fix-deploy-ca90b0` | `1ede467c3b34b756d295b783c6d9707f5ceab6b9` | 2026-05-31 | разовая правка деплоя Railway |
| `claude/modeling-agency-website-jp2Qd` | `7349f0398d8f6b944e0a45456b11864f3db89049` | 2026-05-22 | сайт модельного агентства — посторонний проект |
| `claude/ai-agents-business-LCLnI` | `fb84949b9b6c7f3ba53596e368a78fd4bd52f202` | 2026-05-17 | **BASIC.FOOD** — другой продукт (ИИ-агенты магазина зоотоваров), хранится здесь |
