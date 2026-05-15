FROM node:20-alpine AS base

# Install OS-level deps needed for sharp (WebP conversion)
RUN apk add --no-cache \
    vips-dev \
    fftw-dev \
    gcc \
    g++ \
    make \
    libc6-compat

WORKDIR /app

# ── Dependencies layer (cached unless package.json changes) ──────────────────
FROM base AS deps
COPY package*.json ./
RUN npm ci --only=production --ignore-scripts=false && npm cache clean --force

# ── Production image ─────────────────────────────────────────────────────────
FROM node:20-alpine AS production

# Runtime libs for sharp
RUN apk add --no-cache vips

WORKDIR /app

# Create required directories
RUN mkdir -p uploads/thumbs uploads/models data backups logs && \
    chmod 755 uploads uploads/thumbs uploads/models data backups logs

# Non-root user for security
RUN addgroup -g 1001 -S nodejs && adduser -S nodejs -u 1001

# Copy node_modules from deps layer
COPY --from=deps --chown=nodejs:nodejs /app/node_modules ./node_modules

# Copy application source (excludes .dockerignore patterns)
COPY --chown=nodejs:nodejs . .

USER nodejs

EXPOSE 3000

# Health check via /api/health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD node -e "require('http').get('http://localhost:3000/api/health',r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>{try{const j=JSON.parse(d);process.exit(j.status==='healthy'||j.status==='degraded'?0:1)}catch{process.exit(r.statusCode===200?0:1)}})}).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
