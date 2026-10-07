import { Processor, Process } from '@nestjs/bull';
import { Job } from 'bull';
import { Logger } from '@nestjs/common';
import { RelayService } from './relay.service';
import { TelegramUpdate } from '@platform/types';

interface UpdateJob {
  botToken: string;
  update: TelegramUpdate;
}

@Processor('updates')
export class RelayProcessor {
  private readonly logger = new Logger(RelayProcessor.name);

  constructor(private readonly relayService: RelayService) {}

  @Process('process')
  async handle(job: Job<UpdateJob>): Promise<void> {
    const { botToken, update } = job.data;
    try {
      await this.relayService.processUpdate(botToken, update);
    } catch (err) {
      this.logger.error(`Failed to process update ${update.update_id}`, err);
      throw err;
    }
  }
}
