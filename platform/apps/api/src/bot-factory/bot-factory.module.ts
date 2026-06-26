import { Module } from '@nestjs/common';
import { BotFactoryController } from './bot-factory.controller';
import { BotFactoryService } from './bot-factory.service';

@Module({
  controllers: [BotFactoryController],
  providers: [BotFactoryService],
})
export class BotFactoryModule {}
