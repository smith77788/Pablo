# Supabase Edge Functions

## Deploy all functions

```bash
supabase functions deploy telegram-webhook
supabase functions deploy send-sms
supabase functions deploy send-email
supabase functions deploy payment-webhook
supabase functions deploy broadcast
```

## Required environment variables (set in Supabase Dashboard → Edge Functions → Secrets)

| Variable                                                          | Required for                | Description                  |
| ----------------------------------------------------------------- | --------------------------- | ---------------------------- |
| `TELEGRAM_BOT_TOKEN`                                              | telegram-webhook, broadcast | From @BotFather              |
| `TELEGRAM_WEBHOOK_SECRET`                                         | telegram-webhook            | Optional security secret     |
| `SMS_PROVIDER`                                                    | send-sms                    | `smsru`, `smsc`, or `twilio` |
| `SMS_RU_API_KEY`                                                  | send-sms                    | For SMS.ru                   |
| `SMSC_LOGIN` / `SMSC_PASSWORD`                                    | send-sms                    | For SMSC.ru                  |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | send-sms                    | For Twilio                   |
| `SENDGRID_API_KEY`                                                | send-email                  | For SendGrid                 |
| `FROM_EMAIL` / `FROM_NAME`                                        | send-email                  | Sender info                  |
| `YOOKASSA_SECRET_KEY`                                             | payment-webhook             | YooKassa integration         |
| `STRIPE_WEBHOOK_SECRET`                                           | payment-webhook             | Stripe webhook               |

## Set Telegram Webhook

After deploying, set the webhook:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<PROJECT_REF>.supabase.co/functions/v1/telegram-webhook&secret_token=<WEBHOOK_SECRET>"
```
