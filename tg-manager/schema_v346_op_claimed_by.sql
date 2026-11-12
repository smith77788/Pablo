-- schema_v346: хозяин запущенной операции — для горизонтального исполнителя.
--
-- Очередь операций разбирал ОДИН процесс (аренда исполнителя), и это был
-- потолок продукта по ёмкости. С OP_SHARDED_EXECUTOR=1 операции разбирают все
-- процессы-исполнители, и сторожам лидера нужно знать, чья операция: свои,
-- бесхозные и операции умерших процессов чинить можно, живые операции
-- соседей — нельзя. Колонку пишет отбор операций (op_worker._process_pending)
-- в любом режиме.
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS claimed_by TEXT;
-- Сторож «операции умерших процессов» ходит только по запущенным.
CREATE INDEX IF NOT EXISTS idx_operation_queue_running_claimed
    ON operation_queue (claimed_by) WHERE status = 'running';
