import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { supabase } from '../lib/supabase';

export function useSetting(key: string) {
  return useQuery({
    queryKey: ['setting', key],
    queryFn: async () => {
      const { data } = await supabase
        .from('bot_settings')
        .select('value')
        .eq('key', key)
        .single();
      return data?.value ?? null;
    },
  });
}

export function useSettings(keys?: string[]) {
  return useQuery({
    queryKey: ['settings', keys],
    queryFn: async () => {
      let query = supabase.from('bot_settings').select('key, value');
      if (keys?.length) query = query.in('key', keys);
      const { data, error } = await query;
      if (error) throw error;
      return Object.fromEntries(
        (data ?? []).map((r) => [r.key, r.value])
      ) as Record<string, string | null>;
    },
  });
}

export function useUpdateSetting() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ key, value }: { key: string; value: string }) => {
      const { error } = await supabase
        .from('bot_settings')
        .upsert({ key, value, updated_at: new Date().toISOString() });
      if (error) throw error;
    },
    onSuccess: (_, { key }) => {
      queryClient.invalidateQueries({ queryKey: ['setting', key] });
      queryClient.invalidateQueries({ queryKey: ['settings'] });
    },
  });
}
