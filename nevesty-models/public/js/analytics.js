// Analytics wrapper — GA4 + Yandex.Metrica
window.NM = window.NM || {};

NM.analytics = {
  // Consent handler — called by cookie-consent.js after user choice
  consent(level) {
    if (level === 'all' && typeof gtag !== 'undefined') {
      gtag('consent', 'update', { analytics_storage: 'granted' });
    }
  },

  // GA4 + Yandex.Metrica event tracking
  event(name, params = {}) {
    try {
      if (typeof gtag !== 'undefined') gtag('event', name, params);
      if (typeof ym !== 'undefined' && window.YM_ID) ym(window.YM_ID, 'reachGoal', name, params);
    } catch {}
  },

  // UTM parameter extraction
  getUTM() {
    const p = new URLSearchParams(window.location.search);
    return {
      source: p.get('utm_source') || '',
      medium: p.get('utm_medium') || '',
      campaign: p.get('utm_campaign') || '',
      content: p.get('utm_content') || '',
      term: p.get('utm_term') || ''
    };
  },

  saveUTM() {
    const utm = this.getUTM();
    if (utm.source) sessionStorage.setItem('utm', JSON.stringify(utm));
  },

  getSavedUTM() {
    try { return JSON.parse(sessionStorage.getItem('utm') || '{}'); } catch { return {}; }
  },

  // ─── Named tracking events ─────────────────────────────────────

  viewModel(modelId, modelName) {
    this.event('view_model', { model_id: modelId, model_name: modelName, ...this.getSavedUTM() });
  },

  startBooking(modelId, eventType) {
    this.event('begin_checkout', { model_id: modelId, event_type: eventType, ...this.getSavedUTM() });
  },

  submitOrder(eventType, budget) {
    this.event('purchase', { event_type: eventType, value: budget || 0, currency: 'RUB', ...this.getSavedUTM() });
  },

  addToFavorites(modelId, modelName) {
    this.event('add_to_wishlist', { model_id: modelId, model_name: modelName });
  },

  addToCompare(modelId, modelName) {
    this.event('add_to_compare', { model_id: modelId, model_name: modelName });
  },

  searchModels(params) {
    this.event('search', { search_term: JSON.stringify(params) });
  },

  filterCatalog(filterType, filterValue) {
    this.event('filter_catalog', { filter_type: filterType, filter_value: filterValue });
  },

  clickWhatsApp() {
    this.event('contact_whatsapp', { page: window.location.pathname });
  },

  clickTelegram() {
    this.event('contact_telegram', { page: window.location.pathname });
  },

  openQuickBooking() {
    this.event('quick_booking_open', { page: window.location.pathname });
  },

  submitQuickBooking() {
    this.event('quick_booking_submit', { ...this.getSavedUTM() });
  }
};

// Auto-init
document.addEventListener('DOMContentLoaded', () => {
  NM.analytics.saveUTM();

  // Only fire GA4/Metrica events when full analytics consent is given
  if (!window.NM?.cookieConsent || window.NM.cookieConsent.hasConsent()) {
    NM.analytics.event('page_view', {
      page_title: document.title,
      page_location: window.location.href,
      ...NM.analytics.getSavedUTM()
    });
  }

  // Auto-track WhatsApp/Telegram link clicks
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href]');
    if (!a) return;
    const href = a.href || '';
    if (href.includes('wa.me') || href.includes('whatsapp.com')) NM.analytics.clickWhatsApp();
    if (href.startsWith('https://t.me/')) NM.analytics.clickTelegram();
  });
});
