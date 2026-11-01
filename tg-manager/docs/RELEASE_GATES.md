# Release Gates — Ворота релиза

## Перед деплоем

```bash
# 1. Syntax check
python3 -c "import ast; ast.parse(open('file.py').read())"

# 2. Protected DB calls
grep -n "await pool\." services/op_worker.py | grep -v "_safe_" | wc -l
# Должно быть: 0

# 3. Navigation buttons
grep -rn "edit_text.*Ошибка\|edit_text.*не найден" bot/handlers/ | grep -v "kb\|markup" | wc -l
# Должно быть: 0
```

## Deployment Flow
1. `git add . && git commit -m "описание"`
2. `git push origin claude/telegram-bot-services-xfAh6`
3. Railway auto-deploy (~2-3 мин)
4. Verify: `/version` в боте

## Post-Deploy
- `/start` — меню открывается
- Mini App загружается, SSE 🟢
- Нажать любую кнопку — нет ошибок

## Rollback
`git revert HEAD && git push`
