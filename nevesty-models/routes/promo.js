/**
 * Promo Codes API — БЛОК 21
 * Public: POST /api/promo/check
 * Admin:  GET/POST/PATCH/DELETE /api/admin/promo[/:id]
 */
const express = require('express');
const router = express.Router();
const { query, run, get } = require('../database');
const auth = require('../middleware/auth');

// ─── Helper: resolve promo and compute discount ───────────────────────────────
async function resolvePromo(code, orderTotal) {
  const promo = await get('SELECT * FROM promo_codes WHERE UPPER(code) = UPPER(?) AND is_active = 1', [code.trim()]);
  if (!promo) return { valid: false, reason: 'not_found' };

  const now = new Date().toISOString().slice(0, 10);
  if (promo.valid_from && now < promo.valid_from) return { valid: false, reason: 'not_started' };
  if (promo.valid_until && now > promo.valid_until) return { valid: false, reason: 'expired' };
  if (promo.max_uses !== null && promo.used_count >= promo.max_uses) {
    return { valid: false, reason: 'limit_reached' };
  }

  const total = parseFloat(orderTotal) || 0;
  let discount_amount = 0;
  if (promo.discount_type === 'percent') {
    discount_amount = Math.round((total * promo.discount_value) / 100);
  } else {
    discount_amount = promo.discount_value;
  }
  const final_price = Math.max(0, total - discount_amount);

  return {
    valid: true,
    promo_code_id: promo.id,
    discount_type: promo.discount_type,
    discount_value: promo.discount_value,
    discount_amount,
    final_price,
    promo,
  };
}

// ─── Public: check promo code ─────────────────────────────────────────────────
// POST /api/promo/check
router.post('/promo/check', async (req, res, next) => {
  try {
    const { code, order_total } = req.body;
    if (!code || typeof code !== 'string') {
      return res.status(400).json({ valid: false, reason: 'not_found' });
    }
    const result = await resolvePromo(code, order_total);
    if (!result.valid) return res.json({ valid: false, reason: result.reason });
    const { promo: _p, ...out } = result; // strip raw promo row
    return res.json(out);
  } catch (e) {
    next(e);
  }
});

// ─── Admin: list all promo codes ──────────────────────────────────────────────
// GET /api/admin/promo
router.get('/admin/promo', auth, async (req, res, next) => {
  try {
    const promos = await query(
      `SELECT p.*, a.username as created_by_name
       FROM promo_codes p
       LEFT JOIN admins a ON a.id = p.created_by
       ORDER BY p.created_at DESC`
    );
    res.json({ promos });
  } catch (e) {
    next(e);
  }
});

// ─── Admin: get stats ─────────────────────────────────────────────────────────
// GET /api/admin/promo/stats
router.get('/admin/promo/stats', auth, async (req, res, next) => {
  try {
    const total = await get('SELECT COUNT(*) as n FROM promo_codes');
    const active = await get('SELECT COUNT(*) as n FROM promo_codes WHERE is_active=1');
    const usages = await get('SELECT SUM(used_count) as n FROM promo_codes');
    const discountSum = await get(
      `SELECT ROUND(SUM(discount_amount),2) as n FROM orders WHERE discount_amount IS NOT NULL`
    );
    const topPromos = await query(
      `SELECT code, discount_type, discount_value, used_count, max_uses
       FROM promo_codes ORDER BY used_count DESC LIMIT 5`
    );
    res.json({
      total: total?.n || 0,
      active: active?.n || 0,
      total_usages: usages?.n || 0,
      total_discount_given: discountSum?.n || 0,
      top_promos: topPromos,
    });
  } catch (e) {
    next(e);
  }
});

// ─── Admin: create promo code ─────────────────────────────────────────────────
// POST /api/admin/promo
router.post('/admin/promo', auth, async (req, res, next) => {
  try {
    const { code, discount_type, discount_value, max_uses, valid_from, valid_until } = req.body;
    if (!code || typeof code !== 'string' || !code.trim()) {
      return res.status(400).json({ error: 'Укажите code' });
    }
    if (!['percent', 'fixed'].includes(discount_type)) {
      return res.status(400).json({ error: 'discount_type должен быть percent или fixed' });
    }
    const val = parseFloat(discount_value);
    if (!val || val <= 0) return res.status(400).json({ error: 'Укажите discount_value > 0' });
    if (discount_type === 'percent' && val > 100) {
      return res.status(400).json({ error: 'Процент скидки не может превышать 100' });
    }

    const result = await run(
      `INSERT INTO promo_codes (code, discount_type, discount_value, max_uses, valid_from, valid_until, created_by)
       VALUES (UPPER(?), ?, ?, ?, ?, ?, ?)`,
      [
        code.trim(),
        discount_type,
        val,
        max_uses ? parseInt(max_uses) : null,
        valid_from || null,
        valid_until || null,
        req.admin?.id || null,
      ]
    );
    res.json({ id: result.id, code: code.trim().toUpperCase() });
  } catch (e) {
    if (e.message && e.message.includes('UNIQUE')) {
      return res.status(409).json({ error: 'Промокод с таким кодом уже существует' });
    }
    next(e);
  }
});

// ─── Admin: update promo code ─────────────────────────────────────────────────
// PATCH /api/admin/promo/:id
router.patch('/admin/promo/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'Invalid id' });

    const existing = await get('SELECT * FROM promo_codes WHERE id=?', [id]);
    if (!existing) return res.status(404).json({ error: 'Промокод не найден' });

    // Validate discount_value against effective discount_type (existing or being updated)
    const effectiveType = req.body.discount_type !== undefined ? req.body.discount_type : existing.discount_type;
    if (req.body.discount_value !== undefined && req.body.discount_value !== '') {
      const val = parseFloat(req.body.discount_value);
      if (!val || val <= 0) return res.status(400).json({ error: 'discount_value должен быть > 0' });
      if (effectiveType === 'percent' && val > 100) {
        return res.status(400).json({ error: 'Процент скидки не может превышать 100' });
      }
    }
    if (req.body.discount_type !== undefined && !['percent', 'fixed'].includes(req.body.discount_type)) {
      return res.status(400).json({ error: 'discount_type должен быть percent или fixed' });
    }

    const fields = [];
    const params = [];
    const allowed = ['is_active', 'max_uses', 'valid_from', 'valid_until', 'discount_value', 'discount_type'];
    for (const key of allowed) {
      if (req.body[key] !== undefined) {
        fields.push(`${key}=?`);
        params.push(req.body[key] === '' ? null : req.body[key]);
      }
    }
    if (!fields.length) return res.status(400).json({ error: 'Нет полей для обновления' });
    params.push(id);
    await run(`UPDATE promo_codes SET ${fields.join(', ')} WHERE id=?`, params);
    const updated = await get('SELECT * FROM promo_codes WHERE id=?', [id]);
    res.json(updated);
  } catch (e) {
    next(e);
  }
});

// ─── Admin: delete promo code ─────────────────────────────────────────────────
// DELETE /api/admin/promo/:id
router.delete('/admin/promo/:id', auth, async (req, res, next) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'Invalid id' });
    const { changes } = await run('DELETE FROM promo_codes WHERE id=?', [id]);
    if (!changes) return res.status(404).json({ error: 'Промокод не найден' });
    res.json({ ok: true });
  } catch (e) {
    next(e);
  }
});

module.exports = router;
module.exports.resolvePromo = resolvePromo;
