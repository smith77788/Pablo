import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { supabase } from '../lib/supabase';
import { Model, ModelInsert, ModelFilters } from '../types';

export function useModels(filters?: ModelFilters) {
  return useQuery({
    queryKey: ['models', filters],
    queryFn: async () => {
      let query = supabase
        .from('models')
        .select('*')
        .eq('archived', false)
        .order('featured', { ascending: false })
        .order('created_at', { ascending: false });

      if (filters?.category) query = query.eq('category', filters.category);
      if (filters?.city) query = query.eq('city', filters.city);
      if (filters?.available !== undefined) query = query.eq('available', filters.available);
      if (filters?.featured !== undefined) query = query.eq('featured', filters.featured);
      if (filters?.min_height) query = query.gte('height', filters.min_height);
      if (filters?.max_height) query = query.lte('height', filters.max_height);
      if (filters?.min_age) query = query.gte('age', filters.min_age);
      if (filters?.max_age) query = query.lte('age', filters.max_age);
      if (filters?.search) query = query.ilike('name', `%${filters.search}%`);

      const { data, error } = await query;
      if (error) throw error;
      return data as Model[];
    },
  });
}

export function useModel(id: number) {
  return useQuery({
    queryKey: ['model', id],
    queryFn: async () => {
      const { data, error } = await supabase
        .from('models')
        .select('*')
        .eq('id', id)
        .single();
      if (error) throw error;
      return data as Model;
    },
    enabled: !!id,
  });
}

export function useCreateModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (model: ModelInsert) => {
      const { data, error } = await supabase.from('models').insert(model).select().single();
      if (error) throw error;
      return data as Model;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['models'] }),
  });
}

export function useUpdateModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, ...updates }: Partial<Model> & { id: number }) => {
      const { data, error } = await supabase
        .from('models')
        .update(updates)
        .eq('id', id)
        .select()
        .single();
      if (error) throw error;
      return data as Model;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['models'] });
      queryClient.invalidateQueries({ queryKey: ['model', data.id] });
    },
  });
}

export function useDeleteModel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await supabase.from('models').update({ archived: true }).eq('id', id);
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['models'] }),
  });
}
