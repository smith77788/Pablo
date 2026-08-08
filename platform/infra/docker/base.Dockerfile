FROM node:20-alpine AS base
RUN npm install -g pnpm@9 turbo
WORKDIR /app
