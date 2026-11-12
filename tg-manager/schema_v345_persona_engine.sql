-- schema_v345: «мотор» персоны — какой моделью, насколько смело и как тщательно
-- она говорит, и когда спит.
--
-- До этой миграции голос персоны шёл общим каскадом моделей прогрева, и
-- владелец не мог ни выбрать модель для конкретной персоны, ни задать ей
-- смелость, ни попросить «лучше дороже, но живее». А персона, которая пишет в
-- три часа ночи по своему городу, выдаёт себя так же, как одинаковые фразы.
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS ai_provider VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS ai_model VARCHAR(96) NOT NULL DEFAULT '';
-- 0..100: 0 — сдержанно и предсказуемо, 100 — смело и неожиданно.
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS creativity SMALLINT NOT NULL DEFAULT 60;
-- fast — одна реплика; best — несколько вариантов и ИИ-судья выбирает лучший.
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS quality VARCHAR(8) NOT NULL DEFAULT 'fast';
-- Иногда отправлять мысль двумя короткими сообщениями, как живые люди.
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS split_messages BOOLEAN NOT NULL DEFAULT TRUE;
-- Часы сна по местному времени персоны (NULL — не спит) и её смещение от UTC
-- в часах (NULL — часовой пояс владельца).
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS sleep_from SMALLINT;
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS sleep_to SMALLINT;
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS utc_offset SMALLINT;
