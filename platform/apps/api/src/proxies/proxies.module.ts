import { Module } from '@nestjs/common';
import { ProxiesController } from './proxies.controller';
import { ProxiesService } from './proxies.service';

@Module({
  controllers: [ProxiesController],
  providers: [ProxiesService],
  exports: [ProxiesService],
})
export class ProxiesModule {}
