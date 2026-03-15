import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8888',
    },
  },
  build: {
    outDir: '../pmon/dashboard/static/dist',
    emptyOutDir: true,
  },
})
