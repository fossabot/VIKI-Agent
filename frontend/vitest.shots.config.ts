import { defineConfig } from "vitest/config";

// Screenshot runs for a person to look at (e2e/*.shots.ts): real Edge. layout.shots.ts and
// screenshots.shots.ts run against the Vite dev server with the REST routes mocked in the page;
// app.shots.ts needs a real conversation, so it runs against the real server and `frontend/dist`
// instead (its own header explains why, and it checks the served bundle is current). Never part
// of scripts/check.py.
export default defineConfig({
  test: {
    environment: "node",
    include: ["e2e/**/*.shots.ts"],
    fileParallelism: false,
    testTimeout: 120_000,
    hookTimeout: 60_000,
  },
});
