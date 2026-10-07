import {
  Controller, Get, Post, Patch, Delete,
  Param, Body, Query, UseGuards, Req,
} from '@nestjs/common';
import { AssetsService } from './assets.service';
import { JwtAuthGuard } from '../auth/jwt.guard';
import { CreateAssetDto } from './dto/create-asset.dto';
import { UpdateAssetDto } from './dto/update-asset.dto';

class BulkTagDto { assetIds: string[]; tags: string[]; }
class BulkClusterDto { assetIds: string[]; clusterId: string; }

@Controller('assets')
@UseGuards(JwtAuthGuard)
export class AssetsController {
  constructor(private readonly assets: AssetsService) {}

  @Get()
  list(
    @Req() req: any,
    @Query('type') type?: string,
    @Query('status') status?: string,
    @Query('projectId') projectId?: string,
    @Query('clusterId') clusterId?: string,
    @Query('search') search?: string,
    @Query('page') page?: string,
    @Query('limit') limit?: string,
  ) {
    return this.assets.findAll(req.user.tenantId, {
      type, status, projectId, clusterId, search,
      page: page ? Number(page) : undefined,
      limit: limit ? Number(limit) : undefined,
    });
  }

  @Post()
  create(@Req() req: any, @Body() dto: CreateAssetDto) {
    return this.assets.create(req.user.tenantId, dto);
  }

  @Get(':id')
  get(@Req() req: any, @Param('id') id: string) {
    return this.assets.findOne(req.user.tenantId, id);
  }

  @Patch(':id')
  update(@Req() req: any, @Param('id') id: string, @Body() dto: UpdateAssetDto) {
    return this.assets.update(req.user.tenantId, id, dto);
  }

  @Delete(':id')
  archive(@Req() req: any, @Param('id') id: string) {
    return this.assets.archive(req.user.tenantId, id);
  }

  @Post('bulk-tag')
  bulkTag(@Req() req: any, @Body() dto: BulkTagDto) {
    return this.assets.bulkTag(req.user.tenantId, dto.assetIds, dto.tags);
  }

  @Post('bulk-cluster')
  bulkCluster(@Req() req: any, @Body() dto: BulkClusterDto) {
    return this.assets.bulkCluster(req.user.tenantId, dto.assetIds, dto.clusterId);
  }
}
