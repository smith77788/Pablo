"""Онбординг-чеклист активации: путь новичка от нуля до первого результата.

Проблема конверсии: новый пользователь открывает мини-апп и видит стену из
десятков модулей — не понимает, с чего начать, и уходит, не активировавшись
(а значит и не покупает подписку). Чеклист сводит старт к 2–3 обязательным шагам
с действием в один тап и САМ ИСЧЕЗАЕТ, как только пользователь активирован
(подключил аккаунты и запустил первое действие) — не мозолит глаза ветеранам.

build_checklist — чистая функция (тестируется без БД): по счётчикам состояния
собирает шаги. Эндпоинт лишь подставляет счётчики.
"""
from __future__ import annotations


def build_checklist(
    *,
    accounts: int,
    accounts_with_proxy: int,
    proxies: int,
    ops_total: int,
) -> dict:
    """Собрать чеклист активации из счётчиков состояния владельца.

    Ядро активации = есть аккаунты И запущено первое действие. Прокси —
    РЕКОМЕНДУЕМЫЙ шаг (без прокси работает прямой host-IP), поэтому он не
    блокирует активацию и помечен optional. Когда ядро выполнено — activated=True,
    и поверхность прячет чеклист целиком.
    """
    accounts = max(0, int(accounts or 0))
    accounts_with_proxy = max(0, int(accounts_with_proxy or 0))
    proxies = max(0, int(proxies or 0))
    ops_total = max(0, int(ops_total or 0))

    has_accounts = accounts > 0
    has_proxy = proxies > 0 or accounts_with_proxy > 0
    has_op = ops_total > 0

    steps = [
        {
            "id": "accounts",
            "title": "Подключить аккаунты",
            "hint": "Импортируйте сессии или войдите по номеру/QR — без аккаунтов "
                    "ничего не работает.",
            "done": has_accounts,
            "optional": False,
            "cta": "Подключить",
            "action": {"kind": "import_accounts"},
        },
        {
            "id": "proxy",
            "title": "Задать прокси",
            "hint": "Рекомендуется: свой прокси = стабильный IP и меньше рисков. "
                    "Без прокси аккаунт идёт напрямую с IP сервера.",
            "done": has_proxy,
            "optional": True,
            "cta": "Добавить прокси",
            "action": {"kind": "proxy"},
        },
        {
            "id": "first_op",
            "title": "Запустить первое действие",
            "hint": "Инвайт, рассылка или разогрев — с первым действием система "
                    "начинает подсказывать следующие шаги под ваш сценарий.",
            "done": has_op,
            "optional": False,
            "cta": "Выбрать действие",
            "action": {"kind": "invite"},
        },
    ]

    core = [s for s in steps if not s["optional"]]
    activated = all(s["done"] for s in core)
    done_count = sum(1 for s in steps if s["done"])
    # Следующий незакрытый обязательный шаг — на нём фокус (подсветка/автоскролл).
    next_step = next((s["id"] for s in steps if not s["done"] and not s["optional"]), None)

    return {
        "activated": activated,
        "steps": steps,
        "done": done_count,
        "total": len(steps),
        "next_step": next_step,
    }
