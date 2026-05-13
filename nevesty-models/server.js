require('dotenv').config();
const express = require('express');
const path = require('path');
const cors = require('cors');
const { initDatabase } = require('./database');
const { initBot } = require('./bot');
const apiRouter = require('./routes/api');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use('/uploads', express.static(path.join(__dirname, 'uploads')));
app.use(express.static(path.join(__dirname, 'public')));

app.use('/api', apiRouter);

// Fallback: serve index.html for SPA-like navigation
app.get('*', (req, res) => {
  if (!req.path.startsWith('/api')) {
    const file = req.path.endsWith('.html') ? req.path.slice(1) : 'index.html';
    const fullPath = path.join(__dirname, 'public', file);
    res.sendFile(fullPath, err => {
      if (err) res.sendFile(path.join(__dirname, 'public', 'index.html'));
    });
  }
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
  });
}

start().catch(err => {
  console.error('Startup error:', err);
  process.exit(1);
});
