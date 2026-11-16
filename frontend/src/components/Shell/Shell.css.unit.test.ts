/**
 * Shell.css's containment of untrusted text, pinned against the source: jsdom applies no CSS
 * from stylesheets and lays nothing out, so no render test can catch a column that overflows.
 */
import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Shell.css");
const conversation = readCss(import.meta.url, "../Conversation/Conversation.css");

test("the wrapping rule sits on the one ancestor the transcript and the cards share", () => {
  // Conversation.css sets the same rule, but only on `.conversation-list`; App renders the
  // permission, question, blocked and error cards as siblings of it, so nothing wrapped their
  // text — and a question card's fields come off the wire uncut. `.message-column` is the
  // element Shell puts around all of it, and overflow-wrap inherits.
  expect(ruleIn(css, ".message-column")).toMatch(/overflow-wrap:\s*anywhere/);
  expect(ruleIn(conversation, ".conversation-list")).toMatch(/overflow-wrap:\s*anywhere/);
});

test("a rail drawer has a positioning host that stops at the top bar and the status bar", () => {
  // Absolute inside `.shell-body`: the drawer covers the conversation and the docked rail, and
  // the two bars stay visible (and inert) above and below it.
  expect(ruleIn(css, ".shell-body")).toMatch(/position:\s*relative/);
});

test("the center owns the remaining width and a hidden chat pane cannot retain flex space", () => {
  const center = ruleIn(css, ".shell-center");
  expect(center).toMatch(/flex:\s*1/);
  expect(center).toMatch(/min-width:\s*0/);
  expect(center).toMatch(/min-height:\s*0/);
  expect(center).toMatch(/overflow:\s*hidden/);
  expect(ruleIn(css, ".conversation[hidden]")).toMatch(/display:\s*none/);
});

test("a multi-pane tree does not scroll itself, and each pane shares the remaining space", () => {
  // Each pane's own `.conversation-scroll` does the scrolling; the outer wrapper hiding overflow
  // keeps the two from fighting over a scrollbar.
  expect(ruleIn(css, ".conversation-panes")).toMatch(/overflow:\s*hidden/);
  const pane = ruleIn(css, ".conversation.pane");
  expect(pane).toMatch(/flex:\s*1/);
  expect(pane).toMatch(/min-width:\s*0/);
  expect(pane).toMatch(/min-height:\s*0/);
  expect(ruleIn(css, ".pane[data-focused] > .conversation-header")).toMatch(
    /box-shadow:\s*inset 0 2px 0 var\(--or\)/,
  );
});

test("message gutters use 24px by default and come down to 16px below 800px", () => {
  const narrow = narrowBlock(css);
  expect(ruleIn(narrow, ".conversation-header")).toMatch(/padding:\s*0 16px/);
  expect(ruleIn(narrow, ".conversation-messages")).toMatch(/padding:\s*16px 16px 0/);
  // The design's own gutters are untouched at every width the frozen e2e measures (1440/1279/900).
  expect(ruleIn(css, ".conversation-header")).toMatch(/padding:\s*0 28px/);
  expect(ruleIn(css, ".conversation-messages")).toMatch(/padding:\s*24px 24px 0/);
});

test("the scroll container is the containing block for absolutely positioned content", () => {
  // Screen-reader-only labels such as the voice chip are absolutely positioned children
  // of transcript elements inside .conversation-scroll. Without position: relative they
  // position against .conversation and escape the scroller's clipping, stretching the pane.
  expect(ruleIn(css, ".conversation-scroll")).toMatch(/position:\s*relative/);
});

test("the view region scrolls itself, vertically only, and takes the narrow gutter", () => {
  // The narrow rule for every view: 24px gutters docked, 16px narrow, and no horizontal
  // page scroll at any width — a wide table scrolls inside its own box instead.
  expect(ruleIn(css, ".app-view")).toMatch(/overflow-y:\s*auto/);
  expect(ruleIn(css, ".app-view")).toMatch(/overflow-x:\s*hidden/);
  expect(ruleIn(css, ".app-view")).toMatch(/padding:\s*24px/);
  // Without this the status bar is pushed off the window by a long settings page: a flex item's
  // default min-height is its content.
  expect(ruleIn(css, ".app-view")).toMatch(/min-height:\s*0/);
  expect(ruleIn(narrowBlock(css), ".app-view")).toMatch(/padding:\s*16px/);
});
