import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { AutomationProcessor } from './automation.processor';
import { AutomationService } from './automation.service';

@Module({
  imports: [BullModule.registerQueue({ name: 'automation' })],
  providers: [AutomationProcessor, AutomationService],
})
export class AutomationModule {}
