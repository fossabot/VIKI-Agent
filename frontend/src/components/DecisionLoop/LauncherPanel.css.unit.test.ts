/**
 * LauncherPanel.css and LauncherChat.css facts pinned against the source (including the chat
 * region's own floor): jsdom applies no CSS from stylesheets, so the flex layout
 * that caps the items at 45% and lets VIKI's chat (and its own message box) take the rest, the
 * two resize handles' edge placement, and the chat region's refusal to shrink below its content,
 * can only be pinned by checking the source text.
 */
import { expect, test } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const panelCss = readCss(import.meta.url, "LauncherPanel.css");
const chatCss = readCss(import.meta.url, "LauncherChat.css");
const tokens = readCss(import.meta.url, "../../styles/tokens.css");

function customPropsIn(text: string): Set<string> {
  const names = new Set<string>();
  for (const m of text.matchAll(/(--[\w-]+)/g)) names.add(m[1] as string);
  return names;
}

test("every var(--x) LauncherPanel.css and LauncherChat.css use names a token tokens.css defines", () => {
  const defined = customPropsIn(tokens);
  for (const css of [panelCss, chatCss]) {
    const used = new Set<string>();
    for (const m of css.matchAll(/var\((--[\w-]+)\)/g)) used.add(m[1] as string);
    const missing = [...used].filter((name) => !defined.has(name));
    expect(missing).toEqual([]);
  }
});

test("the items box caps at 45% so VIKI's chat gets the rest of the panel", () => {
  const rule = ruleIn(panelCss, ".dloop-panel-body");
  expect(rule).toMatch(/flex:\s*0 1 auto/);
  expect(rule).toMatch(/max-height:\s*45%/);
});

test("the chat region grows to fill the rest and lays out as a column, so the composer stays at the foot", () => {
  const chatRegion = ruleIn(panelCss, ".dloop-panel-chat");
  expect(chatRegion).toMatch(/flex:\s*1 1 auto/);
  expect(chatRegion).toMatch(/display:\s*flex/);
  expect(chatRegion).toMatch(/flex-direction:\s*column/);
  const chat = ruleIn(panelCss, ".dloop-panel-chat > .dloop-chat");
  expect(chat).toMatch(/flex:\s*1 1 auto/);
});

test("the chat region and its own .dloop-chat keep min-height at its default auto, so neither shrinks below its content", () => {
  // Leaving out min-height: 0 here is deliberate: at the default auto, a flex item's
  // automatic minimum size comes from its content, so dloop-panel-body (which does keep
  // min-height: 0, above) gives way first, down to 0 and scrolling, before this region or
  // .dloop-chat below it gives up any of the Composer's own height. Asserting plain absence
  // rather than a specific value, since "auto" is simply never declared.
  expect(ruleIn(panelCss, ".dloop-panel-chat")).not.toMatch(/min-height/);
  expect(ruleIn(panelCss, ".dloop-panel-chat > .dloop-chat")).not.toMatch(/min-height/);
});

test(".dloop-panel scrolls itself vertically as a last resort, so Message VIKI stays reachable even if the layout above still overflows", () => {
  expect(ruleIn(panelCss, ".dloop-panel")).toMatch(/overflow-y:\s*auto/);
});

test("LauncherChat.css's message box grows with the panel instead of a fixed var() cap", () => {
  expect(chatCss).not.toMatch(/--dloop-chat-max/);
  const rule = ruleIn(chatCss, ".dloop-chat-messages");
  expect(rule).toMatch(/flex:\s*1 1 0/);
  expect(rule).toMatch(/min-height:\s*96px/);
  expect(rule).toMatch(/overflow-y:\s*auto/);
});

test("the panel's own two resize handles drop ResizeHandle.css's centring and sit flush with whichever edge grows", () => {
  expect(ruleIn(panelCss, ".dloop-panel > .resize-handle")).toMatch(/transform:\s*none/);
  expect(
    ruleIn(
      panelCss,
      '.dloop-panel[data-grow-x="left"] > .resize-handle[aria-orientation="vertical"]',
    ),
  ).toMatch(/left:\s*0/);
  expect(
    ruleIn(
      panelCss,
      '.dloop-panel[data-grow-x="right"] > .resize-handle[aria-orientation="vertical"]',
    ),
  ).toMatch(/right:\s*0/);
  expect(
    ruleIn(
      panelCss,
      '.dloop-panel[data-grow-y="up"] > .resize-handle[aria-orientation="horizontal"]',
    ),
  ).toMatch(/top:\s*0/);
  expect(
    ruleIn(
      panelCss,
      '.dloop-panel[data-grow-y="down"] > .resize-handle[aria-orientation="horizontal"]',
    ),
  ).toMatch(/bottom:\s*0/);
});
