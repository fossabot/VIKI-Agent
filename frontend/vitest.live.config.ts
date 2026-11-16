import { defineConfig } from "vitest/config";

// The live smoke run (e2e/*.live.ts): real Edge against the real server, using the claude.exe,
// vault and ~/.claude of whoever runs it. It spends subscription quota and leaves a real
// transcript behind, so it has its own config and its own npm script and is never part of
// scripts/check.py.
export default defineConfig({
  test: {
    environment: "node",
    include: ["e2e/**/*.live.ts"],
    fileParallelism: false,
    testTimeout: 180_000,
    hookTimeout: 120_000,
  },
});
