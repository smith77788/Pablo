import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';
import { prisma } from '@platform/db';

@Injectable()
export class SchedulerService implements OnModuleInit {
  private readonly logger = new Logger(SchedulerService.name);

  constructor(@InjectQueue('broadcasts') private readonly broadcastQueue: Queue) {}

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
}
