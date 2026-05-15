'use strict';
// Instagram Graph API service
// Docs: https://developers.facebook.com/docs/instagram-api
// Requires: INSTAGRAM_BUSINESS_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN in .env

const https = require('https');

const BASE = 'https://graph.facebook.com/v19.0';
const ACCOUNT_ID = process.env.INSTAGRAM_BUSINESS_ACCOUNT_ID;
const ACCESS_TOKEN = process.env.INSTAGRAM_ACCESS_TOKEN;

function isConfigured() {
  return !!(ACCOUNT_ID && ACCESS_TOKEN);
}

function apiRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const url = new URL(BASE + path);
    if (method === 'GET') {
      url.searchParams.set('access_token', ACCESS_TOKEN);
    }
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
    if (body) req.write(JSON.stringify({ ...body, access_token: ACCESS_TOKEN }));
    req.end();
  });
}

// Create a media container for a photo post
async function createPhotoContainer(imageUrl, caption) {
  if (!isConfigured()) throw new Error('Instagram not configured');
  return apiRequest('POST', `/${ACCOUNT_ID}/media`, {
    image_url: imageUrl,
    caption,
    media_type: 'IMAGE',
  });
}

// Create a video/reel container
async function createVideoContainer(videoUrl, caption, isReel = false) {
  if (!isConfigured()) throw new Error('Instagram not configured');
  return apiRequest('POST', `/${ACCOUNT_ID}/media`, {
    video_url: videoUrl,
    caption,
    media_type: isReel ? 'REELS' : 'VIDEO',
  });
}

// Publish a previously created media container
async function publishContainer(containerId) {
  if (!isConfigured()) throw new Error('Instagram not configured');
  return apiRequest('POST', `/${ACCOUNT_ID}/media_publish`, {
    creation_id: containerId,
  });
}

// One-step publish: create container then publish
async function publishPhoto(imageUrl, caption) {
  const { id } = await createPhotoContainer(imageUrl, caption);
  return publishContainer(id);
}

// Schedule a post (seconds from now, max 75 days)
async function schedulePost(imageUrl, caption, scheduledTime) {
  if (!isConfigured()) throw new Error('Instagram not configured');
  const scheduled_publish_time = scheduledTime ? Math.floor(new Date(scheduledTime).getTime() / 1000) : undefined;
  const body = {
    image_url: imageUrl,
    caption,
    media_type: 'IMAGE',
  };
  if (scheduled_publish_time) {
    body.published = false;
    body.scheduled_publish_time = scheduled_publish_time;
  }
  const { id } = await apiRequest('POST', `/${ACCOUNT_ID}/media`, body);
  return publishContainer(id);
}

// Get account insights (last 30 days)
async function getInsights(metrics = ['impressions', 'reach', 'profile_views', 'follower_count']) {
  if (!isConfigured()) throw new Error('Instagram not configured');
  const since = Math.floor((Date.now() - 30 * 86400000) / 1000);
  const until = Math.floor(Date.now() / 1000);
  return apiRequest(
    'GET',
    `/${ACCOUNT_ID}/insights?metric=${metrics.join(',')}&period=day&since=${since}&until=${until}`
  );
}

// Get recent media with engagement metrics
async function getRecentMedia(limit = 10) {
  if (!isConfigured()) throw new Error('Instagram not configured');
  return apiRequest(
    'GET',
    `/${ACCOUNT_ID}/media?fields=id,caption,media_type,timestamp,like_count,comments_count,insights.metric(impressions,reach,engagement)&limit=${limit}`
  );
}

// Check media container status (for async video uploads)
async function getContainerStatus(containerId) {
  if (!isConfigured()) throw new Error('Instagram not configured');
  return apiRequest('GET', `/${containerId}?fields=status_code,status`);
}

// Verify webhook signature from Meta
function verifyWebhookSignature(rawBody, signature) {
  const crypto = require('crypto');
  const secret = process.env.INSTAGRAM_APP_SECRET || '';
  if (!secret) return false;
  const expected = 'sha256=' + crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
  return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
}

module.exports = {
  isConfigured,
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
