import { Processor, Process } from '@nestjs/bull';
import { Job } from 'bull';
import { Logger } from '@nestjs/common';
import { BroadcastSender } from './broadcast.sender';

@Processor('broadcasts')
export class BroadcastProcessor {
  private readonly logger = new Logger(BroadcastProcessor.name);

  constructor(private readonly sender: BroadcastSender) {}

  @Process('run')
  async run(job: Job<{ broadcastId: string; tenantId: string }>): Promise<void> {
    const { broadcastId, tenantId } = job.data;
    this.logger.log(`Starting broadcast ${broadcastId}`);
    try {
      await this.sender.execute(broadcastId, tenantId);
    } catch (err) {
      this.logger.error(`Broadcast ${broadcastId} failed`, err);
      throw err;
    }
  }
}
