-- v142: Auto-reply rules — depth to parity.
-- reply_delay_sec  : human-like pause before replying (anti-detect / natural feel)
-- active_from_hour / active_to_hour : working-hours window (UTC hour 0-23; NULL = always).
--                    Supports overnight windows (from > to, e.g. 22→6).
-- priority         : higher fires first (first-match-wins loop → priority decides the winner)
ALTER TABLE auto_replies ADD COLUMN IF NOT EXISTS reply_delay_sec INT DEFAULT 0;
ALTER TABLE auto_replies ADD COLUMN IF NOT EXISTS active_from_hour INT;
ALTER TABLE auto_replies ADD COLUMN IF NOT EXISTS active_to_hour INT;
ALTER TABLE auto_replies ADD COLUMN IF NOT EXISTS priority INT DEFAULT 0;
