import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // `npm run dev` talks to uvicorn on :8000 without a CORS round-trip.
    proxy: { '/api': 'http://localhost:8000' },
  },
})
