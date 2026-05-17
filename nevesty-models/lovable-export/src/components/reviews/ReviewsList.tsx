import React from 'react';
import { useReviews } from '../../hooks/useReviews';

const STARS = [1, 2, 3, 4, 5];

interface ReviewsListProps {
  modelId?: number;
  showAll?: boolean;
  limit?: number;
}

export const ReviewsList: React.FC<ReviewsListProps> = ({ modelId, limit = 10 }) => {
  const { data: reviews, isLoading } = modelId
    ? useReviews({ model_id: modelId, approved: true })
    : useReviews({ approved: true });

  if (isLoading) return <div className="reviews-loading">Загрузка отзывов...</div>;
  const items = (reviews || []).slice(0, limit);

  if (!items.length) return <div className="reviews-empty">Пока нет отзывов. Будьте первым!</div>;

  return (
    <div className="reviews-list">
      {items.map(review => (
        <div key={review.id} className="review-card">
          <div className="review-header">
            <span className="review-author">{review.client_name || 'Клиент'}</span>
            <span className="review-stars">
              {STARS.map(s => (
                <span key={s} className={s <= (review.rating || 0) ? 'star star--filled' : 'star'}>★</span>
              ))}
            </span>
            <span className="review-date">
              {new Date(review.created_at).toLocaleDateString('ru-RU')}
            </span>
          </div>
          <p className="review-text">{review.text}</p>
          {review.admin_reply && (
            <div className="review-reply">
              <strong>Ответ агентства:</strong> {review.admin_reply}
            </div>
          )}
        </div>
      ))}
    </div>
  );
};
