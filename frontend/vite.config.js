import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev server proxies API calls to the FastAPI backend (uvicorn main:app on :8000).
// `npm run build` writes to ../static, which FastAPI serves at /studio.
export default defineConfig({
  plugins: [react()],
  base: '/studio/',
  server: { port: 5173, proxy: { '/api': 'http://localhost:8000' } },
  build: { outDir: '../static', emptyOutDir: false },
});
