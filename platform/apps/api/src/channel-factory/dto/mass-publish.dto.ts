import { IsString, IsOptional, IsEnum, IsNumber, IsBoolean } from 'class-validator';

export enum MassPublishScope {
  ALL = 'all',
  BY_ACCOUNT = 'by_account',
  BY_CLUSTER = 'by_cluster',
}

export class MassPublishDto {
  @IsEnum(MassPublishScope)
  scope: MassPublishScope;

  @IsOptional()
  @IsString()
  accountId?: string;

  @IsOptional()
  @IsString()
  clusterId?: string;

  @IsString()
  text: string;

  @IsNumber()
  delaySeconds: number;  // 5 | 30 | 60

  @IsOptional()
  @IsBoolean()
  dryRun?: boolean;
}
