import React, { useState } from 'react';
import { useModels } from '../../hooks/useModels';
import { ModelCard } from './ModelCard';
import { ModelFilters, Model } from '../../types';

export const CatalogPage: React.FC = () => {
  const [filters, setFilters] = useState<ModelFilters>({});
  const [selectedModel, setSelectedModel] = useState<Model | null>(null);
  const { data: models, isLoading, error } = useModels(filters);

  const categories = ['fashion', 'commercial', 'events'];
  const cities = ['Москва', 'Санкт-Петербург', 'Екатеринбург', 'Новосибирск'];

  if (isLoading) return <div className="catalog-loading">Загрузка...</div>;
  if (error) return <div className="catalog-error">Ошибка загрузки каталога</div>;

  return (
    <div className="catalog-page">
      <div className="catalog-filters">
        <input
          type="text"
          placeholder="Поиск по имени..."
          value={filters.search || ''}
          onChange={e => setFilters(f => ({ ...f, search: e.target.value || undefined }))}
          className="filter-search"
        />
        <select
          value={filters.category || ''}
          onChange={e => setFilters(f => ({ ...f, category: e.target.value || undefined }))}
          className="filter-select"
        >
          <option value="">Все категории</option>
          {categories.map(c => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <select
          value={filters.city || ''}
          onChange={e => setFilters(f => ({ ...f, city: e.target.value || undefined }))}
          className="filter-select"
        >
          <option value="">Все города</option>
          {cities.map(c => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <label className="filter-checkbox">
          <input
            type="checkbox"
            checked={filters.available === true}
            onChange={e => setFilters(f => ({ ...f, available: e.target.checked ? true : undefined }))}
          />
          Только свободные
        </label>
        <label className="filter-checkbox">
          <input
            type="checkbox"
            checked={filters.featured === true}
            onChange={e => setFilters(f => ({ ...f, featured: e.target.checked ? true : undefined }))}
          />
          Топ-модели
        </label>
      </div>

      <div className="catalog-stats">
        Найдено: {models?.length || 0} моделей
      </div>

      <div className="catalog-grid">
        {models?.map(model => (
          <ModelCard
            key={model.id}
            model={model}
            onSelect={setSelectedModel}
            onBook={(m) => { /* navigate to booking */ }}
          />
        ))}
        {models?.length === 0 && (
          <div className="catalog-empty">Модели не найдены. Попробуйте изменить фильтры.</div>
        )}
      </div>
    </div>
  );
};
