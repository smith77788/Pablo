import React, { useState } from 'react';
import { useCreateOrder } from '../../hooks/useOrders';
import { OrderInsert, EventType, Model } from '../../types';

interface BookingFormProps {
  model?: Model;
  onSuccess?: (orderNumber: string) => void;
  onCancel?: () => void;
}

type Step = 'event' | 'date' | 'contact' | 'confirm';

const EVENT_TYPES: { value: EventType; label: string }[] = [
  { value: 'photo', label: '📷 Фотосессия' },
  { value: 'video', label: '🎬 Видеосъёмка' },
  { value: 'event', label: '🎉 Мероприятие' },
  { value: 'promo', label: '📢 Промоакция' },
  { value: 'fashion', label: '👗 Показ мод' },
  { value: 'commercial', label: '📺 Реклама' },
  { value: 'other', label: '📋 Другое' },
];

export const BookingForm: React.FC<BookingFormProps> = ({ model, onSuccess, onCancel }) => {
  const [step, setStep] = useState<Step>('event');
  const [form, setForm] = useState<Partial<OrderInsert>>({
    model_id: model?.id,
    event_duration: 4,
  });
  const createOrder = useCreateOrder();

  const steps: Step[] = ['event', 'date', 'contact', 'confirm'];
  const stepIndex = steps.indexOf(step);

  const update = (key: keyof OrderInsert, value: unknown) =>
    setForm(f => ({ ...f, [key]: value }));

  const handleSubmit = async () => {
    if (!form.client_name || !form.client_phone || !form.event_type) return;
    try {
      const order = await createOrder.mutateAsync(form as OrderInsert);
      onSuccess?.(order.order_number);
    } catch (err) {
      console.error('Booking error:', err);
    }
  };

  return (
    <div className="booking-form">
      <div className="booking-progress">
        {steps.map((s, i) => (
          <div key={s} className={`progress-step ${i <= stepIndex ? 'active' : ''}`}>
            {i + 1}
          </div>
        ))}
      </div>

      {step === 'event' && (
        <div className="booking-step">
          <h2>Тип мероприятия</h2>
          {model && <p>Модель: <strong>{model.name}</strong></p>}
          <div className="event-types">
            {EVENT_TYPES.map(et => (
              <button
                key={et.value}
                className={`event-type-btn ${form.event_type === et.value ? 'selected' : ''}`}
                onClick={() => update('event_type', et.value)}
              >
                {et.label}
              </button>
            ))}
          </div>
          <button
            className="btn btn-primary"
            disabled={!form.event_type}
            onClick={() => setStep('date')}
          >
            Далее →
          </button>
        </div>
      )}

      {step === 'date' && (
        <div className="booking-step">
          <h2>Дата и место</h2>
          <input
            type="date"
            value={form.event_date || ''}
            onChange={e => update('event_date', e.target.value)}
            className="form-input"
            min={new Date().toISOString().split('T')[0]}
          />
          <select
            value={form.event_duration || 4}
            onChange={e => update('event_duration', Number(e.target.value))}
            className="form-select"
          >
            {[2,4,6,8,12].map(h => <option key={h} value={h}>{h} часов</option>)}
          </select>
          <input
            type="text"
            placeholder="Место проведения"
            value={form.location || ''}
            onChange={e => update('location', e.target.value)}
            className="form-input"
          />
          <input
            type="text"
            placeholder="Бюджет (опционально)"
            value={form.budget || ''}
            onChange={e => update('budget', e.target.value)}
            className="form-input"
          />
          <div className="step-actions">
            <button className="btn btn-secondary" onClick={() => setStep('event')}>← Назад</button>
            <button className="btn btn-primary" onClick={() => setStep('contact')}>Далее →</button>
          </div>
        </div>
      )}

      {step === 'contact' && (
        <div className="booking-step">
          <h2>Ваши контакты</h2>
          <input
            type="text"
            placeholder="Ваше имя *"
            value={form.client_name || ''}
            onChange={e => update('client_name', e.target.value)}
            className="form-input"
            required
          />
          <input
            type="tel"
            placeholder="+7 (___) ___-__-__ *"
            value={form.client_phone || ''}
            onChange={e => update('client_phone', e.target.value)}
            className="form-input"
            required
          />
          <input
            type="email"
            placeholder="Email (опционально)"
            value={form.client_email || ''}
            onChange={e => update('client_email', e.target.value)}
            className="form-input"
          />
          <textarea
            placeholder="Комментарии к заявке"
            value={form.comments || ''}
            onChange={e => update('comments', e.target.value)}
            className="form-textarea"
            rows={3}
          />
          <div className="step-actions">
            <button className="btn btn-secondary" onClick={() => setStep('date')}>← Назад</button>
            <button
              className="btn btn-primary"
              disabled={!form.client_name || !form.client_phone}
              onClick={() => setStep('confirm')}
            >
              Далее →
            </button>
          </div>
        </div>
      )}

      {step === 'confirm' && (
        <div className="booking-step">
          <h2>Подтверждение заявки</h2>
          <div className="booking-summary">
            {model && <p><strong>Модель:</strong> {model.name}</p>}
            <p><strong>Тип:</strong> {EVENT_TYPES.find(e => e.value === form.event_type)?.label}</p>
            {form.event_date && <p><strong>Дата:</strong> {new Date(form.event_date).toLocaleDateString('ru-RU')}</p>}
            {form.event_duration && <p><strong>Длительность:</strong> {form.event_duration} часов</p>}
            {form.location && <p><strong>Место:</strong> {form.location}</p>}
            {form.budget && <p><strong>Бюджет:</strong> {form.budget}</p>}
            <hr />
            <p><strong>Имя:</strong> {form.client_name}</p>
            <p><strong>Телефон:</strong> {form.client_phone}</p>
            {form.client_email && <p><strong>Email:</strong> {form.client_email}</p>}
            {form.comments && <p><strong>Комментарии:</strong> {form.comments}</p>}
          </div>
          <div className="step-actions">
            <button className="btn btn-secondary" onClick={() => setStep('contact')}>← Назад</button>
            <button
              className="btn btn-success"
              onClick={handleSubmit}
              disabled={createOrder.isPending}
            >
              {createOrder.isPending ? 'Отправка...' : '✅ Отправить заявку'}
            </button>
          </div>
          {createOrder.isError && (
            <div className="form-error">Ошибка при отправке. Попробуйте снова.</div>
          )}
        </div>
      )}

      {onCancel && (
        <button className="btn-cancel" onClick={onCancel}>Отмена</button>
      )}
    </div>
  );
};
