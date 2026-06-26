import { Module } from '@nestjs/common';
import { ClustersController } from './clusters.controller';
import { ClustersService } from './clusters.service';

@Module({
  controllers: [ClustersController],
  providers: [ClustersService],
  exports: [ClustersService],
})
export class ClustersModule {}
