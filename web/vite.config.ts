import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `kite web build` builds into a fresh folder (KITE_WEB_OUT) and swaps it in when the build succeeded.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: process.env.KITE_WEB_OUT || 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
    sourcemap: false,
  },
  // `npm run dev` on a laptop: the API of a `kite web serve --insecure-cookies` on this machine.
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8790', ws: true } } },
})
