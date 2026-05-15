'use strict';

const express = require('express');
const path = require('path');
const fs = require('fs');
const router = express.Router();
const { query, run, get } = require('../database');
const jwt = require('jsonwebtoken');

// ─── Auth middleware ──────────────────────────────────────────────────────────
function auth(req, res, next) {
  const header = req.headers['authorization'] || '';
  const token = header.startsWith('Bearer ') ? header.slice(7) : null;
  if (!token) return res.status(401).json({ error: 'Unauthorized' });
  try {
    const payload = jwt.verify(token, process.env.JWT_SECRET || 'secret');
    // Block non-admin typed tokens (e.g. client OTP tokens have type:'client')
    if (payload.type && payload.type !== 'admin') return res.status(403).json({ error: 'Forbidden' });
    req.admin = payload;
    next();
  } catch {
    return res.status(401).json({ error: 'Invalid token' });
  }
}

// ─── Revenue trend (monthly) ──────────────────────────────────────────────────
router.get('/admin/analytics/revenue', auth, async (req, res, next) => {
  try {
    const months = Math.min(Math.max(parseInt(req.query.months || '12'), 1), 36);
    const rows = await query(`
      SELECT
        strftime('%Y-%m', created_at) AS month,
        COUNT(*) AS orders,
        SUM(COALESCE(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER), 0)) AS revenue,
        AVG(COALESCE(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER), 0)) AS avg_budget
      FROM orders
      WHERE status IN ('completed','confirmed','in_progress')
        AND created_at >= datetime('now', '-' || ? || ' months')
      GROUP BY month
      ORDER BY month ASC
    `, [months]);

    const months_rows = rows.map(r => ({
      month: r.month,
      orders: r.orders,
      revenue: Math.round(r.revenue || 0),
      avg_budget: Math.round(r.avg_budget || 0),
    }));

    res.json({ months: months_rows });
  } catch (e) { next(e); }
});

// ─── Model performance stats ──────────────────────────────────────────────────
router.get('/admin/analytics/model-stats/:id', auth, async (req, res, next) => {
  try {
    const modelId = parseInt(req.params.id);
    if (!modelId) return res.status(400).json({ error: 'Invalid id' });

    const [model, orderStats, monthlyOrders, reviews] = await Promise.all([
      get('SELECT id, name, city, category, available, featured FROM models WHERE id = ?', [modelId]),
      get(`
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
          SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
          SUM(CASE WHEN status IN ('new','reviewing','confirmed','in_progress') THEN 1 ELSE 0 END) AS active,
          AVG(COALESCE(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER), 0)) AS avg_budget
        FROM orders WHERE model_id = ?
      `, [modelId]),
      query(`
        SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS cnt
        FROM orders WHERE model_id = ? AND created_at >= datetime('now','-12 months')
        GROUP BY month ORDER BY month
      `, [modelId]),
      query(`
        SELECT rating, COUNT(*) AS cnt, AVG(rating) AS avg_rating
        FROM reviews WHERE model_id = ? AND approved = 1
        GROUP BY rating ORDER BY rating DESC
      `, [modelId]),
    ]);

    if (!model) return res.status(404).json({ error: 'Model not found' });

    const totalReviews = reviews.reduce((s, r) => s + r.cnt, 0);
    const avgRating = totalReviews > 0
      ? reviews.reduce((s, r) => s + r.rating * r.cnt, 0) / totalReviews
      : null;

    res.json({
      model,
      orders: {
        total: orderStats?.total || 0,
        completed: orderStats?.completed || 0,
        cancelled: orderStats?.cancelled || 0,
        active: orderStats?.active || 0,
        avg_budget: Math.round(orderStats?.avg_budget || 0),
      },
      monthly_orders: monthlyOrders,
      reviews: {
        total: totalReviews,
        avg_rating: avgRating ? Math.round(avgRating * 10) / 10 : null,
        distribution: reviews,
      },
    });
  } catch (e) { next(e); }
});

