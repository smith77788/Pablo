// Состояние только текущего окна: бизнес-данные не сохраняются в localStorage.
class VaWorkspace {
  constructor() {
    this.baselines = new Map();
    this.drafts = new Map();
    this.requests = new Map();
    this.pending = new Set();
    this.sequence = 0;
  }

  issue(view, key) {
    const ticket = {view, key, sequence: ++this.sequence};
    this.requests.set(view, ticket);
    return ticket;
  }

  current(ticket) {
    return this.requests.get(ticket.view) === ticket;
  }

  track(key, values) {
    const baseline = this.baselines.get(key) || {};
    const draft = {...(this.drafts.get(key) || {})};
    Object.entries(values).forEach(([field, value]) => {
      if (value === baseline[field]) delete draft[field];
      else draft[field] = value;
    });
    if (Object.keys(draft).length) this.drafts.set(key, draft);
    else this.drafts.delete(key);
  }

  mount(key, values) {
    this.baselines.set(key, {...values});
    const draft = this.drafts.get(key) || {};
    const restored = {...values};
    Object.keys(values).forEach(field => {
      if (Object.prototype.hasOwnProperty.call(draft, field)) restored[field] = draft[field];
    });
    this.track(key, restored);
    return restored;
  }

  acknowledge(key, submitted) {
    const draft = {...(this.drafts.get(key) || {})};
    Object.entries(submitted).forEach(([field, value]) => {
      // Правка, сделанная во время запроса, не принадлежит этому сохранению.
      if (draft[field] === value) delete draft[field];
    });
    this.baselines.set(key, {...(this.baselines.get(key) || {}), ...submitted});
    if (Object.keys(draft).length) this.drafts.set(key, draft);
    else this.drafts.delete(key);
  }

  dirty(key) { return this.drafts.has(key); }
  hasDrafts() { return this.drafts.size > 0; }
  lock(key) {
    if (this.pending.has(key)) return false;
    this.pending.add(key);
    return true;
  }
  unlock(key) { this.pending.delete(key); }
}
