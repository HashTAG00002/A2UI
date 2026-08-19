/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The island builds INTO the APP shell's static tree: Flask serves
// taskvm/workspace_ui/static under /static, so the built assets mount at
// /static/a2ui/ and the host page is GET /a2ui (a2ui_transport.py).
export default defineConfig({
  base: '/static/a2ui/',
  plugins: [react()],
  build: {
    outDir: '../static/a2ui',
    emptyOutDir: true,
  },
  server: {
    // dev-mode convenience: the APP shell (default port 3016) answers
    // /api/app/* while vite serves the island itself
    proxy: {
      '/api': 'http://localhost:3016',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/__tests__/setup.ts'],
    css: false,
  },
})
