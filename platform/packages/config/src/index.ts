import { z } from 'zod';

const envSchema = z.object({
  DATABASE_URL: z.string(),
  CLICKHOUSE_URL: z.string().default('http://localhost:8123'),
  CLICKHOUSE_USER: z.string().default('default'),
  CLICKHOUSE_PASSWORD: z.string().default(''),
  CLICKHOUSE_DATABASE: z.string().default('tgplatform'),
  REDIS_URL: z.string().default('redis://localhost:6379'),
  JWT_SECRET: z.string().min(32),
  JWT_EXPIRES_IN: z.string().default('15m'),
  JWT_REFRESH_EXPIRES_IN: z.string().default('7d'),
  TELEGRAM_WEBHOOK_SECRET: z.string(),
  S3_ENDPOINT: z.string().optional(),
  S3_ACCESS_KEY: z.string().optional(),
  S3_SECRET_KEY: z.string().optional(),
  S3_BUCKET: z.string().default('tgplatform'),
  ANTHROPIC_API_KEY: z.string().optional(),
  NODE_ENV: z.enum(['development', 'production', 'test']).default('development'),
});

export type Config = z.infer<typeof envSchema>;

export function validateEnv(): Config {
  const result = envSchema.safeParse(process.env);
  if (!result.success) {
    console.error('Invalid environment variables:', result.error.format());
    process.exit(1);
  }
  return result.data;
}
