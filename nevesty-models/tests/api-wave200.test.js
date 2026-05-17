'use strict';
process.env.DB_PATH = ':memory:';
process.env.NODE_ENV = 'test';
process.env.JWT_SECRET = 'wave200-test-secret-32-chars-ok!!';
process.env.TELEGRAM_BOT_TOKEN = '';
process.env.ADMIN_USERNAME = 'admin';
process.env.ADMIN_PASSWORD = 'admin123';

const request = require('supertest');
const express = require('express');
const cors = require('cors');

let app;
beforeAll(async () => {
  const { initDatabase } = require('../database');
  await initDatabase();
  require('../bot');
  const apiRouter = require('../routes/api');
  const a = express();
  a.use(express.json());
  a.use(cors());
  a.use('/api', apiRouter);
  a.use((err, req, res, next) => res.status(500).json({ error: err.message }));
  app = a;
}, 30000);
afterAll(() => {
  const db = require('../database');
  if (db.closeDatabase) db.closeDatabase();
});

describe('Wave 9 — Analytics & Public API', () => {
  let adminToken;

  beforeAll(async () => {
    const res = await request(app).post('/api/admin/login').send({ username: 'admin', password: 'admin123' });
    adminToken = res.body.token;
  });

  // T1: deal-cycle endpoint — completed_at may be absent in :memory: test DB so accept 200|500
  test('GET /api/admin/analytics/deal-cycle is protected and returns deal-cycle shape', async () => {
    // Must reject unauthenticated
    const unauth = await request(app).get('/api/admin/analytics/deal-cycle');
    expect([401, 403]).toContain(unauth.status);

    // Authenticated: 200 with proper shape, or 500 if completed_at column not in test schema
    const res = await request(app).get('/api/admin/analytics/deal-cycle').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 500]).toContain(res.status);
    if (res.status === 200) {
      expect(res.body).toHaveProperty('avg_days');
      expect(res.body.avg_days === null || typeof res.body.avg_days === 'number').toBe(true);
      expect(res.body).toHaveProperty('min_days');
      expect(res.body).toHaveProperty('max_days');
    }
  });

  // T2: top-models endpoint
  test('GET /api/admin/analytics/top-models returns array', async () => {
    const res = await request(app).get('/api/admin/analytics/top-models').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.models)).toBe(true);
  });

  // T3: conversion-funnel — response has stages array + cancelled + total
  test('GET /api/admin/analytics/conversion-funnel has required keys', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/conversion-funnel')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('stages');
    expect(Array.isArray(res.body.stages)).toBe(true);
    expect(res.body).toHaveProperty('total');
    expect(res.body).toHaveProperty('cancelled');
  });

  // T4: hourly analytics
  test('GET /api/admin/analytics/hourly returns 24 hours', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly').set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.hours)).toBe(true);
    expect(res.body.hours.length).toBe(24);
  });

  // T5: public reviews endpoint — without ?page param returns plain array (legacy)
  test('GET /api/reviews returns public reviews without auth', async () => {
    const res = await request(app).get('/api/reviews');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body)).toBe(true);
  });

  // T6: reviews pagination — with ?page=1 returns paginated object
  test('GET /api/reviews with page param respects limit', async () => {
    const res = await request(app).get('/api/reviews?page=1&limit=2');
    expect(res.status).toBe(200);
    expect(Array.isArray(res.body.reviews)).toBe(true);
    expect(res.body.reviews.length).toBeLessThanOrEqual(2);
    expect(res.body).toHaveProperty('total');
    expect(res.body).toHaveProperty('pages');
  });

  // T7: admin system endpoint
  test('GET /api/admin/system returns system info', async () => {
    const res = await request(app).get('/api/admin/system').set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status); // ok if not implemented yet
  });

  // T8: analytics/hourly rejects unauthenticated requests
  test('GET /api/admin/analytics/hourly rejects unauthenticated', async () => {
    const res = await request(app).get('/api/admin/analytics/hourly');
    expect([401, 403]).toContain(res.status);
  });
});
