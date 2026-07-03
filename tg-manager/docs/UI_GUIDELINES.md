# UI Guidelines — Руководство по интерфейсу

## Принципы

1. **Telegram-native** — всё работает в Telegram
2. **Минимум кликов** — каждое действие за ≤3 клика
3. **Без тупиков** — каждый экран имеет Back/Cancel
4. **Прогресс** — длинные операции показывают прогресс
5. **Отзывчивость** — <500ms отклик

---

## Кнопки

### Cancel/Back

Каждый экран с вводом данных или FSM должен иметь:

```python
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import BmCb

kb = InlineKeyboardBuilder()
kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
```

### Confirm/Deny

Для критических действий:

```python
kb = InlineKeyboardBuilder()
kb.button(text="✅ Да", callback_data=ConfirmCb(action="yes", id=item_id))
kb.button(text="❌ Нет", callback_data=ConfirmCb(action="no", id=item_id))
kb.adjust(2)
```

---

## Сообщения

### Ошибки

```python
await callback.message.edit_text(
    "❌ Операция не выполнена\n\nПричина: ...",
    reply_markup=kb.as_markup()  # ВСЕГДА с кнопкой
)
```

### Успех

```python
await callback.message.edit_text(
    "✅ Операция выполнена успешно",
    reply_markup=kb.as_markup()  # Кнопка для следующего действия
)
```

### Прогресс

```python
bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
await callback.message.edit_text(
    f"⏳ Выполняется... {pct}%\n[{bar}] {done}/{total}"
)
```

---

## Форматирование

### HTML

```python
text = (
    "📱 <b>Аккаунт</b>\n\n"
    f"📞 Телефон: <code>{phone}</code>\n"
    f"⭐ Trust: {trust_score:.2f}\n"
    f"{'🟢 Активен' if is_active else '🔴 Неактивен'}"
)
```

### Эмодзи

- ✅ Успех / Активен / Включено
- ❌ Ошибка / Неактивен / Выключено
- ⏳ В процессе / Ожидание
- ⚠️ Предупреждение
- 🔒 Заблокировано / Требует подписку

---

## Inline Keyboard

### Паттерн: Главное меню

```python
kb = InlineKeyboardBuilder()
kb.button(text="📱 Аккаунты", callback_data=BmCb(action="accounts"))
kb.button(text="📡 Каналы", callback_data=BmCb(action="channels"))
kb.button(text="🤖 Боты", callback_data=BmCb(action="bots"))
kb.button(text="⚙️ Операции", callback_data=BmCb(action="operations"))
kb.adjust(2)
```

### Паттерн: Детали объекта

```python
kb = InlineKeyboardBuilder()
kb.button(text="🔄 Обновить", callback_data=DetailCb(action="refresh", id=obj_id))
kb.button(text="✏️ Редактировать", callback_data=DetailCb(action="edit", id=obj_id))
kb.button(text="🗑 Удалить", callback_data=DetailCb(action="delete", id=obj_id))
kb.button(text="◀️ Назад", callback_data=BmCb(action="list"))
kb.adjust(2)
```

---

## Mini App

### Компоненты

- KPI карточки: иконка + значение + тренд
- Списки: аватар + название + статус
- Кнопки: primary (синий), secondary (серый), danger (красный)
- Прогресс-бары: █░ с процентом

### Цвета

- Primary: #0088cc (Telegram blue)
- Success: #00c853
- Warning: #ffc107
- Danger: #ff5252
- Background: #f5f5f5
