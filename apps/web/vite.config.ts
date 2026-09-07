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
    // Multi-step editor flows include a bounded 5s permission wait plus user
    // interactions; the enclosing test must allow both on a shared runner.
    testTimeout: 15_000,
  },
});
