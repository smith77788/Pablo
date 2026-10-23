-- v145: Recurring scheduled broadcasts.
-- repeat_interval_min: 0 = one-shot (текущее поведение), >0 = период повтора в
-- минутах (1440 = ежедневно, 10080 = еженедельно). После срабатывания планировщик
-- создаёт следующее вхождение через этот интервал.
ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS repeat_interval_min INT DEFAULT 0;
