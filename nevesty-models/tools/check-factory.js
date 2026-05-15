#!/usr/bin/env node
/**
 * Checks factory last run time and alerts if > 12 hours
 * Run via cron: 0 * * * * node /home/user/Pablo/nevesty-models/tools/check-factory.js
 */
const path = require('path');
const fs = require('fs');
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

// Primary: factory/.last_run written by cycle.py
const FACTORY_LAST_RUN = path.join(__dirname, '..', '..', 'factory', '.last_run');
// Fallback: logs/last_run.txt (legacy path)
const FACTORY_LOG_LEGACY = path.join(__dirname, '..', '..', 'factory', 'logs', 'last_run.txt');
const MAX_AGE_HOURS = 12;

async function main() {
  let lastRunFile = null;
  if (fs.existsSync(FACTORY_LAST_RUN)) {
    lastRunFile = FACTORY_LAST_RUN;
  } else if (fs.existsSync(FACTORY_LOG_LEGACY)) {
    lastRunFile = FACTORY_LOG_LEGACY;
  }

  if (!lastRunFile) {
    console.log('Factory run file not found — skipping check');
    return;
  }

  const raw = fs.readFileSync(lastRunFile, 'utf8').trim();
  const lastRun = new Date(raw);
  if (isNaN(lastRun.getTime())) {
    console.error('Could not parse factory last run timestamp:', raw);
    return;
  }

  const ageHours = (Date.now() - lastRun.getTime()) / 3600000;
  if (ageHours > MAX_AGE_HOURS) {
    const { execSync } = require('child_process');
    const msg = `⚠️ AI Factory не запускалась ${Math.round(ageHours)} часов! Последний запуск: ${lastRun.toISOString().slice(0, 16).replace('T', ' ')} UTC. Проверьте систему.`;
    try {
      execSync(`node ${path.join(__dirname, 'notify.js')} --from "Monitor" ${JSON.stringify(msg)}`, {
        stdio: 'inherit',
      });
    } catch (e) {
      console.error('Failed to send alert:', e.message);
    }
  } else {
    console.log(`Factory OK — последний запуск ${Math.round(ageHours * 10) / 10} часов назад`);
  }
}

main().catch(console.error);
