'use strict';
// Instagram Graph API service
// Docs: https://developers.facebook.com/docs/instagram-api
// Credentials can be set via env vars OR stored in bot_settings DB (preferred)

const https = require('https');

const BASE = 'https://graph.facebook.com/v21.0';

function isConfigured() {
  return !!(process.env.INSTAGRAM_BUSINESS_ACCOUNT_ID && process.env.INSTAGRAM_ACCESS_TOKEN);
}

// Resolve credentials: prefer explicit params, fallback to env, fallback to DB
async function resolveCredentials(token, accountId) {
  if (token && accountId) return { token, accountId };
  if (process.env.INSTAGRAM_ACCESS_TOKEN && process.env.INSTAGRAM_BUSINESS_ACCOUNT_ID) {
    return { token: process.env.INSTAGRAM_ACCESS_TOKEN, accountId: process.env.INSTAGRAM_BUSINESS_ACCOUNT_ID };
  }
  try {
    const { get } = require('../database');
    const [tRow, aRow] = await Promise.all([
      get("SELECT value FROM bot_settings WHERE key='instagram_access_token'"),
      get("SELECT value FROM bot_settings WHERE key='instagram_account_id'"),
    ]);
    if (tRow?.value && aRow?.value) return { token: tRow.value, accountId: aRow.value };
  } catch (_) {}
  return null;
}

function apiRequest(method, path, body, token) {
  return new Promise((resolve, reject) => {
    const url = new URL(BASE + path);
    if (method === 'GET' && token) url.searchParams.set('access_token', token);
    const options = {
      hostname: url.hostname,
      path: url.pathname + url.search,
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    const req = https.request(options, res => {
      let data = '';
      res.on('data', d => (data += d));
      res.on('end', () => {
        try {
          const json = JSON.parse(data);
          if (json.error) reject(new Error(json.error.message));
          else resolve(json);
        } catch (e) {
          reject(e);
        }
      });
    });
    req.on('error', reject);
    req.setTimeout(15000, () => {
      req.destroy();
      reject(new Error('Instagram API: timeout'));
    });
    if (body) req.write(JSON.stringify(token ? { ...body, access_token: token } : body));
    req.end();
  });
}

// Validate token and return account info
async function validateToken(token, accountId) {
  try {
    const url = new URL(`${BASE}/${accountId}`);
    url.searchParams.set('fields', 'id,username,name,biography,followers_count');
    url.searchParams.set('access_token', token);
    const data = await new Promise((resolve, reject) => {
      const req = https.get(url.toString(), res => {
        let d = '';
        res.on('data', c => (d += c));
        res.on('end', () => {
          try {
            resolve(JSON.parse(d));
          } catch {
            reject(new Error('invalid JSON'));
          }
        });
      });
      req.on('error', reject);
      req.setTimeout(10000, () => {
        req.destroy();
        reject(new Error('timeout'));
      });
    });
    if (data.error) return { ok: false, error: data.error.message };
    if (!data.id) return { ok: false, error: 'Account not found' };
    return { ok: true, username: data.username || data.name, id: data.id, followers: data.followers_count };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// Create a media container for a photo post
async function createPhotoContainer(imageUrl, caption, token, accountId) {
  const creds = await resolveCredentials(token, accountId);
  if (!creds) throw new Error('Instagram не подключён — установите токен в настройках бота');
  return apiRequest(
    'POST',
    `/${creds.accountId}/media`,
    { image_url: imageUrl, caption, media_type: 'IMAGE' },
    creds.token
  );
}

// Create a video/reel container
async function createVideoContainer(videoUrl, caption, isReel = false, token, accountId) {
  const creds = await resolveCredentials(token, accountId);
  if (!creds) throw new Error('Instagram не подключён — установите токен в настройках бота');
  return apiRequest(
    'POST',
    `/${creds.accountId}/media`,
    { video_url: videoUrl, caption, media_type: isReel ? 'REELS' : 'VIDEO' },
    creds.token
  );
}

// Publish a previously created media container
async function publishContainer(containerId, token, accountId) {
  const creds = await resolveCredentials(token, accountId);
  if (!creds) throw new Error('Instagram не подключён');
  return apiRequest('POST', `/${creds.accountId}/media_publish`, { creation_id: containerId }, creds.token);
}

// One-step publish: create container then publish
async function publishPhoto(imageUrl, caption, token, accountId) {
  const { id } = await createPhotoContainer(imageUrl, caption, token, accountId);
  return publishContainer(id, token, accountId);
}

// Schedule a post (seconds from now, max 75 days)
async function schedulePost(imageUrl, caption, scheduledTime, token, accountId) {
  const creds = await resolveCredentials(token, accountId);
  if (!creds) throw new Error('Instagram не подключён');
  const scheduled_publish_time = scheduledTime ? Math.floor(new Date(scheduledTime).getTime() / 1000) : undefined;
  const body = { image_url: imageUrl, caption, media_type: 'IMAGE' };
  if (scheduled_publish_time) {
    body.published = false;
    body.scheduled_publish_time = scheduled_publish_time;
  }
  const { id } = await apiRequest('POST', `/${creds.accountId}/media`, body, creds.token);
  return publishContainer(id, creds.token, creds.accountId);
}

// Get account insights (last 30 days)
async function getInsights(metrics = ['impressions', 'reach', 'profile_views', 'follower_count'], token, accountId) {
  const creds = await resolveCredentials(token, accountId);
  if (!creds) throw new Error('Instagram не подключён');
  const since = Math.floor((Date.now() - 30 * 86400000) / 1000);
  const until = Math.floor(Date.now() / 1000);
  return apiRequest(
    'GET',
    `/${creds.accountId}/insights?metric=${metrics.join(',')}&period=day&since=${since}&until=${until}`,
    null,
    creds.token
  );
}

// Get recent media with engagement metrics
async function getRecentMedia(limit = 10, token, accountId) {
  const creds = await resolveCredentials(token, accountId);
  if (!creds) throw new Error('Instagram не подключён');
  return apiRequest(
    'GET',
    `/${creds.accountId}/media?fields=id,caption,media_type,timestamp,like_count,comments_count&limit=${limit}`,
    null,
    creds.token
  );
}

// Check media container status (for async video uploads)
async function getContainerStatus(containerId, token) {
  const creds = await resolveCredentials(token, null);
  return apiRequest('GET', `/${containerId}?fields=status_code,status`, null, creds?.token || token);
}

// Verify webhook signature from Meta
function verifyWebhookSignature(rawBody, signature) {
  const crypto = require('crypto');
  const secret = process.env.INSTAGRAM_APP_SECRET || '';
  if (!secret) return false;
  const expected = 'sha256=' + crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
  const a = Buffer.from(typeof signature === 'string' ? signature : '');
  const b = Buffer.from(expected);
  // timingSafeEqual throws if lengths differ — return false instead
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

module.exports = {
  isConfigured,
  validateToken,
  resolveCredentials,
  publishPhoto,
  schedulePost,
  createPhotoContainer,
  publishContainer,
  createVideoContainer,
  getInsights,
  getRecentMedia,
  getContainerStatus,
  verifyWebhookSignature,
};
