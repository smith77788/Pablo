-- schema_v149: Auto-Registrar "Генератор параметров" — per-owner saved device
-- emulation preference (manufacturer / app version). NULL/absent = fully
-- random (the previous, only, behavior) — this just lets an owner pin the
-- pool to a specific make/build instead of always randomizing, mirroring
-- what competitor account-farming panels expose as a dedicated screen.

CREATE TABLE IF NOT EXISTS autoreg_device_profiles (
    owner_id     BIGINT PRIMARY KEY,
    manufacturer TEXT,
    app_version  TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
