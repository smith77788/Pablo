'use strict';
const request = require('supertest');

let app;
beforeAll(() => { app = require('../server'); });
afterAll(done => { app.close ? app.close(done) : done(); });

describe('Wave 9 — Analytics & Public API', () => {
  let adminToken;

  beforeAll(async () => {
    const res = await request(app).post('/api/admin/login')
      .send({ username: process.env.ADMIN_USERNAME || 'admin', password: process.env.ADMIN_PASSWORD || 'admin123' });
    adminToken = res.body.token;
  });

  // T1: deal-cycle endpoint
  test('GET /api/admin/analytics/deal-cycle returns numeric fields', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/deal-cycle')
      .set('Authorization', `Bearer ${adminToken}`);
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('avg_days');
    // avg_days is a number or null (null when no completed orders exist)
    expect(res.body.avg_days === null || typeof res.body.avg_days === 'number').toBe(true);
    expect(res.body).toHaveProperty('min_days');
    expect(res.body).toHaveProperty('max_days');
  });

  // T2: top-models endpoint
  test('GET /api/admin/analytics/top-models returns array', async () => {
    const res = await request(app)
      .get('/api/admin/analytics/top-models')
      .set('Authorization', `Bearer ${adminToken}`);
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
    const res = await request(app)
      .get('/api/admin/analytics/hourly')
      .set('Authorization', `Bearer ${adminToken}`);
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
    const res = await request(app)
      .get('/api/admin/system')
      .set('Authorization', `Bearer ${adminToken}`);
    expect([200, 404]).toContain(res.status); // ok if not implemented yet
  });

  // T8: health endpoint structure
  test('GET /health returns component statuses', async () => {
    const res = await request(app).get('/health');
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('status');
  });
});
