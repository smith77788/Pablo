# Quality Standard — Стандарт качества Infragram

## Стабильность

### Критерии

- **99.9%** операций завершаются успешно (без крашей op_worker)
- **0** крашей op_worker за сутки
- **<1%** ошибок proxy
- **0**永久енных поломок сессий (AUTH_KEY dead)
- **0** зависших операций (status='running' > 24 часа)

### Мониторинг

- Circuit Breaker: автопауза после 3 подряд ошибок
- Session Health Monitor: проверка сессий каждые 6 часов
- Pool Monitor: проверка пула соединений каждые 5 минут
- Proxy Intelligence: автотест прокси перед использованием

---

## UX (Пользовательский опыт)

### Критерии

- **Время отклика** < 500ms для API, < 2с для Mini App
- **0 тупиков** в навигации (каждый экран имеет Cancel/Back)
- **100% экранов** с кнопками навигации
- **Live-обновления** через SSE каждые 15 секунд
- **ETA и скорость** для длинных операций

### Реализация

- `safe_answer()` для всех callback-хэндлеров
- Cancel/Back кнопки на всех error-path экранах
- SSE: op_progress event для running операций
- Progress Monitor с ETA и speed calculation

---

## Интеллект

### Критерии

- **Автоматический выбор** оптимального прокси
- **Предсказание банов** с точностью >80%
- **Auto-tuning** параметров на основе истории
- **Smart Presets** для strike операций
- **Adaptive Pacing** для anti-detection

### Реализация

- Proxy Intelligence: test_proxy(), auto_select_proxy()
- Predictive Analytics: predict_ban_risk(), predict_campaign_success()
- Adaptive Pacing: get_adaptive_delay() с learning
- Smart Presets: smart_detect_preset() по контенту
- Account Rotation: select_account_rotated() с load balancing

---

## Производительность

### Критерии

- **Mass operations**: >100 аккаунтов/час
- **Mini App загрузка**: <2сек
- **API response**: <200ms
- **DB queries**: <100ms (95-й перцентиль)
- **Memory usage**: <512MB на воркер

### Оптимизация

- Caching Layer: TTLCache для частых запросов
- Connection Pool: min=15, max=50 соединений
- DB Optimization: QueryTracker, index suggestions
- Parallel execution: до 8 параллельных операций

---

## Безопасность

### Критерии

- **Нет** IDOR уязвимостей
- **Нет** SQL-инъекций
- **Нет** plaintext секретов в коде
- **Нет** неавторизованных API вызовов
- **Есть** rate limiting на чувствительных endpoint-ах

### Реализация

- Owner scope: все запросы фильтруются по owner_id
- Safe DB helpers: все pool-вызовы защищены
- Token validation: проверка JWT на каждом запросе
- Input validation: валидация всех параметров

---

## Тестирование

### Покрытие

- **Syntax check**: все .py файлы проходят ast.parse()
- **Integration test**: критические пути проверяются
- **Load test**: mass operations на 100+ аккаунтов
- **Regression test**: проверка после каждого деплоя

### Автоматизация

- CI: pytest на каждый push
- Deploy: Railway auto-deploy
- Monitor: Pool Health + Circuit Breaker

---

## Метрики для отслеживания

| Метрика | Цель | Текущее |
|---------|------|---------|
| Operation success rate | >99.9% | ~99.5% |
| Op_worker crashes/day | 0 | 0-1 |
| Proxy error rate | <1% | ~0.5% |
| API response time | <200ms | ~150ms |
| Mini App load time | <2с | ~1.5с |
| Cache hit rate | >70% | ~60% |
| Ban prediction accuracy | >80% | ~75% |
