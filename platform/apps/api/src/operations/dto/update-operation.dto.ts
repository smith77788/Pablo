import { IsString, IsOptional, IsArray } from 'class-validator';

export class UpdateOperationDto {
  @IsString()
  @IsOptional()
  name?: string;

  @IsString()
  @IsOptional()
  description?: string;

  @IsString()
  @IsOptional()
  type?: string;

  @IsString()
  @IsOptional()
  projectId?: string;

  @IsOptional()
  targetScope?: Record<string, unknown>;

  @IsArray()
  @IsOptional()
  selectedAssets?: string[];

  @IsArray()
  @IsOptional()
  selectedClusters?: string[];

  @IsString()
  @IsOptional()
  scheduledAt?: string;

  @IsString()
  @IsOptional()
  status?: string;
}
