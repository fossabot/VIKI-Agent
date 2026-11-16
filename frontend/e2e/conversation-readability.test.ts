// @vitest-environment jsdom
import { render } from "@testing-library/react";
import type { Browser } from "playwright-core";
import { createElement } from "react";
import { afterAll, beforeAll, expect, test } from "vitest";
import { Reply, UserTurn } from "../src/components/Conversation/Conversation";
import type { Turn } from "../src/state/view";
import { readCss } from "../src/testing/css";
import { launchEdge } from "./harness";

const css = ["styles/tokens.css", "styles/base.css", "components/Conversation/Conversation.css"]
  .map((file) => readCss(import.meta.url, `../src/${file}`).replace(/^@import[^;]+;/gm, ""))
  .join("\n");
const user = { text: `Please inspect ${"x".repeat(360)}`, command: "", at: "2026-01-01T00:00:00Z" };
const turn: Turn = {
  id: "synthetic",
  user,
  blocks: [
    { kind: "thinking", id: "thought", text: "Check the example", done: true, durationMs: 1000 },
    {
      kind: "tools",
      id: "tools",
      rows: [false, true].map((isError, index) => ({
        id: `tool-${index}`,
        tool: "Read",
        row: { head: "Read", rest: "example.md", tail: "" },
        done: true,
        isError,
        summary: "Synthetic result",
        preview: "Synthetic result",
      })),
    },
    { kind: "notice", id: "notice", tone: "hook", subject: "Check", detail: "Synthetic notice" },
    {
      kind: "text",
      id: "prose",
      done: true,
      text:
        "A readable reply with [a reference](https://example.com) and `example.md`.\n\n" +
        "> A quoted passage.\n\n| Item | Result |\n| --- | --- |\n| Sample | Ready |\n\n" +
        `\`\`\`\n${"long-code".repeat(60)}\n\`\`\``,
    },
  ],
  status: "success",
  usage: null,
  durationMs: 1000,
  history: false,
};
const rendered = render(
  createElement(
    "main",
    { className: "conversation-list", style: { background: "var(--g1)" } },
    createElement(
      "div",
      { className: "turn" },
      createElement(UserTurn, { user, nowMs: Date.parse("2026-01-01T00:00:00Z") }),
      createElement(Reply, { turn, onOpenUrl: () => {} }),
    ),
  ),
);
const markup = rendered.container.innerHTML;
rendered.unmount();

let browser: Browser;
beforeAll(async () => {
  browser = await launchEdge();
});
afterAll(async () => {
  await browser?.close();
});

// Rendered component markup lets the browser measure the theme's composited text contrast.
test.each([
  ["dark", "no-preference", "none"],
  ["light", "no-preference", "none"],
  ["dark", "more", "none"],
  ["light", "more", "none"],
  ["dark", "no-preference", "active"],
  ["light", "no-preference", "active"],
] as const)(
  "chat stays readable in %s, contrast %s, forced colors %s",
  async (theme, contrast, forcedColors) => {
    const page = await browser.newPage();
    try {
      await page.emulateMedia({ contrast, forcedColors });
      await page.setContent(
        `<html data-theme="${theme}"><style>${css}</style><body>${markup}</body></html>`,
      );
      for (const width of [320, 720]) {
        await page.setViewportSize({ width, height: 1000 });
        const selectors = [
          ".user-turn",
          ".user-at",
          ".reply-name",
          ".thought-label",
          ".tool-head",
          ".tool-error-label",
          ".notice-row",
          ".md > p",
          ".md a",
          ".md-inline-code",
          ".md-code code",
          ".md th",
        ];
        const measure = async () =>
          page.evaluate((selectors) => {
            const canvas = document.createElement("canvas");
            canvas.width = canvas.height = 1;
            const context = canvas.getContext("2d");
            if (!context) throw new Error("Canvas context unavailable");
            const rgba = (color: string) => {
              context.clearRect(0, 0, 1, 1);
              context.fillStyle = color;
              context.fillRect(0, 0, 1, 1);
              return [...context.getImageData(0, 0, 1, 1).data];
            };
            const composite = (front: number[], back: number[]) =>
              front
                .slice(0, 3)
                .map(
                  (value, index) =>
                    value * ((front[3] ?? 255) / 255) +
                    (back[index] ?? 0) * (1 - (front[3] ?? 255) / 255),
                );
            const luminance = (rgb: number[]) =>
              rgb
                .map((value) => {
                  const channel = value / 255;
                  return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
                })
                .reduce(
                  (total, channel, index) =>
                    total + channel * ([0.2126, 0.7152, 0.0722][index] ?? 0),
                  0,
                );
            return selectors.map((selector) => {
              const node = document.querySelector(selector);
              if (!node) throw new Error(`Missing ${selector}`);
              const ancestors: Element[] = [];
              for (let current: Element | null = node; current; current = current.parentElement)
                ancestors.unshift(current);
              const background = ancestors.reduce(
                (back, element) => composite(rgba(getComputedStyle(element).backgroundColor), back),
                [255, 255, 255],
              );
              const color = rgba(getComputedStyle(node).color);
              const foreground = luminance(composite(color, background));
              const under = luminance(background);
              return {
                selector,
                color: color.slice(0, 3),
                ratio: (Math.max(foreground, under) + 0.05) / (Math.min(foreground, under) + 0.05),
              };
            });
          }, selectors);
        const samples = await measure();
        for (const sample of samples)
          expect(sample.ratio, `${sample.selector} at ${width}px`).toBeGreaterThanOrEqual(4.5);
        const prose = samples.find((sample) => sample.selector === ".md > p");
        expect(new Set(prose?.color).size).toBe(1);
        if (forcedColors === "none") {
          const roles = [
            ".reply-name",
            ".thought-label",
            ".tool-head",
            ".tool-error-label",
            ".md a",
          ];
          const roleColors = samples.filter((sample) => roles.includes(sample.selector));
          expect(new Set(roleColors.map((sample) => sample.color.join())).size).toBe(roles.length);
          for (const sample of roleColors)
            expect(new Set(sample.color).size, sample.selector).toBeGreaterThan(1);
        }
        await page.locator(".tool-row").first().hover();
        for (const sample of await measure())
          expect(sample.ratio, `hover ${sample.selector}`).toBeGreaterThanOrEqual(4.5);
        await page.locator(".thought").focus();
        expect(
          await page.locator(".thought").evaluate((node) => getComputedStyle(node).outlineStyle),
        ).toBe("solid");
        expect(await page.locator(".thought").getAttribute("aria-expanded")).toBe("false");
        expect(await page.locator(".tool-error-label").textContent()).toBe("Error");
        expect(
          await page.locator(".md a").evaluate((node) => getComputedStyle(node).textDecorationLine),
        ).toContain("underline");
        expect(
          await page
            .locator(".user-turn")
            .evaluate((node) => Number.parseFloat(getComputedStyle(node).borderInlineStartWidth)),
        ).toBeGreaterThanOrEqual(3);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
          width,
        );
      }
    } finally {
      await page.close();
    }
  },
);