// ─── Daily heatmap (calendar view) ───────────────────────────────────────────
router.get('/admin/analytics/heatmap', auth, async (req, res, next) => {
  try {
    const year = parseInt(req.query.year || new Date().getFullYear());
    const rows = await query(`
      SELECT
        strftime('%Y-%m-%d', created_at) AS day,
        COUNT(*) AS cnt
      FROM orders
      WHERE created_at >= ? AND created_at < ?
      GROUP BY day
      ORDER BY day
    `, [`${year}-01-01`, `${year + 1}-01-01`]);

    const heatmap = {};
    rows.forEach(r => { heatmap[r.day] = r.cnt; });

    res.json({ year, heatmap });
  } catch (e) { next(e); }
});

// ─── Client LTV distribution ──────────────────────────────────────────────────
router.get('/admin/analytics/client-ltv', auth, async (req, res, next) => {
  try {
    const clients = await query(`
      SELECT
        client_phone,
        client_name,
        COUNT(*) AS total_orders,
        SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
        MIN(created_at) AS first_order,
        MAX(created_at) AS last_order,
        SUM(COALESCE(CAST(REPLACE(REPLACE(budget,'₽',''),' ','') AS INTEGER), 0)) AS total_budget
      FROM orders
      WHERE client_phone IS NOT NULL AND client_phone != ''
      GROUP BY client_phone
      ORDER BY completed DESC, total_orders DESC
      LIMIT 50
    `);

    const buckets = { '1': 0, '2': 0, '3-5': 0, '6+': 0 };
    clients.forEach(c => {
      if (c.completed >= 6) buckets['6+']++;
      else if (c.completed >= 3) buckets['3-5']++;
      else if (c.completed === 2) buckets['2']++;
      else buckets['1']++;
    });

    const total = await get('SELECT COUNT(DISTINCT client_phone) AS cnt FROM orders WHERE client_phone IS NOT NULL AND client_phone != \'\'');

    res.json({
      top_clients: clients.slice(0, 20).map(c => ({
        name: c.client_name,
        phone: c.client_phone?.slice(-4).padStart(c.client_phone.length, '*'),
        total_orders: c.total_orders,
        completed: c.completed,
        total_budget: Math.round(c.total_budget || 0),
        first_order: c.first_order?.slice(0, 10),
        last_order: c.last_order?.slice(0, 10),
      })),
      buckets,
      total_clients: total?.cnt || 0,
    });
  } catch (e) { next(e); }
});

// ─── Database backup ──────────────────────────────────────────────────────────
router.post('/admin/db-backup', auth, async (req, res, next) => {
  try {
    if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Superadmin only' });

    const dbPath = process.env.DB_PATH || path.join(__dirname, '..', 'data', 'database.db');
    const backupDir = path.join(__dirname, '..', 'backups');

    if (!fs.existsSync(backupDir)) fs.mkdirSync(backupDir, { recursive: true });

    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const backupPath = path.join(backupDir, `backup-${ts}.db`);

    await run('VACUUM INTO ?', [backupPath]);

    const stat = fs.statSync(backupPath);
    const sizeMB = (stat.size / 1024 / 1024).toFixed(2);

    const files = fs.readdirSync(backupDir)
      .filter(f => f.endsWith('.db'))
      .sort()
      .reverse();

    if (files.length > 10) {
      files.slice(10).forEach(f => {
        try { fs.unlinkSync(path.join(backupDir, f)); } catch {}
      });
    }

    res.json({
      success: true,
      filename: `backup-${ts}.db`,
      size_mb: sizeMB,
      backup_path: backupPath,
    });
  } catch (e) { next(e); }
});

// ─── List backups ─────────────────────────────────────────────────────────────
router.get('/admin/db-backups', auth, async (req, res, next) => {
  try {
    if (req.admin.role !== 'superadmin') return res.status(403).json({ error: 'Superadmin only' });

    const backupDir = path.join(__dirname, '..', 'backups');
    if (!fs.existsSync(backupDir)) return res.json({ backups: [] });

    const files = fs.readdirSync(backupDir)
      .filter(f => f.endsWith('.db'))
      .map(f => {
        const stat = fs.statSync(path.join(backupDir, f));
        return { filename: f, size_mb: (stat.size / 1024 / 1024).toFixed(2), created_at: stat.mtime.toISOString() };
      })
      .sort((a, b) => b.created_at.localeCompare(a.created_at));

    res.json({ backups: files });
  } catch (e) { next(e); }
});

module.exports = router;
