// Analytics wrapper — GA4 + Yandex.Metrica
window.NM = window.NM || {};

NM.analytics = {
  // GA4 event tracking
  event(name, params = {}) {
    if (typeof gtag !== 'undefined') {
      gtag('event', name, params);
    }
    if (typeof ym !== 'undefined' && window.YM_ID) {
      ym(window.YM_ID, 'reachGoal', name, params);
    }
  },

  // UTM parameter extraction
  getUTM() {
    const params = new URLSearchParams(window.location.search);
    return {
      source: params.get('utm_source') || '',
      medium: params.get('utm_medium') || '',
      campaign: params.get('utm_campaign') || '',
      content: params.get('utm_content') || '',
      term: params.get('utm_term') || ''
    };
  },

  // Save UTM to sessionStorage
  saveUTM() {
    const utm = this.getUTM();
    if (utm.source) {
      sessionStorage.setItem('utm', JSON.stringify(utm));
    }
  },

  // Get saved UTM (for form submission)
  getSavedUTM() {
    try {
      return JSON.parse(sessionStorage.getItem('utm') || '{}');
    } catch { return {}; }
  }
};

// Auto-save UTM on page load
document.addEventListener('DOMContentLoaded', () => NM.analytics.saveUTM());

// Page view event
document.addEventListener('DOMContentLoaded', () => {
  NM.analytics.event('page_view', {
    page_title: document.title,
    page_location: window.location.href,
    ...NM.analytics.getSavedUTM()
  });
});
