export class CreateApiKeyDto {
  name: string;
  expiresAt?: string; // ISO date или null
}
