import { Injectable, Logger } from '@nestjs/common';
import { prisma } from '@platform/db';
import axios from 'axios';
import * as https from 'https';

const DELAY_MS = 50; // 20 msg/sec — well within Telegram limits
const BATCH_SIZE = 30;

const tgHttp = axios.create({
  httpsAgent: new https.Agent({ rejectUnauthorized: false }),
  timeout: 15_000,
});

@Injectable()
export class BroadcastSender {
  private readonly logger = new Logger(BroadcastSender.name);

  async execute(broadcastId: string, tenantId: string): Promise<void> {
    const bc = await prisma.broadcast.findUnique({
      where: { id: broadcastId },
      include: { bot: true },
    });
    if (!bc || bc.status !== 'RUNNING') return;

    // Build recipient list from segment or all bot users
    let telegramIds: bigint[] = [];
    if (bc.segmentId) {
      const rows = await prisma.segmentUser.findMany({
        where: { segmentId: bc.segmentId },
        include: { user: { select: { telegramId: true, isBlocked: true } } },
      });
      telegramIds = rows
        .filter((r) => !r.user.isBlocked)
        .map((r) => r.user.telegramId);
    } else {
      const rows = await prisma.telegramUser.findMany({
        where: { tenantId, isBlocked: false },
        select: { telegramId: true },
      });
      telegramIds = rows.map((r) => r.telegramId);
    }

    await prisma.broadcast.update({
      where: { id: broadcastId },
      data: { totalCount: telegramIds.length },
    });

    // Create recipient records in batches
    for (let i = 0; i < telegramIds.length; i += BATCH_SIZE) {
      const chunk = telegramIds.slice(i, i + BATCH_SIZE);
      await prisma.broadcastRecipient.createMany({
        data: chunk.map((tid) => ({ broadcastId, telegramId: tid })),
        skipDuplicates: true,
      });
    }

    // Send messages
    const msg = bc.message as any;
    const text = msg?.text ?? msg?.caption ?? '';
    let sent = 0, failed = 0;

    for (const telegramId of telegramIds) {
      try {
        await tgHttp.post(`https://api.telegram.org/bot${bc.bot.token}/sendMessage`, {
          chat_id: telegramId.toString(),
          text,
          parse_mode: 'HTML',
        });

        await prisma.broadcastRecipient.updateMany({
          where: { broadcastId, telegramId },
          data: { status: 'SENT', sentAt: new Date() },
        });
        sent++;
      } catch (err: any) {
        const errorCode = err?.response?.data?.error_code;
        const errorMsg = err?.response?.data?.description ?? String(err);

        if (errorCode === 403) {
          // User blocked the bot
          await prisma.telegramUser.updateMany({
            where: { tenantId, telegramId },
            data: { isBlocked: true },
          });
          await prisma.broadcastRecipient.updateMany({
            where: { broadcastId, telegramId },
            data: { status: 'BLOCKED', errorCode, errorMsg },
          });
        } else {
          await prisma.broadcastRecipient.updateMany({
            where: { broadcastId, telegramId },
            data: { status: 'FAILED', errorCode, errorMsg },
          });
        }
        failed++;
      }

      // Update progress every 50 messages
      if ((sent + failed) % 50 === 0) {
        await prisma.broadcast.update({
          where: { id: broadcastId },
          data: { sentCount: sent, failedCount: failed },
        });
      }

      await new Promise((r) => setTimeout(r, DELAY_MS));
    }

    await prisma.broadcast.update({
      where: { id: broadcastId },
      data: {
        status: 'COMPLETED',
        sentCount: sent,
        failedCount: failed,
        completedAt: new Date(),
      },
    });

    this.logger.log(`Broadcast ${broadcastId} done: ${sent} sent, ${failed} failed`);
  }
}
