-- v143: шифрование сессий at-rest — детерминированный fingerprint для дедупа.
--
-- session_str теперь хранится зашифрованным (AES-256-GCM, token_vault, префикс
-- "ENC:"). Шифр недетерминирован (случайный nonce), поэтому дедуп по равенству
-- session_str невозможен — для него добавлен session_fp = sha256(plaintext).
-- Legacy-строки (plaintext session_str, session_fp NULL) продолжают читаться
-- через decrypt_token passthrough; шифруются при следующей записи.

ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS session_fp TEXT;
CREATE INDEX IF NOT EXISTS idx_tg_accounts_session_fp ON tg_accounts(session_fp);
