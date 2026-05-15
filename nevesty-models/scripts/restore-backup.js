#!/usr/bin/env node
'use strict';
/**
 * Database restore script.
 * Usage: node scripts/restore-backup.js [backup-file-path]
 * If no path given, lists available backups and restores the latest.
 */

const fs = require('fs');
const path = require('path');

const DB_PATH = process.env.DB_PATH || './data/nevesty.db';
const BACKUP_DIR = process.env.BACKUP_DIR || './backups';

function listBackups() {
  if (!fs.existsSync(BACKUP_DIR)) {
    console.log('No backup directory found:', BACKUP_DIR);
    return [];
  }
  const files = fs.readdirSync(BACKUP_DIR)
    .filter(f => f.startsWith('nevesty_') && f.endsWith('.db'))
    .sort()
    .reverse(); // newest first
  return files.map(f => ({
    name: f,
    path: path.join(BACKUP_DIR, f),
    size: Math.round(fs.statSync(path.join(BACKUP_DIR, f)).size / 1024) + ' KB',
    date: f.replace('nevesty_', '').replace('.db', '').replace('T', ' ').replace(/-/g, (m, o) => o > 10 ? ':' : '-')
  }));
}

const args = process.argv.slice(2);
const backups = listBackups();

if (!backups.length) {
  console.log('No backups available in', BACKUP_DIR);
  process.exit(1);
}

if (args[0] === '--list' || args[0] === '-l') {
  console.log('\nAvailable backups:\n');
  backups.forEach((b, i) => {
    console.log(`  ${i + 1}. ${b.name} (${b.size}) — ${b.date}`);
  });
  process.exit(0);
}

const backupPath = args[0] || backups[0].path;

if (!fs.existsSync(backupPath)) {
  console.error('Backup file not found:', backupPath);
  process.exit(1);
}

// Safety: rename current DB
if (fs.existsSync(DB_PATH)) {
  const safeguard = DB_PATH + '.before-restore-' + Date.now();
  fs.copyFileSync(DB_PATH, safeguard);
  console.log('Current DB saved to:', safeguard);
}

// Restore
fs.copyFileSync(backupPath, DB_PATH);
console.log('Restored:', backupPath, '->', DB_PATH);
console.log('\nRestart the server to apply the restored database.');
