-- Pablo AI — pg_cron scheduled jobs
-- Run in Supabase SQL Editor (requires pg_cron extension, already enabled)

-- ─── Morning Brief: щодня о 09:00 Київ (06:00 UTC літо) ─────────────────
SELECT cron.schedule(
  'pablo-morning-brief',
  '0 6 * * *',
  $$
    SELECT net.http_post(
      url := current_setting('app.supabase_url') || '/functions/v1/pablo-morning-brief',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'Authorization', 'Bearer ' || current_setting('app.supabase_service_role_key')
      ),
      body := '{"source": "cron"}'::jsonb
    );
  $$
);

-- ─── Expire stale approvals: кожні 6 годин ───────────────────────────────
SELECT cron.schedule(
  'pablo-expire-approvals',
  '0 */6 * * *',
  $$ SELECT public.pablo_expire_approvals(); $$
);

-- ─── Executive health check: кожного понеділка о 09:00 Київ ─────────────
-- Weekly report: CEO synthesis of full week
SELECT cron.schedule(
  'pablo-weekly-report',
  '0 6 * * 1',
  $$
    SELECT net.http_post(
      url := current_setting('app.supabase_url') || '/functions/v1/pablo-executive-brain',
      headers := jsonb_build_object(
        'Content-Type', 'application/json',
        'Authorization', 'Bearer ' || current_setting('app.supabase_service_role_key')
      ),
      body := jsonb_build_object(
        'agent', 'ceo',
        'task', 'Підготуй тижневий стратегічний огляд бізнесу. Проаналізуй KPI за тиждень, визнач головні досягнення та проблеми, постав 3 цілі на наступний тиждень.',
        'include_business_context', true
      )
    );
  $$
);

-- Перевір задачі:
-- SELECT * FROM cron.job WHERE jobname LIKE 'pablo%';
