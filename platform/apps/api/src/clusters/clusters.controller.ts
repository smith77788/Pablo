import {
  Controller, Get, Post, Patch, Delete,
  Param, Body, UseGuards, Req,
} from '@nestjs/common';
import { ClustersService } from './clusters.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateClusterDto } from './dto/create-cluster.dto';
import { UpdateClusterDto } from './dto/update-cluster.dto';

class AddAssetDto { assetId: string; }

@Controller('clusters')
@UseGuards(JwtAuthGuard)
export class ClustersController {
  constructor(private readonly clusters: ClustersService) {}

  @Get()
  list(@Req() req: any) {
    return this.clusters.findAll(req.user.tenantId);
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateClusterDto) {
    return this.clusters.create(req.user.tenantId, dto);
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.clusters.findOne(req.user.tenantId, id);
  }

  @Patch(':id')
  update(@Req() req: any, @Param('id') id: string, @Body() dto: UpdateClusterDto) {
    return this.clusters.update(req.user.tenantId, id, dto);
  }

  @Delete(':id')
  delete(@Req() req: any, @Param('id') id: string) {
    return this.clusters.delete(req.user.tenantId, id);
  }

  @Get(':id/assets')
  getAssets(@Req() req: any, @Param('id') id: string) {
    return this.clusters.getAssets(req.user.tenantId, id);
  }

  @Post(':id/assets')
  addAsset(@Req() req: any, @Param('id') id: string, @Body() dto: AddAssetDto) {
    return this.clusters.addAsset(req.user.tenantId, id, dto.assetId);
  }

  @Delete(':id/assets/:assetId')
  removeAsset(@Req() req: any, @Param('id') id: string, @Param('assetId') assetId: string) {
    return this.clusters.removeAsset(req.user.tenantId, id, assetId);
  }
}
