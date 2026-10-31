import { defineConfig } from 'vite';
import { performanceBudget } from './build-budget.js';

export default defineConfig({
  plugins: [performanceBudget()],
  build: { outDir: '../app/static', emptyOutDir: true },
  server: { proxy: {
    '/api': { target: 'http://127.0.0.1:8000', ws: true },
    '/health': 'http://127.0.0.1:8000',
    '/sync': 'http://127.0.0.1:8000',
    '/google-health': 'http://127.0.0.1:8000',
  } },
});
