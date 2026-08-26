import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built to web/dist, which service/app.py mounts. One artifact, one process.
// The dev proxy exists only so `npm run dev` works during development; the
// shipped bundle calls /api on its own origin and needs no configuration.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: { proxy: { '/api': 'http://127.0.0.1:8000' } },
})
