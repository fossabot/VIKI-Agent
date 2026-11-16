import type { Browser } from "playwright-core";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterAll, beforeAll, expect, test } from "vitest";
import { ContextSummary } from "../src/components/RightRail/ContextSummary";
import { readCss } from "../src/testing/css";
import { launchEdge } from "./harness";

const css = ["styles/tokens.css", "styles/base.css", "components/RightRail/ContextSummary.css"]
  .map((file) => readCss(import.meta.url, `../src/${file}`).replace(/^@import[^;]+;/gm, ""))
  .join("\n");

let browser: Browser;
beforeAll(async () => {
  browser = await launchEdge();
});
afterAll(async () => {
  await browser?.close();
});

// Actual component markup and CSS, without an app server, provider, or screenshots.
test.each([
  ["dark", "none"],
  ["light", "none"],
  ["dark", "active"],
  ["light", "active"],
] as const)("context fill renders in %s with forced colors %s", async (theme, forcedColors) => {
  const page = await browser.newPage();
  try {
    await page.emulateMedia({ forcedColors });
    await page.setContent(
      `<html data-theme="${theme}"><style>${css}</style><body><main></main></body></html>`,
    );
    for (const width of [240, 560]) {
      await page.locator("main").evaluate((node, width) => {
        node.style.width = `${width}px`;
      }, width);
      for (const percent of [0, 5, 30, 75, 20, 100, 0]) {
        const markup = renderToStaticMarkup(createElement(ContextSummary, { percent }));
        await page.locator("main").evaluate((node, markup) => {
          node.innerHTML = markup;
        }, markup);
        const meter = page.getByRole("meter", { name: "Context window used" });
        expect(await meter.locator(".context-summary-fill").count()).toBe(1);
        const measured = await meter.evaluate((track) => {
          const fill = track.firstElementChild as HTMLElement;
          const rect = track.getBoundingClientRect();
          const filled = fill.getBoundingClientRect();
          const style = getComputedStyle(track);
          return {
            width: rect.width,
            height: rect.height,
            fillWidth: filled.width,
            fillHeight: filled.height,
            trackColor: style.backgroundColor,
            fillColor: getComputedStyle(fill).backgroundColor,
            outlineStyle: style.outlineStyle,
            outlineColor: style.outlineColor,
          };
        });
        expect(measured.width).toBe(width);
        expect(measured.height).toBe(5);
        expect(measured.fillHeight).toBe(5);
        expect(measured.fillWidth).toBeCloseTo((percent / 100) * width, 1);
        expect(measured.fillColor).not.toBe(measured.trackColor);
        if (forcedColors === "none") {
          expect(measured.fillColor).toBe(theme === "dark" ? "rgb(248, 248, 255)" : "rgb(0, 0, 0)");
        } else {
          expect(measured.outlineStyle).toBe("solid");
          expect(measured.outlineColor).not.toBe(measured.trackColor);
        }
      }
    }
  } finally {
    await page.close();
  }
});
