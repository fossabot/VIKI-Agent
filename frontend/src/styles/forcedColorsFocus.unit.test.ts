import { expect, test } from "vitest";
import { narrowBlock, readCss, ruleIn } from "../testing/css";

// Under forced colours a focus ring drawn with a shadow or a background vanishes, so each of these
// controls takes a 2px Highlight outline. jsdom applies no stylesheet, so the source is pinned.
// With the brace: Composer.css also names the query in a comment.
const FORCED = "@media (forced-colors: active) {";
const FOCUS_RULES: [string, string, string][] = [
  ["./base.css", ".btn:focus-visible", "button"],
  ["../components/Composer/Composer.css", ".composer-box:focus-visible", "composer"],
  ["../components/Conversation/Conversation.css", ".jump-pill:focus-visible", "jump pill"],
  [
    "../components/Header/HeaderMenu.css",
    ".header-menu .mi:focus-visible:not(:disabled)",
    "menu item",
  ],
  ["../components/Header/HeaderMenu.css", ".header-menu-button:focus-visible", "menu button"],
  ["../components/LeftRail/rowState.css", ".sess .m .sess-menu:focus-visible", "row menu"],
  ["../components/Onboarding/Onboarding.css", ".onboarding-card:focus-visible", "onboarding card"],
  ["../components/Search/SearchPalette.css", ".palette-input:focus", "palette input"],
  ["../components/Kanban/Kanban.css", ".kanban-card:focus-within", "Kanban card"],
  ["../components/VaultBrowser/NoteCard.css", ".note-card:has(:focus-visible)", "vault note card"],
];

test.each(FOCUS_RULES)(
  "%s: %s takes a 2px Highlight outline under forced colours (%s)",
  (sheet, selector) => {
    const forced = narrowBlock(readCss(import.meta.url, sheet), FORCED);
    expect(ruleIn(forced, selector)).toMatch(/outline:\s*2px solid Highlight/);
  },
);

/** The body of every rule whose comma-separated selector list names `selector` exactly. */
function bodiesNaming(css: string, selector: string): string[] {
  const bodies: string[] = [];
  for (const [, list = "", body = ""] of css
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const names = list.split(",").map((name) => name.trim().replace(/\s+/g, " "));
    if (names.includes(selector)) bodies.push(body);
  }
  return bodies;
}

// The right rail's rings are pinned in both modes, a 2px accent outline normally and 2px Highlight
// under forced colours, and a grouped rule counts for each selector it names.
const RAIL_RINGS: [string, string, string][] = [
  ["../components/RightRail/TeamStrip.css", ".rail-team-member:focus-visible", "team member"],
  ["../components/RightRail/NeedsYou.css", ".need-open:focus-visible", "needs you"],
  ["../components/RightRail/RunStop.css", ".run-stop:focus-visible", "run stop"],
  ["../components/RightRail/RunStop.css", ".run-stop-send:focus-visible", "run stop send"],
  ["../components/RightRail/RunStop.css", ".run-stop-keep:focus-visible", "run stop keep"],
  ["../components/RightRail/RunStop.css", ".run-stop-retry:focus-visible", "run stop retry"],
  ["../components/RightRail/RunStop.css", ".run-stop-dismiss:focus-visible", "run stop dismiss"],
];

test.each(RAIL_RINGS)(
  "%s: %s takes a 2px accent outline outside forced colours (%s)",
  (sheet, selector) => {
    const source = readCss(import.meta.url, sheet);
    const normal = source.replace(narrowBlock(source, FORCED), "");
    expect(
      bodiesNaming(normal, selector).some((body) => /outline:\s*2px solid var\(--or\)/.test(body)),
    ).toBe(true);
  },
);

test.each(RAIL_RINGS)(
  "%s: %s takes a 2px Highlight outline under forced colours (%s)",
  (sheet, selector) => {
    const forced = narrowBlock(readCss(import.meta.url, sheet), FORCED);
    expect(
      bodiesNaming(forced, selector).some((body) => /outline:\s*2px solid Highlight/.test(body)),
    ).toBe(true);
  },
);
