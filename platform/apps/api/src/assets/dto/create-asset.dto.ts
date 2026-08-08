import { IsString, IsOptional, IsArray } from 'class-validator';

export class CreateAssetDto {
  @IsString()
  type: string;

  @IsString()
  name: string;

  @IsString()
  @IsOptional()
  username?: string;

  @IsString()
  @IsOptional()
  externalId?: string;

  @IsString()
  @IsOptional()
  projectId?: string;

  @IsString()
  @IsOptional()
  clusterId?: string;

  @IsArray()
  @IsOptional()
  tags?: string[];

  @IsOptional()
  metadata?: Record<string, unknown>;

  @IsString()
  @IsOptional()
  notes?: string;
}
