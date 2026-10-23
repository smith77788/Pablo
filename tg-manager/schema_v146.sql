-- v146: шифрование proxy_url at-rest в user_proxies — детерминированный fingerprint.
--
-- proxy_url теперь хранится зашифрованным (AES-256-GCM, token_vault, "ENC:").
-- Шифр недетерминирован → дедуп по равенству proxy_url и ON CONFLICT(owner_id,
-- proxy_url) ломаются. Для них добавлен proxy_fp = sha256(plaintext) + UNIQUE.
-- Legacy-строки (plaintext proxy_url, proxy_fp NULL) читаются через decrypt_token
-- passthrough; шифруются при следующей записи.
--
-- ВАЖНО: шифруется ТОЛЬКО user_proxies (приватные прокси с кредами user:pass).
-- platform_proxy_pool (публичные спарсенные) и infra_memory_proxies (ключи по
-- plaintext-значению, использованному при подключении) остаются plaintext.

ALTER TABLE user_proxies ADD COLUMN IF NOT EXISTS proxy_fp TEXT;
-- частичный UNIQUE только по не-NULL fp: legacy-строки (fp NULL) не конфликтуют,
-- новые/перешифрованные дедуплицируются по (owner_id, proxy_fp).
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_proxies_owner_fp
    ON user_proxies(owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL;
