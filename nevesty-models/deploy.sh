#!/bin/bash
set -e

echo "🚀 Deploying Nevesty Models..."

# Pull latest changes
git pull origin main

# Build and restart
docker-compose pull 2>/dev/null || true
docker-compose build --no-cache
docker-compose up -d

# Wait for health check
echo "⏳ Waiting for health check..."
sleep 5
for i in {1..10}; do
  if curl -sf http://localhost:3000/health > /dev/null; then
    echo "✅ Application is healthy!"
    break
  fi
  echo "  Attempt $i/10..."
  sleep 3
done

# Show logs
docker-compose logs --tail=20 app

echo "🎉 Deploy complete!"
