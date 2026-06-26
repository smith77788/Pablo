import { NestFactory } from '@nestjs/core';
import {
  FastifyAdapter,
  NestFastifyApplication,
} from '@nestjs/platform-fastify';
import { AppModule } from './app.module';

async function bootstrap() {
  const app = await NestFactory.create<NestFastifyApplication>(
    AppModule,
    new FastifyAdapter({ logger: true }),
  );

  const port = parseInt(process.env.GATEWAY_PORT ?? '3001', 10);
  await app.listen(port, '0.0.0.0');
  console.log(`Gateway running on port ${port}`);
}

bootstrap();
