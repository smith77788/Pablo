import axios from 'axios';

const BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:3002/api/v1';

const client = axios.create({ baseURL: BASE });

client.interceptors.request.use((config) => {
  const token = typeof window !== 'undefined' ? localStorage.getItem('token') : null;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

client.interceptors.response.use(
  (r) => r.data,
  async (err) => {
    if (err.response?.status === 401 && typeof window !== 'undefined') {
      const refresh = localStorage.getItem('refreshToken');
      if (refresh) {
        try {
          const res = await axios.post(`${BASE}/auth/refresh`, { refreshToken: refresh });
          localStorage.setItem('token', res.data.accessToken);
          err.config.headers.Authorization = `Bearer ${res.data.accessToken}`;
          return client.request(err.config);
        } catch { localStorage.clear(); window.location.href = '/login'; }
      } else { window.location.href = '/login'; }
    }
    return Promise.reject(err);
  },
);

export const api = {
  get: (url: string) => client.get(url),
  post: (url: string, data?: unknown) => client.post(url, data),
  patch: (url: string, data?: unknown) => client.patch(url, data),
  delete: (url: string) => client.delete(url),
};

export const authApi = api;
