import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vendor chunking keeps the initial landing bundle small: React + router +
// lucide ship as `vendor-core`, everything else is lazy-loaded per-route.
export default defineConfig({
  test: {
    environment: 'happy-dom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
  },
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  build: {
    target: 'es2020',
    cssCodeSplit: true,
    chunkSizeWarningLimit: 640,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('recharts') || id.includes('d3-')) return 'vendor-charts'
          if (id.includes('@paper-design') || id.includes('three') || id.includes('ogl')) return 'vendor-shaders'
          if (id.includes('lucide-react')) return 'vendor-icons'
          if (id.includes('motion')) return 'vendor-motion'
          if (id.includes('@sentry')) return 'vendor-sentry'
          if (id.includes('react-router')) return 'vendor-router'
          if (id.includes('react-dom') || id.includes('/react/') || id.includes('scheduler/')) return 'vendor-react'
          return 'vendor-misc'
        },
      },
    },
  },
})
