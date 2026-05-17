import React, { useState } from 'react';
import { supabase } from '../../lib/supabase';

interface PromoResult {
  valid: boolean;
  discount_type?: 'percent' | 'fixed';
  discount_value?: number;
  message?: string;
}

interface PromoCodeInputProps {
  budget?: number;
  onApply: (result: PromoResult & { code: string }) => void;
}

export const PromoCodeInput: React.FC<PromoCodeInputProps> = ({ budget, onApply }) => {
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<PromoResult | null>(null);

  const validate = async () => {
    if (!code.trim()) return;
    setLoading(true);
    setResult(null);

    const { data } = await supabase
      .from('promo_codes')
      .select('*')
      .eq('code', code.toUpperCase().trim())
      .eq('active', true)
      .single();

    if (!data) {
      const r = { valid: false, message: 'Промокод не найден или недействителен' };
      setResult(r);
      setLoading(false);
      return;
    }

    const now = new Date();
    if (data.valid_until && new Date(data.valid_until) < now) {
      const r = { valid: false, message: 'Срок действия промокода истёк' };
      setResult(r);
      setLoading(false);
      return;
    }

    if (data.max_uses && data.used_count >= data.max_uses) {
      const r = { valid: false, message: 'Лимит использований исчерпан' };
      setResult(r);
      setLoading(false);
      return;
    }

    if (budget && data.min_budget && budget < data.min_budget) {
      const r = { valid: false, message: `Минимальный бюджет для этого промокода: ${data.min_budget} ₽` };
      setResult(r);
      setLoading(false);
      return;
    }

    const r: PromoResult = {
      valid: true,
      discount_type: data.discount_type,
      discount_value: data.discount_value,
      message: data.discount_type === 'percent'
        ? `Скидка ${data.discount_value}% применена!`
        : `Скидка ${data.discount_value} ₽ применена!`,
    };
    setResult(r);
    onApply({ ...r, code: code.toUpperCase().trim() });
    setLoading(false);
  };

  return (
    <div className="promo-code-input">
      <div className="promo-row">
        <input
          type="text"
          placeholder="Введите промокод"
          value={code}
          onChange={e => setCode(e.target.value.toUpperCase())}
          className="promo-input"
          onKeyDown={e => e.key === 'Enter' && validate()}
        />
        <button
          className="btn btn-secondary"
          onClick={validate}
          disabled={loading || !code.trim()}
        >
          {loading ? '...' : 'Применить'}
        </button>
      </div>
      {result && (
        <p className={`promo-result ${result.valid ? 'promo-result--success' : 'promo-result--error'}`}>
          {result.valid ? '✅' : '❌'} {result.message}
        </p>
      )}
    </div>
  );
};
