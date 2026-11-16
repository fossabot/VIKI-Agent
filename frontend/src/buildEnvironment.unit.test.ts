// @vitest-environment node
// @ts-expect-error Node types are excluded from this browser-focused tsconfig.
import { spawnSync } from "node:child_process";
// @ts-expect-error see above
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
// @ts-expect-error see above
import { tmpdir } from "node:os";
// @ts-expect-error see above
import { join } from "node:path";
// @ts-expect-error see above
import { execPath } from "node:process";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

it("keeps local environment files and process values out of the client configuration", () => {
  const root = mkdtempSync(join(tmpdir(), "viki-build-environment-"));
  try {
    writeFileSync(
      join(root, ".env"),
      "VITE_FILE_CANARY=synthetic-file\nBROWSER_ARGS=synthetic-side-effect\n",
    );
    const vite = new URL("../node_modules/vite/dist/node/index.js", import.meta.url).href;
    const configFile = fileURLToPath(new URL("../vite.config.ts", import.meta.url));
    const script = `
      import { resolveConfig } from ${JSON.stringify(vite)};
      const config = await resolveConfig({
        configFile: ${JSON.stringify(configFile)}, root: ${JSON.stringify(root)}
      }, "build", "production", "production");
      console.log(JSON.stringify({
        env: config.env, browserArgs: process.env.BROWSER_ARGS ?? null
      }));
    `;
    const result = spawnSync(execPath, ["--input-type=module", "-e", script], {
      cwd: root,
      encoding: "utf8",
      env: { SystemRoot: process.env.SystemRoot ?? "", VITE_PROCESS_CANARY: "synthetic-process" },
    });
    expect(result.status, result.stderr).toBe(0);
    const resolved = JSON.parse(result.stdout);
    expect(resolved.env).not.toHaveProperty("VITE_FILE_CANARY");
    expect(resolved.env).not.toHaveProperty("VITE_PROCESS_CANARY");
    expect(resolved.browserArgs).toBeNull();
    expect(resolved.env).toMatchObject({ MODE: "production", DEV: false, PROD: true });
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
