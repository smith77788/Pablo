-- AlterTable: add telegramChatId to operators for AI briefing delivery
ALTER TABLE "operators" ADD COLUMN IF NOT EXISTS "telegramChatId" TEXT;
