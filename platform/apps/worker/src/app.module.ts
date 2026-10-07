import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { BroadcastModule } from './broadcast/broadcast.module';
import { EventsModule } from './events/events.module';
import { AutomationModule } from './automation/automation.module';
import { SchedulerModule } from './scheduler/scheduler.module';

@Module({
  imports: [
    BullModule.forRoot({ redis: process.env.REDIS_URL ?? 'redis://localhost:6379' }),
    BroadcastModule,
    EventsModule,
    AutomationModule,
    SchedulerModule,
  ],
})
export class AppModule {}
