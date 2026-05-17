import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { supabase } from '../lib/supabase';
import { Order, OrderInsert, OrderFilters } from '../types';

export function useOrders(filters?: OrderFilters) {
  return useQuery({
    queryKey: ['orders', filters],
    queryFn: async () => {
      let query = supabase
        .from('orders')
        .select('*, models(id, name, photos)')
        .order('created_at', { ascending: false });

      if (filters?.status) query = query.eq('status', filters.status);
      if (filters?.model_id) query = query.eq('model_id', filters.model_id);
      if (filters?.manager_id) query = query.eq('manager_id', filters.manager_id);
      if (filters?.payment_status) query = query.eq('payment_status', filters.payment_status);
      if (filters?.from_date) query = query.gte('created_at', filters.from_date);
      if (filters?.to_date) query = query.lte('created_at', filters.to_date);
      if (filters?.search) query = query.ilike('client_name', `%${filters.search}%`);

      const { data, error } = await query;
      if (error) throw error;
      return data as Order[];
    },
  });
}

export function useOrder(id: number) {
  return useQuery({
    queryKey: ['order', id],
    queryFn: async () => {
      const { data, error } = await supabase
        .from('orders')
        .select('*, models(id, name, photos, category, city)')
        .eq('id', id)
        .single();
      if (error) throw error;
      return data as Order;
    },
    enabled: !!id,
  });
}

export function useCreateOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (order: OrderInsert) => {
      const { data, error } = await supabase.from('orders').insert(order).select().single();
      if (error) throw error;
      return data as Order;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['orders'] }),
  });
}

export function useUpdateOrderStatus() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, status }: { id: number; status: string }) => {
      const { data, error } = await supabase
        .from('orders')
        .update({ status, updated_at: new Date().toISOString() })
        .eq('id', id)
        .select()
        .single();
      if (error) throw error;
      return data as Order;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['order', data.id] });
    },
  });
}

export function useUpdateOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, ...updates }: Partial<Order> & { id: number }) => {
      const { data, error } = await supabase
        .from('orders')
        .update({ ...updates, updated_at: new Date().toISOString() })
        .eq('id', id)
        .select()
        .single();
      if (error) throw error;
      return data as Order;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['orders'] });
      queryClient.invalidateQueries({ queryKey: ['order', data.id] });
    },
  });
}
