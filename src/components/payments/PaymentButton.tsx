import React, { useState } from 'react';

interface PaymentButtonProps {
  bookingId: number;
  amount: number;
  orderNumber: string;
  clientEmail?: string;
  clientChatId?: string;
  provider?: 'yookassa' | 'stripe';
}

export const PaymentButton: React.FC<PaymentButtonProps> = ({
  bookingId,
  amount,
  orderNumber,
  clientEmail,
  clientChatId,
  provider = 'yookassa',
}) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handlePay = async () => {
    setLoading(true);
    setError(null);

    // Call your backend API (Node.js server or Supabase Edge Function)
    // to create a payment and get redirect URL
    const res = await fetch('/api/payments/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        booking_id: bookingId,
        amount,
        order_number: orderNumber,
        client_email: clientEmail,
        client_chat_id: clientChatId,
        provider,
      }),
    });

    if (!res.ok) {
      setError('Ошибка создания платежа. Попробуйте снова.');
      setLoading(false);
      return;
    }

    const { payment_url } = await res.json();
    if (payment_url) {
      window.location.href = payment_url;
    } else {
      setError('Не удалось получить ссылку на оплату.');
    }
    setLoading(false);
  };

  return (
    <div className="payment-button-wrapper">
      <button
        className="btn btn-payment"
        onClick={handlePay}
        disabled={loading}
      >
        {loading ? 'Создаём платёж...' : `💳 Оплатить ${amount.toLocaleString('ru-RU')} ₽`}
      </button>
      {error && <p className="payment-error">{error}</p>}
    </div>
  );
};
