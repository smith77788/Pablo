import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { SchedulerService } from './scheduler.service';

@Module({
  imports: [BullModule.registerQueue({ name: 'broadcasts' })],
  providers: [SchedulerService],
})
export class SchedulerModule {}
