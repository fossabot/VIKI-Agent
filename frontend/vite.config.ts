import react from "@vitejs/plugin-react";
import { defaultClientConditions } from "vite";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  // Runtime settings come from the authenticated server, not the build machine's environment.
  envDir: false,
  envPrefix: [],
  // The `worker` condition first: without it, a worker bundle resolves the entity decoder's
  // browser build, which calls document.createElement at load and crashes off the UI thread.
  resolve: { conditions: ["worker", ...defaultClientConditions] },
  worker: { format: "es" },
  // Dev only: the Python server allows this origin when started with --dev (token still required).
  // changeOrigin sets the Host header to 127.0.0.1:8765, which the server insists on; the Origin
  // header passes through unchanged (never rewriteWsOrigin).
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8765", ws: true, changeOrigin: true },
    },
  },
  // assetsInlineLimit: 0 — the 24 IBM Plex Sans JP faces must stay real files under
  // dist/assets/*.woff2, never inlined as data: URIs (the server's CSP is font-src 'self').
  build: { outDir: "dist", emptyOutDir: true, assetsInlineLimit: 0 },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/testBrowser.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
