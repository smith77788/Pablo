#!/bin/bash
set -e
cd /home/user/Pablo

echo "Deploying Nevesty Models..."

# Pull latest
git pull origin claude/modeling-agency-website-jp2Qd

# Install dependencies
cd nevesty-models && npm install --production && cd ..

# Run migrations (start bot briefly to trigger initDatabase)
# pm2 reload nevesty-models
pm2 reload all --update-env

echo "Deploy complete!"
