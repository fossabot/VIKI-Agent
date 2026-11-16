/** Real Edge HTTP cache, then the real scratch backend and the built UI; no route mocks. */
// This test's Node HTTP server is outside the shared harness's small node.d.ts contract.
import {
  createServer,
  type Server as HttpServer,
  type IncomingMessage,
  type ServerResponse,
  // @ts-expect-error test-local Node API
} from "node:http";
import type { Browser } from "playwright-core";
import { afterAll, beforeAll, expect, test } from "vitest";
import { appUrl, launchEdge, waitForText } from "../../harness";
import { PORT, type Server, startServer } from "../../server";
import { navigateSidebar } from "../../sidebar-navigation";

let browser: Browser | undefined;
let legacy: HttpServer | undefined;
let server: Server | undefined;

beforeAll(async () => {
  browser = await launchEdge();
});

afterAll(async () => {
  await browser?.close();
  if (legacy) await closeLegacy(legacy);
  await server?.stop();
});

function closeLegacy(old: HttpServer): Promise<void> {
  return new Promise((resolve, reject) => {
    old.close((error?: Error) => (error ? reject(error) : resolve()));
    old.closeAllConnections();
  });
}

test("a native launch bypasses cached old HTML and ordinary reload fetches the current UI", async () => {
  if (!browser) throw new Error("Edge did not launch");
  let oldRequests = 0;
  legacy = createServer((request: IncomingMessage, response: ServerResponse) => {
    if (request.url !== "/") {
      response.writeHead(404, { "Cache-Control": "no-store" });
      response.end();
      return;
    }
    oldRequests++;
    response.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "public, max-age=31536000",
    });
    response.end('<!doctype html><h1 id="old-shell">Synthetic old UI</h1>');
  });
  await new Promise<void>((resolve, reject) => {
    legacy.once("error", reject);
    legacy.listen(PORT, "127.0.0.1", resolve);
  });

  // Keep the same browser context, with its HTTP cache enabled, across the server upgrade.
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const oldPage = await context.newPage();
  await oldPage.goto(appUrl(PORT, "synthetic-old-token"));
  expect(await oldPage.locator("#old-shell").textContent()).toBe("Synthetic old UI");
  await oldPage.close();
  const page = await context.newPage();
  await page.goto(appUrl(PORT));
  expect(await page.locator("#old-shell").textContent()).toBe("Synthetic old UI");
  expect(oldRequests).toBe(1);

  await closeLegacy(legacy);
  legacy = undefined;
  server = await startServer({});

  // A changed fragment does not change the cache key: this old shell still survives the upgrade.
  await page.goto("about:blank");
  await page.goto(appUrl(PORT));
  expect(await page.locator("#old-shell").textContent()).toBe("Synthetic old UI");

  const assetPolicies: Promise<string | null>[] = [];
  page.on("response", (asset) => {
    if (new URL(asset.url()).pathname.startsWith("/assets/")) {
      assetPolicies.push(asset.headerValue("cache-control"));
    }
  });
  const launched = new URL(appUrl(PORT));
  launched.searchParams.set("ui", crypto.randomUUID().replaceAll("-", ""));
  const response = await page.goto(launched.href);
  expect(response?.status()).toBe(200);
  expect(await response?.headerValue("cache-control")).toBe("no-store");
  await page.locator('[data-region="shell"]').waitFor();
  await waitForText(page, ".empty-wordmark", "VIKIAGENT");
  const sidebar = page.getByRole("navigation", { name: "Navigation and sessions" });
  expect(await sidebar.getByRole("button", { name: "New session", exact: true }).isVisible()).toBe(
    true,
  );
  await navigateSidebar(page, "Capabilities");
  const hub = page.getByRole("region", { name: "Capabilities", exact: true });
  await hub.waitFor();
  for (const name of ["Skills", "Tools", "Connectors", "Plugins"]) {
    expect(await hub.getByRole("tab", { name, exact: true }).isVisible()).toBe(true);
  }

  const reloaded = await page.reload();
  expect(reloaded?.status()).toBe(200);
  expect(await reloaded?.headerValue("cache-control")).toBe("no-store");
  await page.locator('[data-region="shell"]').waitFor();
  await navigateSidebar(page, "Chat");
  await waitForText(page, ".empty-wordmark", "VIKIAGENT");
  const transfers = await page.evaluate(() => ({
    document: (performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming)
      .transferSize,
    assets: performance
      .getEntriesByType("resource")
      .filter((entry) => new URL(entry.name).pathname.startsWith("/assets/"))
      .map((entry) => (entry as PerformanceResourceTiming).transferSize),
  }));
  expect(transfers.document).toBeGreaterThan(0);
  expect(transfers.assets.length).toBeGreaterThan(0);
  expect(transfers.assets.every((bytes) => bytes > 0)).toBe(true);
  expect(assetPolicies.length).toBeGreaterThan(0);
  for (const policy of await Promise.all(assetPolicies)) expect(policy).toBe("no-store");
  await context.close();
});
