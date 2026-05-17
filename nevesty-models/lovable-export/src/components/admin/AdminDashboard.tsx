import React, { useState } from 'react';
import { useOrders } from '../../hooks/useOrders';
import { useModels } from '../../hooks/useModels';
import { useReviews } from '../../hooks/useReviews';

export const AdminDashboard: React.FC = () => {
  const { data: orders } = useOrders();
  const { data: models } = useModels();
  const { data: reviews } = useReviews();

  const stats = {
    total_orders: orders?.length ?? 0,
    new_orders: orders?.filter(o => o.status === 'new').length ?? 0,
    confirmed_orders: orders?.filter(o => o.status === 'confirmed').length ?? 0,
    completed_orders: orders?.filter(o => o.status === 'completed').length ?? 0,
    total_models: models?.length ?? 0,
    available_models: models?.filter(m => m.available).length ?? 0,
    pending_reviews: reviews?.filter(r => r.approved === false).length ?? 0,
  };

  return (
    <div className="admin-dashboard">
      <h1>Панель управления</h1>

      <div className="stats-grid">
        <div className="stat-card stat-card--new">
          <div className="stat-value">{stats.new_orders}</div>
          <div className="stat-label">Новых заявок</div>
        </div>
        <div className="stat-card stat-card--confirmed">
          <div className="stat-value">{stats.confirmed_orders}</div>
          <div className="stat-label">Подтверждено</div>
        </div>
        <div className="stat-card stat-card--completed">
          <div className="stat-value">{stats.completed_orders}</div>
          <div className="stat-label">Выполнено</div>
        </div>
        <div className="stat-card">
          <div className="stat-value">{stats.total_models}</div>
          <div className="stat-label">Моделей</div>
        </div>
        <div className="stat-card">
          <div className="stat-value">{stats.available_models}</div>
          <div className="stat-label">Свободных</div>
        </div>
        <div className="stat-card stat-card--warning">
          <div className="stat-value">{stats.pending_reviews}</div>
          <div className="stat-label">Отзывов на проверке</div>
        </div>
      </div>

      <div className="recent-orders">
        <h2>Последние заявки</h2>
        <table className="orders-table">
          <thead>
            <tr>
              <th>№</th>
              <th>Клиент</th>
              <th>Модель</th>
              <th>Дата</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {orders?.slice(0, 10).map(order => (
              <tr key={order.id}>
                <td>{order.order_number}</td>
                <td>{order.client_name}</td>
                <td>{order.model_id}</td>
                <td>{order.event_date}</td>
                <td>
                  <span className={`status-badge status-badge--${order.status}`}>
                    {order.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
