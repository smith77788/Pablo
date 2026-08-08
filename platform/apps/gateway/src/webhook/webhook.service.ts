import { Injectable } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';
import { TelegramUpdate } from '@platform/types';

@Injectable()
export class WebhookService {
  constructor(@InjectQueue('updates') private readonly queue: Queue) {}

  async enqueue(botToken: string, update: TelegramUpdate): Promise<void> {
    await this.queue.add(
      'process',
      { botToken, update },
      {
        removeOnComplete: 100,
        removeOnFail: 500,
        attempts: 3,
        backoff: { type: 'exponential', delay: 2000 },
      },
    );
  }
}
