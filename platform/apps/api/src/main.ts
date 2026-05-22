import { NestFactory } from '@nestjs/core';
import { FastifyAdapter, NestFastifyApplication } from '@nestjs/platform-fastify';
import { ValidationPipe } from '@nestjs/common';
import { AppModule } from './app.module';

async function bootstrap() {
  const app = await NestFactory.create<NestFastifyApplication>(
    AppModule,
    new FastifyAdapter(),
  );

  app.useGlobalPipes(new ValidationPipe({ whitelist: true, transform: true }));
  app.enableCors({ origin: '*' });
  app.setGlobalPrefix('api/v1');

  const port = parseInt(process.env.API_PORT ?? '3002', 10);
  await app.listen(port, '0.0.0.0');
  console.log(`API running on port ${port}`);
}

bootstrap();
