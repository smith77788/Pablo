# UI Guidelines — Интерфейс

## Принципы
1. Telegram-native
2. ≤3 клика до действия
3. Каждый экран = Cancel/Back
4. Прогресс для длинных операций
5. <500ms отклик

## Кнопки
```python
# Cancel/Back
kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))

# Confirm/Deny
kb.button(text="✅ Да", callback_data=ConfirmCb(action="yes"))
kb.button(text="❌ Нет", callback_data=ConfirmCb(action="no"))
```

## Сообщения
```python
# Ошибка — ВСЕГДА с кнопкой
await msg.edit_text("❌ Ошибка", reply_markup=kb.as_markup())

# Прогресс
bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
await msg.edit_text(f"⏳ {pct}%\n[{bar}] {done}/{total}")
```

## Форматирование
```python
text = "📱 <b>Аккаунт</b>\n📞 <code>{phone}</code>\n⭐ {trust:.2f}"
```

## Эмодзи
✅ успех · ❌ ошибка · ⏳ процесс · ⚠️ предупреждение · 🔒 блокировка
