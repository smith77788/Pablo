import React from 'react';
import { Model } from '../../types';

interface ModelCardProps {
  model: Model;
  onSelect?: (model: Model) => void;
  onBook?: (model: Model) => void;
}

export const ModelCard: React.FC<ModelCardProps> = ({ model, onSelect, onBook }) => {
  const photos = typeof model.photos === 'string'
    ? JSON.parse(model.photos || '[]')
    : model.photos || [];
  const mainPhoto = model.photo_main || photos[0] || null;

  return (
    <div className="model-card" onClick={() => onSelect?.(model)}>
      <div className="model-card__photo">
        {mainPhoto ? (
          <img src={mainPhoto} alt={model.name} loading="lazy" />
        ) : (
          <div className="model-card__photo-placeholder">No photo</div>
        )}
        {model.featured && (
          <span className="model-card__badge model-card__badge--top">⭐ Топ</span>
        )}
        {!model.available && (
          <span className="model-card__badge model-card__badge--busy">Занята</span>
        )}
      </div>
      <div className="model-card__info">
        <h3 className="model-card__name">{model.name}</h3>
        {model.city && <p className="model-card__city">{model.city}</p>}
        <div className="model-card__params">
          {model.age && <span>{model.age} лет</span>}
          {model.height && <span>{model.height} см</span>}
          {model.category && <span className="model-card__category">{model.category}</span>}
        </div>
        {model.order_count > 0 && (
          <p className="model-card__orders">{model.order_count} заказов</p>
        )}
        <div className="model-card__actions">
          <button
            className="btn btn-primary"
            onClick={(e) => { e.stopPropagation(); onBook?.(model); }}
            disabled={!model.available}
          >
            Забронировать
          </button>
        </div>
      </div>
    </div>
  );
};
