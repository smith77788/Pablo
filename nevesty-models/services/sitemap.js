'use strict';

/**
 * services/sitemap.js
 * SEO: Generate and persist public/sitemap.xml from active models + static pages.
 *
 * Usage:
 *   const { generateSitemap } = require('./sitemap');
 *   await generateSitemap();
 */

const fs = require('fs');
const path = require('path');
const { query } = require('../database');

const SITEMAP_PATH = path.join(__dirname, '../public/sitemap.xml');

const STATIC_PAGES = [
  { path: '/', priority: '1.0', freq: 'daily' },
  { path: '/catalog.html', priority: '0.9', freq: 'daily' },
  { path: '/booking.html', priority: '0.9', freq: 'weekly' },
  { path: '/about.html', priority: '0.7', freq: 'monthly' },
  { path: '/reviews.html', priority: '0.7', freq: 'weekly' },
  { path: '/faq.html', priority: '0.6', freq: 'monthly' },
  { path: '/contact.html', priority: '0.6', freq: 'monthly' },
  { path: '/pricing.html', priority: '0.7', freq: 'weekly' },
  { path: '/cases.html', priority: '0.7', freq: 'monthly' },
  { path: '/search.html', priority: '0.6', freq: 'weekly' },
  { path: '/favorites.html', priority: '0.5', freq: 'weekly' },
];

/**
 * Reads all active (non-archived, available) models from the DB,
 * builds a full sitemap XML, and writes it to public/sitemap.xml.
 */
async function generateSitemap() {
  const models = await query(
    'SELECT id, name, created_at FROM models WHERE available=1 AND COALESCE(archived,0)=0 ORDER BY id'
  );

  const baseUrl = process.env.SITE_URL || 'https://nevesty-models.ru';
  const today = new Date().toISOString().split('T')[0];

  const staticUrls = STATIC_PAGES.map(
    p =>
      `  <url>\n    <loc>${baseUrl}${p.path}</loc>\n    <lastmod>${today}</lastmod>\n    <changefreq>${p.freq}</changefreq>\n    <priority>${p.priority}</priority>\n  </url>`
  ).join('\n');

  const modelUrls = models
    .map(m => {
      const lastmod = m.created_at ? m.created_at.split('T')[0] || m.created_at.slice(0, 10) : today;
      return `  <url>\n    <loc>${baseUrl}/model/${m.id}</loc>\n    <lastmod>${lastmod}</lastmod>\n    <changefreq>weekly</changefreq>\n    <priority>0.8</priority>\n  </url>`;
    })
    .join('\n');

  const xml =
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    `${staticUrls}\n` +
    `${modelUrls}\n` +
    `</urlset>`;

  fs.writeFileSync(SITEMAP_PATH, xml, 'utf8');
  console.log(
    `[Sitemap] Written ${models.length} model URL(s) + ${STATIC_PAGES.length} static pages → ${SITEMAP_PATH}`
  );
}

module.exports = { generateSitemap };
