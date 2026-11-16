import { defineConfig } from "vitest/config";

// End-to-end runs drive real Microsoft Edge (headless) through playwright-core.
export default defineConfig({
  test: {
    environment: "node",
    include: ["e2e/**/*.test.ts"],
    fileParallelism: false,
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
});
