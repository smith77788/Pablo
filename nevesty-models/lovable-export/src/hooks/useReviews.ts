import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { supabase } from '../lib/supabase';
import { Review, ReviewInsert, ReviewFilters, ReviewStatus } from '../types';

export function useReviews(filters?: ReviewFilters | boolean) {
  // Support legacy `useReviews(approved?: boolean)` call signature
  const normalizedFilters: ReviewFilters | undefined =
    typeof filters === 'boolean' ? { approved: filters } : filters;

  return useQuery({
    queryKey: ['reviews', normalizedFilters],
    queryFn: async () => {
      let query = supabase
        .from('reviews')
        .select('*, models(id, name)')
        .order('created_at', { ascending: false });

      if (normalizedFilters?.approved !== undefined)
        query = query.eq('approved', normalizedFilters.approved);
      if (normalizedFilters?.status)
        query = query.eq('status', normalizedFilters.status);
      if (normalizedFilters?.model_id)
        query = query.eq('model_id', normalizedFilters.model_id);
      if (normalizedFilters?.search)
        query = query.ilike('client_name', `%${normalizedFilters.search}%`);

      const { data, error } = await query;
      if (error) throw error;
      return data as Review[];
    },
  });
}

export function useModelReviews(modelId: number) {
  return useQuery({
    queryKey: ['reviews', 'model', modelId],
    queryFn: async () => {
      const { data, error } = await supabase
        .from('reviews')
        .select('*')
        .eq('model_id', modelId)
        .eq('approved', true)
        .eq('status', 'approved' satisfies ReviewStatus)
        .order('created_at', { ascending: false });
      if (error) throw error;
      return data as Review[];
    },
    enabled: !!modelId,
  });
}

export function useCreateReview() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (review: ReviewInsert) => {
      const { data, error } = await supabase.from('reviews').insert(review).select().single();
      if (error) throw error;
      return data as Review;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['reviews'] }),
  });
}

export function useApproveReview() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      approved,
    }: {
      id: number;
      approved: boolean;
    }) => {
      const status: ReviewStatus = approved ? 'approved' : 'rejected';
      const { data, error } = await supabase
        .from('reviews')
        .update({ approved, status, rejected: !approved })
        .eq('id', id)
        .select()
        .single();
      if (error) throw error;
      return data as Review;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['reviews'] });
      if (data.model_id) {
        queryClient.invalidateQueries({ queryKey: ['reviews', 'model', data.model_id] });
      }
    },
  });
}

export function useReplyToReview() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, reply }: { id: number; reply: string }) => {
      const { data, error } = await supabase
        .from('reviews')
        .update({ admin_reply: reply, reply_at: new Date().toISOString() })
        .eq('id', id)
        .select()
        .single();
      if (error) throw error;
      return data as Review;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['reviews'] }),
  });
}

export function useDeleteReview() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { error } = await supabase.from('reviews').delete().eq('id', id);
      if (error) throw error;
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['reviews'] }),
  });
}
