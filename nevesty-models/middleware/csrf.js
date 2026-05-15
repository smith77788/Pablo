const crypto = require('crypto');

const TOKENS = new Map(); // token → { ip, expires }
const TOKEN_TTL = 2 * 60 * 60 * 1000; // 2 hours

// Cleanup old tokens every hour
setInterval(() => {
  const now = Date.now();
  for (const [t, v] of TOKENS) {
    if (v.expires < now) TOKENS.delete(t);
  }
}, 60 * 60 * 1000);

function generateToken(ip) {
  const token = crypto.randomBytes(32).toString('hex');
  TOKENS.set(token, { ip, expires: Date.now() + TOKEN_TTL });
  return token;
}

function validateToken(token, ip) {
  if (!token) return false;
  const entry = TOKENS.get(token);
  if (!entry) return false;
  if (entry.expires < Date.now()) { TOKENS.delete(token); return false; }
  // IP check is optional (some users have dynamic IPs / proxies)
  TOKENS.delete(token); // one-time use
  return true;
}

module.exports = { generateToken, validateToken };
