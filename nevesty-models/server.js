require('dotenv').config();
const express = require('express');
const path = require('path');
const cors = require('cors');
const { initDatabase } = require('./database');
const { initBot } = require('./bot');
const apiRouter = require('./routes/api');

// Fail fast if JWT_SECRET is insecure in production
if (process.env.NODE_ENV === 'production' && (!process.env.JWT_SECRET || process.env.JWT_SECRET === 'secret')) {
  console.error('FATAL: Set a strong JWT_SECRET in .env before running in production!');
  process.exit(1);
}

const app = express();
const PORT = process.env.PORT || 3000;

// Security headers
try {
  const helmet = require('helmet');
  app.use(helmet({ contentSecurityPolicy: false }));
} catch {}

// Rate limiting
try {
  const rateLimit = require('express-rate-limit');
  app.use('/api/orders', rateLimit({ windowMs: 15 * 60 * 1000, max: 20, message: { error: 'Слишком много запросов. Попробуйте позже.' } }));
  app.use('/api/admin/login', rateLimit({ windowMs: 15 * 60 * 1000, max: 10, message: { error: 'Слишком много попыток входа.' } }));
} catch {}

const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || '').split(',').map(s => s.trim()).filter(Boolean);
app.use(cors(ALLOWED_ORIGINS.length ? { origin: ALLOWED_ORIGINS } : {}));
app.use(express.json({ limit: '1mb' }));
app.use(express.urlencoded({ extended: true, limit: '1mb' }));
app.use('/uploads', express.static(path.join(__dirname, 'uploads')));
app.use(express.static(path.join(__dirname, 'public')));

app.use('/api', apiRouter);

// Serve correct file for any frontend path
app.get('*', (req, res) => {
  const reqPath = req.path;

  // Resolve directory paths (e.g. /admin/ → admin/index.html)
  let filePath = reqPath.startsWith('/') ? reqPath.slice(1) : reqPath;
  if (!filePath || filePath.endsWith('/')) filePath += 'index.html';
  if (!path.extname(filePath)) filePath += '.html';

  const fullPath = path.join(__dirname, 'public', filePath);
  res.sendFile(fullPath, err => {
    if (err) res.sendFile(path.join(__dirname, 'public', 'index.html'));
  });
});

async function start() {
  await initDatabase();

  const botInstance = initBot();
  if (botInstance) {
    apiRouter.setBot(botInstance);
  }

  app.listen(PORT, () => {
    console.log(`\n🌐 Nevesty Models running at http://localhost:${PORT}`);
    console.log(`🔐 Admin panel: http://localhost:${PORT}/admin/login.html`);
    console.log(`   Login: ${process.env.ADMIN_USERNAME || 'admin'} / ${process.env.ADMIN_PASSWORD || 'admin123'}\n`);
    if (!process.env.JWT_SECRET || process.env.JWT_SECRET === 'secret') {
      console.warn('⚠️  JWT_SECRET is not set or is insecure — set it in .env!');
    }
  });
}

start().catch(err => {
  console.error('Startup error:', err);
  process.exit(1);
});
