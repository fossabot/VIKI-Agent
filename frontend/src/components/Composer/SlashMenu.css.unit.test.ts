/**
 * Where the slash menu sits in the stacking order. jsdom applies no CSS from stylesheets and
 * paints nothing, so neither a render nor an acceptance test can see a layering bug: this reads
 * the three sources and compares the numbers.
 *
 * The menu set no z-index at all, so `.jump-pill` — opaque, sticky at the bottom of the scroll
 * box, and on screen exactly when the user has scrolled up — painted over the menu's last row
 * and its hint footer, and took the clicks aimed at them.
 */
import { expect, test } from "vitest";
import { readCss, ruleIn } from "../../testing/css";

const menu = readCss(import.meta.url, "SlashMenu.css");
const conversation = readCss(import.meta.url, "../Conversation/Conversation.css");
const toast = readCss(import.meta.url, "../Toast/SessionLimitToast.css");

function zIndex(css: string, selector: string): number {
  const declared = ruleIn(css, selector).match(/z-index:\s*(\d+)/);
  if (!declared) throw new Error(`no z-index in the ${selector} rule`);
  return Number(declared[1]);
}

test("the open menu paints above the jump-to-latest pill", () => {
  expect(zIndex(menu, ".menu")).toBeGreaterThan(zIndex(conversation, ".jump-pill"));
});

test("and stays under the session-limit toast, which has to reach the user over anything", () => {
  expect(zIndex(menu, ".menu")).toBeLessThan(zIndex(toast, ".toast"));
});
