import { Processor, Process } from '@nestjs/bull';
import { Job } from 'bull';
import { Logger } from '@nestjs/common';
import { insertEvents } from '@platform/clickhouse';

@Processor('events')
export class EventsProcessor {
  private readonly logger = new Logger(EventsProcessor.name);
  private readonly buffer: any[] = [];
  private flushTimer: NodeJS.Timeout | null = null;

  @Process('ingest')
  async ingest(job: Job): Promise<void> {
    this.buffer.push({
      ...job.data,
      telegram_id: job.data.telegram_id?.toString() ?? '0',
      timestamp: new Date(job.data.timestamp),
    });

    if (this.buffer.length >= 100) {
      await this.flush();
    } else if (!this.flushTimer) {
      this.flushTimer = setTimeout(() => this.flush(), 5_000);
    }
  }

  private async flush(): Promise<void> {
    if (this.flushTimer) { clearTimeout(this.flushTimer); this.flushTimer = null; }
    if (!this.buffer.length) return;
    const batch = this.buffer.splice(0, this.buffer.length);
    try {
      await insertEvents(batch);
      this.logger.debug(`Flushed ${batch.length} events to ClickHouse`);
    } catch (err) {
      this.logger.warn('ClickHouse flush failed (non-fatal)', err);
    }
  }
}
