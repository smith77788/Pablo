import { IsString, IsOptional, IsArray, IsNotEmpty } from 'class-validator';

export class CreateOperationDto {
  @IsString()
  @IsNotEmpty()
  name: string;

  @IsString()
  @IsOptional()
  description?: string;

  @IsString()
  @IsNotEmpty()
  type: string;

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
}
