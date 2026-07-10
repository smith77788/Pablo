# Audience Intelligence

## Назначение

Продвинутая аналитика аудитории, сегментация и прогнозирование вовлечённости. Предоставляет комплексный анализ подписчиков каналов, выявляет паттерны активности и генерирует персонализированные рекомендации.

## Основные функции

- **Обзор аудитории** — общая статистика по подписчикам, активности и retention
- **Сегментация** — разделение аудитории по поведенческим характеристикам (Champions, Loyal, At Risk, Dormant, New)
- **Прогнозирование вовлечённости** — предсказание ER на основе исторических данных
- **Инсайты аудитории** — тренды роста, риск оттока, качество аудитории
- **Рекомендации** — персонализированные советы по оптимизации контента и Engagement

## API эндпоинты

### Обзор аудитории

```python
async def analyze_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
) -> AudienceOverview | None:
    """
    Комплексный анализ аудитории канала.
    
    Возвращает AudienceOverview:
        - total_subscribers: общее количество подписчиков
        - active_users: активные пользователи (за 7 дней)
        - active_rate: процент активных
        - avg_session_duration_min: средняя продолжительность сессии
        - peak_hour: пиковый час активности
        - peak_day: пиковый день недели
        - retention_7d: retention за 7 дней
        - retention_30d: retention за 30 дней
    """
```

### Сегментация аудитории

```python
async def segment_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
) -> list[AudienceSegment]:
    """
    Сегментация аудитории по поведенческим характеристикам.
    
    Возвращает список сегментов:
        - Champions: высокоактивные, высокий ER
        - Loyal: стабильная активность, средний ER
        - At Risk: снижение активности
        - Dormant: неактивные 14+ дней
        - New: присоединились за последние 7 дней
    """
```

### Прогнозирование вовлечённости

```python
async def predict_engagement(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
) -> EngagementPrediction | None:
    """
    Прогнозирование вовлечённости на основе исторических данных.
    
    Возвращает EngagementPrediction:
        - predicted_er: прогнозируемый Engagement Rate
        - confidence: уверенность прогноза (0-1)
        - best_post_hour: лучший час для публикации
        - best_post_day: лучший день для публикации
        - recommended_content_types: рекомендуемые форматы контента
        - factors: факторы влияния на прогноз
    """
```

### Инсайты аудитории

```python
async def get_audience_insights(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
) -> AudienceInsights | None:
    """
    Комплексные инсайты аудитории с рекомендациями.
    
    Возвращает AudienceInsights:
        - growth_trend: тренд роста (growing, declining, stable)
        - growth_rate_pct: процент роста
        - churn_risk_pct: риск оттока
        - top_activity_hours: топ-3 часа активности
        - content_preferences: предпочтения по форматам контента
        - audience_quality_score: качество аудитории (0-100)
        - recommendations: список рекомендаций
    """
```

### Форматирование для Telegram

```python
def format_overview(overview: AudienceOverview) -> str:
    """Форматировать обзор аудитории для Telegram."""

def format_segments(segments: list[AudienceSegment]) -> str:
    """Форматировать сегменты аудитории для Telegram."""

def format_prediction(prediction: EngagementPrediction) -> str:
    """Форматировать прогноз вовлечённости для Telegram."""

def format_insights(insights: AudienceInsights) -> str:
    """Форматировать инсайты аудитории для Telegram."""
```

## Примеры использования

```python
# Получение обзора аудитории
overview = await analyze_audience(pool, owner_id=123, channel_id=-100123456)
if overview:
    print(f"Подписчики: {overview.total_subscribers}")
    print(f"Активные (7д): {overview.active_users}")
    print(f"Retention 30д: {overview.retention_30d}%")

# Сегментация аудитории
segments = await segment_audience(pool, owner_id=123, channel_id=-100123456)
for seg in segments:
    print(f"{seg.name}: {seg.user_count} ({seg.percentage}%)")

# Прогнозирование вовлечённости
prediction = await predict_engagement(pool, owner_id=123, channel_id=-100123456)
if prediction:
    print(f"Прогноз ER: {prediction.predicted_er}%")
    print(f"Лучшее время: {prediction.best_post_hour}:00 {prediction.best_post_day}")

# Получение инсайтов
insights = await get_audience_insights(pool, owner_id=123, channel_id=-100123456)
if insights:
    print(f"Тренд: {insights.growth_trend}")
    print(f"Качество аудитории: {insights.audience_quality_score}/100")
    for rec in insights.recommendations:
        print(rec)

# Форматирование для вывода в Telegram
text = format_overview(overview)
await message.answer(text, parse_mode="HTML")
```

## Метрики и расчёты

- **Retention Rate** — процент пользователей, вернувшихся через N дней
- **Engagement Rate** — (лайки + комментарии + репосты) / просмотры
- **Quality Score** — комплексная оценка (0-100) на основе активности, retention и ER
- **Churn Risk** — процент неактивных пользователей (14+ дней)
- **Growth Trend** — сравнение прироста текущей и предыдущей недели

## Интеграция

- **Content Performance** — данные об эффективности контента для прогнозов
- **User Activity** — данные об активности пользователей для сегментации
- **Bot Users** — данные о подписчиках каналов