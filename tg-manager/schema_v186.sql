-- Хартбиты процессов-реплик — чтобы обнаружить запуск НЕСКОЛЬКИХ реплик.
-- Зачем: лимиты флуда, потолки параллельности и троттлы восстановления живут в
-- памяти ОДНОГО процесса (реестр KNOWN_DIVERGENCE). На двух репликах они
-- разъезжаются, и суммарный темп по флоту превышает безопасный — прямой риск
-- бана. Пока лимиты не вынесены в общий слой, «одна реплика» — это ограничение,
-- а не случайность. Guard читает эту таблицу и громко предупреждает, если реплик
-- больше одной (см. services/replica_guard.py).
CREATE TABLE IF NOT EXISTS process_heartbeats (
    worker_id  TEXT PRIMARY KEY,   -- hostname:pid:uuid (_WORKER_ID)
    role       TEXT NOT NULL DEFAULT 'all',   -- web|worker|all
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_process_heartbeats_seen
    ON process_heartbeats(last_seen);
