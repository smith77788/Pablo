-- schema_v341: голос персоны — полный набор черт и память о сказанном.
--
-- До этой миграции персона была именем, стилем и биографией: пол, город и
-- занятие модель выдумывала заново в каждой реплике (сегодня «я пошёл», завтра
-- «я пошла»; в одном чате из Казани, в другом из Сочи), эмодзи и грамотность
-- держались только словами промпта, а память персоны (persona_memory) не
-- писалась НИКЕМ — счётчик «🧠» на экране был нулём у всех всегда.
--
-- Здесь:
--   • черты, которые обязаны совпадать во всех чатах (пол, город, занятие);
--   • манера, которую движок ДОВОДИТ сам после модели (эмодзи, грамотность);
--   • границы (темы, которых персона не касается) и образцы её речи;
--   • сжатая память о себе (self_facts) — то, что персона уже рассказала о
--     себе людям, чтобы следующая реплика этому не противоречила.
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS gender VARCHAR(8) NOT NULL DEFAULT '';
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS city VARCHAR(64);
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS occupation VARCHAR(96);
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS emoji_level VARCHAR(8) NOT NULL DEFAULT 'rare';
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS literacy VARCHAR(8) NOT NULL DEFAULT 'casual';
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS taboo_topics TEXT;
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS sample_messages TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[];
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS memory_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS self_facts TEXT;
ALTER TABLE persona_profiles ADD COLUMN IF NOT EXISTS self_facts_at TIMESTAMPTZ;
