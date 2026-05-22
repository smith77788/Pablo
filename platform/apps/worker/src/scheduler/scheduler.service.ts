import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { prisma } from '@platform/db';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';

// This is imported at module level to avoid circular dep with BullModule
let broadcastQueue: Queue;

@Injectable()
export class SchedulerService implements OnModuleInit {
  private readonly logger = new Logger(SchedulerService.name);

  onModuleInit() {
    setInterval(() => this.checkScheduled(), 60_000);
    this.checkScheduled();
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
        this.logger.log(`Firing scheduled broadcast ${bc.id}`);
      }
    } catch (err) {
      this.logger.error('Scheduler check failed', err);
    }
  }
}
