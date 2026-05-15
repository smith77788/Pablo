const jwt = require('jsonwebtoken');

module.exports = function authMiddleware(req, res, next) {
  const header = req.headers.authorization;
  if (!header || !header.startsWith('Bearer ')) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  const token = header.slice(7);
  try {
    const secret = process.env.JWT_SECRET;
    if (!secret) {
      return res.status(500).json({ error: 'JWT_SECRET not configured' });
    }
    const payload = jwt.verify(token, secret);
    // Reject client tokens from admin endpoints (type must be 'admin' or absent for legacy tokens)
    if (payload.type && payload.type !== 'admin') {
      return res.status(401).json({ error: 'Invalid token type' });
    }
    req.admin = payload;
    next();
  } catch {
    res.status(401).json({ error: 'Invalid token' });
  }
};
