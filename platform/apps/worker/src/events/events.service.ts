import { Injectable } from '@nestjs/common';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';
import { EventRow } from '@platform/clickhouse';
import { randomUUID } from 'crypto';

@Injectable()
export class EventsService {
  constructor(@InjectQueue('events') private readonly queue: Queue) {}

  async track(event: Omit<EventRow, 'timestamp'> & { timestamp?: Date }): Promise<void> {
    await this.queue.add('ingest', { ...event, timestamp: (event.timestamp ?? new Date()).toISOString() }, {
      removeOnComplete: 200,
      removeOnFail: 100,
    });
  }

  async trackBatch(events: (Omit<EventRow, 'timestamp'> & { timestamp?: Date })[]): Promise<void> {
    await this.queue.addBulk(
      events.map((e) => ({
        name: 'ingest',
        data: { ...e, timestamp: (e.timestamp ?? new Date()).toISOString() },
        opts: { removeOnComplete: 200 },
      })),
    );
  }
}
