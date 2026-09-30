import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  define: {
    'import.meta.env.VITE_DASHBOARD': JSON.stringify(mode === 'meeting' ? 'meetings' : 'employees'),
  },
  plugins: [
    react(),
    tailwindcss(),
  ],
  server: {
    port: mode === 'meeting' ? 5174 : 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
}))
