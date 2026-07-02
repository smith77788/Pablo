-- Schema v6: photo support in broadcasts
ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS photo_file_id TEXT;
