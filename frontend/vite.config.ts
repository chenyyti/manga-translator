import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

const devServerPort = Number(process.env.VITE_DEV_SERVER_PORT ?? 5173)
const apiTarget = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8000'
const websocketOrigin = process.env.VITE_WEBSOCKET_ORIGIN

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: devServerPort,
    strictPort: true,
    proxy: {
      '/api': {
        target: apiTarget,
        ws: true,
        configure(proxy) {
          if (!websocketOrigin) return
          proxy.on('proxyReqWs', (proxyRequest) => {
            proxyRequest.setHeader('origin', websocketOrigin)
          })
        },
      },
    },
  },
})
