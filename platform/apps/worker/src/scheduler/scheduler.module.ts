import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { SchedulerService } from './scheduler.service';
import { AiBriefingService } from './ai-briefing.service';

@Module({
  imports: [BullModule.registerQueue({ name: 'broadcasts' })],
  providers: [SchedulerService, AiBriefingService],
})
export class SchedulerModule {}
