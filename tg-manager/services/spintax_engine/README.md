# services/spintax_engine

Встроенная (vendored) копия движка **SpintaxAI** — компилятора DSL для
рандомизации текста (Lexer → Parser → AST → Semantic → Validator → Generator →
Formatter → Reporter → публичный API).

- Нулевые внешние зависимости, полностью типизирован, потокобезопасен,
  детерминирован при заданном `seed`.
- Внутри пакета только относительные импорты, поэтому он работает под путём
  `services.spintax_engine` без изменений.

Используется модулем `services/spintax_service.py` и хендлером
`bot/handlers/spintax.py` для валидации и раскрытия spintax-шаблонов.

Источник: https://github.com/smith77788/SpintaxAI (пакет `src/spintaxai`).
Обновление — заменой `*.py` из апстрима; локально файлы не правим.

## Кратко об API

```python
from services.spintax_engine import generate, generate_many, validate, Context

generate("{Привет|Здравствуйте} мир!")            # один вариант
generate_many("{A|B|C}", 10, unique=True)          # батч без повторов
validate("{oops").ok                               # False — диагностика, без исключений
```
