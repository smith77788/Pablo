import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { EventsProcessor } from './events.processor';
import { EventsService } from './events.service';

@Module({
  imports: [BullModule.registerQueue({ name: 'events' })],
  providers: [EventsProcessor, EventsService],
  exports: [EventsService],
})
export class EventsModule {}
