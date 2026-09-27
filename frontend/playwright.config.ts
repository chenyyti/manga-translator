import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from '@playwright/test'

const backendDir = fileURLToPath(new URL('../backend', import.meta.url))
const e2eDataDir = fileURLToPath(new URL('../.e2e-data', import.meta.url))
const projectPython = process.env.MANGA_TRANSLATOR_PYTHON || 'python'
const backendPort = Number(process.env.MANGA_TRANSLATOR_E2E_BACKEND_PORT ?? 8000)
const frontendPort = Number(process.env.MANGA_TRANSLATOR_E2E_FRONTEND_PORT ?? 5173)
const backendOrigin = `http://127.0.0.1:${backendPort}`
const reuseExistingServers = process.env.MANGA_TRANSLATOR_E2E_REUSE_SERVERS === '1'

export default defineConfig({
  testDir: './e2e',
  // The local E2E backend intentionally uses one persistent SQLite workspace
  // so each test exercises the same recovery and asset paths.  Serializing
  // browser workers prevents concurrent fixtures from racing global settings
  // and task queues; individual tests still cover the asynchronous flows.
  workers: 1,
  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    channel: 'chrome',
  },
  webServer: [
    {
      command: `"${projectPython}" -m uvicorn tests.e2e_app:app --host 127.0.0.1 --port ${backendPort}`,
      cwd: backendDir,
      env: { MANGA_TRANSLATOR_DATA_DIR: e2eDataDir },
      port: backendPort,
      reuseExistingServer: reuseExistingServers,
    },
    {
      // The runner avoids Vite's temporary config bundle under node_modules,
      // which is read-only in the packaged Windows test environment.
      command: `node .\\node_modules\\vite\\bin\\vite.js --host 127.0.0.1 --port ${frontendPort} --configLoader runner`,
      env: {
        VITE_API_TARGET: backendOrigin,
        VITE_DEV_SERVER_PORT: String(frontendPort),
        // Production keeps a strict WebSocket origin allowlist. The local
        // proxy presents its standard trusted origin when E2E uses a spare
        // frontend port to avoid colliding with a running desktop instance.
        VITE_WEBSOCKET_ORIGIN: 'http://127.0.0.1:5173',
      },
      port: frontendPort,
      reuseExistingServer: reuseExistingServers,
    },
  ],
})
