'use strict';

// Handler registry — loaded by bot.js
// Each module exports its handler functions and receives deps via init()
module.exports = {
  adminHandlers: require('./admin'),
  // Future: catalogHandlers: require('./catalog'),
};
