import { defineConfig } from 'vite';

export default defineConfig({
  build: { outDir: '../app/static', emptyOutDir: true },
  server: { proxy: {
    '/api': { target: 'http://127.0.0.1:8000', ws: true },
    '/health': 'http://127.0.0.1:8000',
    '/sync': 'http://127.0.0.1:8000',
    '/google-health': 'http://127.0.0.1:8000',
  } },
});
