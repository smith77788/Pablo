import React from 'react';
import { useSettings, useUpdateSetting } from '../../hooks/useSettings';

const SETTINGS_SECTIONS = [
  {
    title: '💬 Контакты',
    settings: [
      { key: 'agency_phone', label: 'Телефон', type: 'text' },
      { key: 'agency_email', label: 'Email', type: 'text' },
      { key: 'contacts_whatsapp', label: 'WhatsApp', type: 'text' },
      { key: 'greeting_text', label: 'Текст приветствия', type: 'textarea' },
      { key: 'about_text', label: 'О нас', type: 'textarea' },
    ],
  },
  {
    title: '🔔 Уведомления',
    settings: [
      { key: 'notif_new_order', label: 'Новые заявки', type: 'toggle' },
      { key: 'notif_new_review', label: 'Новые отзывы', type: 'toggle' },
      { key: 'notif_new_message', label: 'Новые сообщения', type: 'toggle' },
    ],
  },
  {
    title: '🛒 Бронирование',
    settings: [
      { key: 'booking_auto_confirm', label: 'Авто-подтверждение', type: 'toggle' },
      { key: 'booking_require_email', label: 'Требовать Email', type: 'toggle' },
      { key: 'booking_min_budget', label: 'Минимальный бюджет', type: 'text' },
    ],
  },
  {
    title: '⭐ Отзывы',
    settings: [
      { key: 'reviews_auto_approve', label: 'Авто-одобрение', type: 'toggle' },
      { key: 'reviews_min_completed', label: 'Мин. заказов для отзыва', type: 'number' },
    ],
  },
  {
    title: '🏆 Программы',
    settings: [
      { key: 'loyalty_enabled', label: 'Программа лояльности', type: 'toggle' },
      { key: 'referral_enabled', label: 'Реферальная программа', type: 'toggle' },
    ],
  },
];

export const SettingsPanel: React.FC = () => {
  const allKeys = SETTINGS_SECTIONS.flatMap(s => s.settings.map(set => set.key));
  const { data: settings, isLoading } = useSettings(allKeys);
  const updateSetting = useUpdateSetting();

  if (isLoading) return <div>Загрузка настроек...</div>;

  const handleToggle = (key: string, currentValue: string | null | undefined) => {
    updateSetting.mutate({ key, value: currentValue === '1' ? '0' : '1' });
  };

  const handleText = (key: string, value: string) => {
    updateSetting.mutate({ key, value });
  };

  return (
    <div className="settings-panel">
      <h1>Настройки</h1>
      {SETTINGS_SECTIONS.map(section => (
        <div key={section.title} className="settings-section">
          <h2>{section.title}</h2>
          {section.settings.map(setting => (
            <div key={setting.key} className="setting-row">
              <label className="setting-label">{setting.label}</label>
              {setting.type === 'toggle' ? (
                <button
                  className={`toggle-btn ${settings?.[setting.key] === '1' ? 'on' : 'off'}`}
                  onClick={() => handleToggle(setting.key, settings?.[setting.key])}
                >
                  {settings?.[setting.key] === '1' ? '✅ Вкл' : '❌ Выкл'}
                </button>
              ) : setting.type === 'textarea' ? (
                <textarea
                  defaultValue={settings?.[setting.key] || ''}
                  onBlur={e => handleText(setting.key, e.target.value)}
                  className="setting-textarea"
                  rows={3}
                />
              ) : (
                <input
                  type={setting.type}
                  defaultValue={settings?.[setting.key] || ''}
                  onBlur={e => handleText(setting.key, e.target.value)}
                  className="setting-input"
                />
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
};
