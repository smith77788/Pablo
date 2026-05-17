import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { supabase } from '../../lib/supabase';

interface Stats {
  orders_today: number;
  orders_week: number;
  orders_month: number;
  revenue_month: number;
  conversion_rate: number;
  avg_budget: number;
  top_models: Array<{ name: string; count: number }>;
  orders_by_status: Record<string, number>;
}

export const AnalyticsDashboard: React.FC = () => {
  const { data: stats, isLoading } = useQuery({
    queryKey: ['analytics'],
    queryFn: async (): Promise<Stats> => {
      const now = new Date();
      const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
      const weekStart = new Date(now.getTime() - 7 * 86400000).toISOString();
      const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).toISOString();

      const [todayRes, weekRes, monthRes, allRes, modelsRes] = await Promise.all([
        supabase.from('bookings').select('id', { count: 'exact' }).gte('created_at', todayStart),
        supabase.from('bookings').select('id', { count: 'exact' }).gte('created_at', weekStart),
        supabase.from('bookings').select('id, budget, status', { count: 'exact' }).gte('created_at', monthStart),
        supabase.from('bookings').select('status'),
        supabase.from('bookings').select('model_id, models(name)').not('model_id', 'is', null).gte('created_at', monthStart),
      ]);

      const monthOrders = monthRes.data || [];
      const allOrders = allRes.data || [];

      const revenue = monthOrders
        .filter(o => o.status === 'completed')
        .reduce((sum, o) => sum + parseFloat(o.budget || '0'), 0);

      const budgets = monthOrders.filter(o => parseFloat(o.budget) > 0).map(o => parseFloat(o.budget));
      const avgBudget = budgets.length ? budgets.reduce((a, b) => a + b, 0) / budgets.length : 0;

      const statusCounts: Record<string, number> = {};
      allOrders.forEach(o => { statusCounts[o.status] = (statusCounts[o.status] || 0) + 1; });

      const new_count = statusCounts['new'] || 0;
      const confirmed_count = statusCounts['confirmed'] || 0;
      const completed_count = statusCounts['completed'] || 0;
      const total = new_count + confirmed_count + completed_count;
      const conversion = total > 0 ? Math.round((completed_count / total) * 100) : 0;

      const modelCounts: Record<string, number> = {};
      (modelsRes.data || []).forEach((b: any) => {
        const name = b.models?.name || 'Неизвестно';
        modelCounts[name] = (modelCounts[name] || 0) + 1;
      });
      const topModels = Object.entries(modelCounts)
        .sort(([, a], [, b]) => b - a)
        .slice(0, 5)
        .map(([name, count]) => ({ name, count }));

      return {
        orders_today: todayRes.count || 0,
        orders_week: weekRes.count || 0,
        orders_month: monthRes.count || 0,
        revenue_month: revenue,
        conversion_rate: conversion,
        avg_budget: Math.round(avgBudget),
        top_models: topModels,
        orders_by_status: statusCounts,
      };
    },
    staleTime: 1000 * 60 * 5,
  });

  if (isLoading) return <div>Загрузка аналитики...</div>;
  if (!stats) return null;

  return (
    <div className="analytics-dashboard">
      <h2>Аналитика</h2>

      <div className="analytics-grid">
        <div className="analytics-card">
          <div className="analytics-value">{stats.orders_today}</div>
          <div className="analytics-label">Заявок сегодня</div>
        </div>
        <div className="analytics-card">
          <div className="analytics-value">{stats.orders_week}</div>
          <div className="analytics-label">За неделю</div>
        </div>
        <div className="analytics-card">
          <div className="analytics-value">{stats.orders_month}</div>
          <div className="analytics-label">За месяц</div>
        </div>
        <div className="analytics-card analytics-card--revenue">
          <div className="analytics-value">{stats.revenue_month.toLocaleString('ru-RU')} ₽</div>
          <div className="analytics-label">Выручка (месяц)</div>
        </div>
        <div className="analytics-card">
          <div className="analytics-value">{stats.conversion_rate}%</div>
          <div className="analytics-label">Конверсия</div>
        </div>
        <div className="analytics-card">
          <div className="analytics-value">{stats.avg_budget.toLocaleString('ru-RU')} ₽</div>
          <div className="analytics-label">Средний чек</div>
        </div>
      </div>

      {stats.top_models.length > 0 && (
        <div className="analytics-section">
          <h3>Топ моделей (месяц)</h3>
          {stats.top_models.map(m => (
            <div key={m.name} className="top-model-row">
              <span>{m.name}</span>
              <span className="top-model-count">{m.count} заказов</span>
            </div>
          ))}
        </div>
      )}

      <div className="analytics-section">
        <h3>Заявки по статусам</h3>
        {Object.entries(stats.orders_by_status).map(([status, count]) => (
          <div key={status} className="status-row">
            <span className={`status-badge status-badge--${status}`}>{status}</span>
            <span>{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
};
