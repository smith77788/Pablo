import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';
import { prisma } from '@platform/db';
import { AiBriefingService } from './ai-briefing.service';

@Injectable()
export class SchedulerService implements OnModuleInit {
  private readonly logger = new Logger(SchedulerService.name);
  private briefingSentToday = false;
  private lastBriefingDate = '';

  constructor(
    @InjectQueue('broadcasts') private readonly broadcastQueue: Queue,
    private readonly aiBriefing: AiBriefingService,
  ) {}

  onModuleInit() {
    setInterval(() => this.tick(), 60_000);
    this.tick();
  }

  private async tick(): Promise<void> {
    await this.checkScheduled();
    await this.checkDailyBriefing();
  }

  private async checkScheduled(): Promise<void> {
    try {
      const due = await prisma.broadcast.findMany({
        where: {
          status: 'SCHEDULED',
          scheduledAt: { lte: new Date() },
        },
      });

      for (const bc of due) {
        await prisma.broadcast.update({
          where: { id: bc.id },
          data: { status: 'RUNNING', startedAt: new Date() },
        });
        await this.broadcastQueue.add('run', { broadcastId: bc.id, tenantId: bc.tenantId }, {
          attempts: 1,
          removeOnComplete: 50,
        });
        this.logger.log(`Firing scheduled broadcast ${bc.id}`);
      }
    } catch (err) {
      this.logger.error('Scheduler check failed', err);
    }
  }

  private async checkDailyBriefing(): Promise<void> {
    const now = new Date();
    const hour = now.getUTCHours();
    const today = now.toISOString().slice(0, 10);

    // Запускаем в 09:00 UTC один раз в сутки
    if (hour !== 9) return;
    if (this.lastBriefingDate === today) return;

    this.lastBriefingDate = today;
    this.logger.log('Running AI daily briefing...');
    try {
      await this.aiBriefing.runDailyBriefing();
    } catch (err) {
      this.logger.error('AI briefing failed', err);
    }
  }
}
