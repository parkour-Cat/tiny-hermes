import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    // Multi-step editor flows include a bounded permission wait plus user
    // interactions; the enclosing test must allow both on a shared runner, and
    // outlast the waits inside it so a failure reports what was missing. Under
    // CPU load one such flow spent 6.7s before its 15s wait began (2026-10-01).
    testTimeout: 30_000,
  },
});
