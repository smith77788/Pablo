/**
 * Точка входа: сборка зависимостей и запуск HTTP-сервера.
 *
 * Реальная проводка Prisma → repo/accounts → engine → controller. Секреты берём
 * из окружения; StringSession расшифровывается через опциональный хук.
 */
import express from "express";
import { PrismaClient } from "@prisma/client";
import { NodeClientFactory } from "./NodeClientFactory.js";
import { TelegramNodeEngine } from "./TelegramNodeEngine.js";
import { PrismaNodeRepository } from "./NodeRepository.js";
import { PrismaAccountProvider } from "./AccountProvider.js";
import { createNodeRouter } from "./NodeDashboardController.js";

function decryptSession(enc: string): string {
  // Подключите ваш KMS/секрет. По умолчанию — passthrough (сессия уже plaintext).
  // Пример совместимости с python token_vault: снять префикс "ENC:" и расшифровать.
  return enc.startsWith("ENC:") ? enc.slice(4) : enc;
}

async function main(): Promise<void> {
  const prisma = new PrismaClient();
  await prisma.$connect();

  const factory = new NodeClientFactory({
    minDelayMs: Number(process.env.NODE_MIN_DELAY_MS ?? 3000),
    maxDelayMs: Number(process.env.NODE_MAX_DELAY_MS ?? 5000),
    connectionRetries: 5,
  });
  const engine = new TelegramNodeEngine(factory);
  const repo = new PrismaNodeRepository(prisma);
  const accounts = new PrismaAccountProvider(prisma, {
    decrypt: decryptSession,
    fallbackApiId: process.env.TG_API_ID ? Number(process.env.TG_API_ID) : undefined,
    fallbackApiHash: process.env.TG_API_HASH,
  });

  const app = express();
  app.use(express.json({ limit: "1mb" }));
  app.get("/health", (_req, res) => res.json({ ok: true }));
  app.use(createNodeRouter({ repo, accounts, engine }));

  const port = Number(process.env.PORT ?? 8090);
  const server = app.listen(port, () => {
    // eslint-disable-next-line no-console
    console.log(`[nodes-service] listening on :${port}`);
  });

  const shutdown = async () => {
    server.close();
    await prisma.$disconnect();
    process.exit(0);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

main().catch((e) => {
  // eslint-disable-next-line no-console
  console.error("[nodes-service] fatal:", e);
  process.exit(1);
});
