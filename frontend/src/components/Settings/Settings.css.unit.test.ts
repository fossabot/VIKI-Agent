/**
 * The settings view's layout, pinned against the source. jsdom applies no CSS from stylesheets
 * and lays nothing out, so nothing else can catch a lost rule — and the Appearance, Providers,
 * Workspaces and Diagnostics pages render inside these classes without being able to import this
 * file, so the contract they build against is checked here or nowhere.
 */
import { expect, test } from "vitest";
import { NARROW_QUERY, narrowBlock, readCss, ruleIn } from "../../testing/css";

const css = readCss(import.meta.url, "Settings.css");
const SETTINGS_NARROW_QUERY = "@container (width < 656px)";
// JsonTree.css sits beside this file too (both live under Settings/) and has no unit test of its
// own; its few rules are pinned here instead of in a fourth tiny test file.
const jsonTreeCss = readCss(import.meta.url, "JsonTree.css");

test("the settings class contract exists, and is written down for the pages that render into it", () => {
  // Every class the Appearance, Providers, Workspaces and Diagnostics pages are told to use.
  for (const selector of [
    ".settings-panel",
    ".settings-page",
    ".settings-page-title",
    ".settings-row",
    ".settings-row-label",
    ".settings-row-desc",
    ".settings-row-control",
    ".settings-row-status",
    ".settings-error",
  ])
    expect(() => ruleIn(css, selector)).not.toThrow();

  // The label column is 220px, the width the contract names.
  expect(ruleIn(css, ".settings-row")).toMatch(/grid-template-columns:\s*220px/);
  // And the contract itself is quoted in the file, since the pages rendered inside the panel
  // cannot import the rules.
  expect(css).toContain("CLASS CONTRACT FOR PAGES RENDERED INSIDE THE PANEL");
  expect(css).toContain("aria-labelledby={headingId}");
});

test("the nav is a 220px column beside the panel and neither column can push the page sideways", () => {
  expect(ruleIn(css, ".settings-grid")).toMatch(/grid-template-columns:\s*220px minmax\(0, 1fr\)/);
  // `min-width: 0` on both children is what keeps a long path or model name from widening the
  // grid instead of wrapping inside it.
  expect(ruleIn(css, ".settings-nav")).toMatch(/min-width:\s*0/);
  expect(ruleIn(css, ".settings-panel")).toMatch(/min-width:\s*0/);
  expect(ruleIn(css, ".settings-panel")).toMatch(/overflow-wrap:\s*anywhere/);
  // Native input/select intrinsic widths must yield to the control column.
  const controls = ruleIn(css, ".settings-text");
  expect(controls).toMatch(/width:\s*100%/);
  expect(controls).toMatch(/min-width:\s*0/);
  expect(controls).toMatch(/max-width:\s*420px/);
});

test("the nav layout follows its accessible orientation and rows stack within a narrow panel", () => {
  expect(ruleIn(css, '.settings-grid:has(.settings-nav[aria-orientation="horizontal"])')).toMatch(
    /grid-template-columns:\s*minmax\(0, 1fr\)/,
  );
  const nav = ruleIn(css, '.settings-nav[aria-orientation="horizontal"]');
  expect(nav).toMatch(/flex-direction:\s*row/);
  expect(nav).toMatch(/flex-wrap:\s*wrap/);
  expect(nav).not.toMatch(/overflow-x/);
  const tab = ruleIn(css, '.settings-nav[aria-orientation="horizontal"] .settings-tab');
  expect(tab).toMatch(/max-width:\s*100%/);
  expect(tab).toMatch(/white-space:\s*normal/);
  expect(tab).toMatch(/overflow-wrap:\s*anywhere/);
  expect(ruleIn(css, ".settings-panel")).toMatch(/container-type:\s*inline-size/);
  const narrow = narrowBlock(css, SETTINGS_NARROW_QUERY);
  expect(ruleIn(narrow, ".settings-row")).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\)/);
  expect(narrow).not.toContain(".settings-nav");
  // The fixed review panel still follows the viewport, independently of its local container.
  expect(ruleIn(narrowBlock(css), ".settings-review")).toMatch(
    /width:\s*calc\(\(100vw - 32px\) \/ var/,
  );
  expect(css.split(NARROW_QUERY)).toHaveLength(2);
  expect(css.split(SETTINGS_NARROW_QUERY)).toHaveLength(2);
  expect(css).not.toMatch(/max-width:\s*799px/);
});

test("a refused control is dimmed through aria-disabled, never through :disabled", () => {
  // The controls stay focusable, so the styling has to key off the ARIA state.
  expect(css).toMatch(/\[aria-disabled="true"\]/);
  expect(css).not.toMatch(/:disabled/);
});

test("the review panel stays inside the window it is centred in", () => {
  const review = ruleIn(css, ".settings-review");
  expect(review).toMatch(/position:\s*fixed/);
  expect(review).toMatch(/max-width:\s*calc\(\(100vw - 32px\) \/ var/);
  expect(review).toMatch(/max-height:\s*calc\(\(100vh - 32px\) \/ var/);
  expect(review).toMatch(/overflow-y:\s*auto/);
});

test("under forced colours the selected tab draws a border, leaving the outline channel to the UA's own focus ring", () => {
  // A keyboard user tabbed onto the selected tab has to see focus move, not just the selection
  // mark that was already on screen: an outline here would otherwise replace the UA's own
  // `:focus-visible` ring, which is the only other mark this control has.
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, ".settings-tab-on")).toMatch(/border-left:\s*2px solid Highlight/);
  expect(ruleIn(forced, ".settings-tab-on")).not.toMatch(/outline/);
  expect(css.slice(css.indexOf(forced) + forced.length)).not.toContain(".settings-tab-on");
});

test("the review panel is clamped against the window, not against the zoomed page", () => {
  // Measured in Edge: a `calc(100vw - 32px)` box under `html { zoom: 1.5 }` renders 1.5x the
  // clamp, so at 400px the panel's own buttons left the window and could not be scrolled to.
  const panel = ruleIn(css, ".settings-review");
  expect(panel).toMatch(/max-width:\s*calc\(\(100vw - 32px\) \/ var\(--font-scale, 1\)\)/);
  expect(panel).toMatch(/max-height:\s*calc\(\(100vh - 32px\) \/ var\(--font-scale, 1\)\)/);
  expect(narrowBlock(css)).toMatch(/width:\s*calc\(\(100vw - 32px\) \/ var\(--font-scale, 1\)\)/);
});

// --- the shared primitives every page codes against. ---

test("the settings-page-subtitle class is gone; a bare h3 or .settings-card-title carries its old role", () => {
  expect(css).not.toMatch(/\.settings-page-subtitle/);
  expect(css).toContain(".settings-page h3,");
  const title = ruleIn(css, ".settings-card-title");
  expect(title).toMatch(/font-size:\s*14px/);
  expect(title).toMatch(/font-weight:\s*600/);
  expect(title).toMatch(/color:\s*var\(--t0\)/);
  expect(title).toMatch(/margin:\s*0/);
});

test("the settings-page base sets the type scale every page's plain text inherits", () => {
  const page = ruleIn(css, ".settings-page");
  expect(page).toMatch(/font-size:\s*13px/);
  expect(page).toMatch(/line-height:\s*1\.5/);
  expect(page).toMatch(/color:\s*var\(--t1\)/);
});

test("a card groups rows behind one border, with inline padding so a row's columns never touch it", () => {
  const card = ruleIn(css, ".settings-card");
  expect(card).toMatch(/background:\s*var\(--s0\)/);
  expect(card).toMatch(/border:\s*1px solid var\(--line\)/);
  expect(card).toMatch(/border-radius:\s*8px/);
  expect(card).toMatch(/margin-top:\s*12px/);
  expect(card).toMatch(/padding:\s*\d+px \d+px/);

  const head = ruleIn(css, ".settings-card-head");
  expect(head).toMatch(/display:\s*flex/);
  expect(head).toMatch(/flex-wrap:\s*wrap/);
  expect(head).toMatch(/align-items:\s*center/);
  expect(ruleIn(css, ".settings-card-head > .settings-actions")).toMatch(/margin-left:\s*auto/);
});

test("a card's first row drops its hairline, whether or not a card-head comes before it", () => {
  // Not ruleIn: its escaping does not cover the "+" adjacent-sibling combinator this selector
  // needs, so the match is built by hand here instead.
  expect(css).toContain(".settings-card > .settings-row:first-child,");
  expect(css).toMatch(
    /\.settings-card > \.settings-card-head \+ \.settings-row\s*\{\s*border-top:\s*none;/,
  );
});

test("a card's non-row content gets the 12px block margin a row already carries as padding", () => {
  const rule = ruleIn(css, ".settings-card > :not(.settings-row):not(.settings-card-head)");
  expect(rule).toMatch(/margin:\s*12px 0/);
});

test("settings-facts is a dl grid: wrapper divs vanish, dt and dd take the muted and the read colour", () => {
  const facts = ruleIn(css, ".settings-facts");
  expect(facts).toMatch(/display:\s*grid/);
  expect(facts).toMatch(/grid-template-columns:\s*max-content minmax\(0, 1fr\)/);
  expect(facts).toMatch(/row-gap:\s*4px/);
  expect(ruleIn(css, ".settings-facts > div")).toMatch(/display:\s*contents/);
  expect(ruleIn(css, ".settings-facts dt")).toMatch(/color:\s*var\(--t3\)/);
  const dd = ruleIn(css, ".settings-facts dd");
  expect(dd).toMatch(/margin:\s*0/);
  expect(dd).toMatch(/color:\s*var\(--t0\)/);
  expect(dd).toMatch(/min-width:\s*0/);
  expect(dd).toMatch(/overflow-wrap:\s*anywhere/);
});

test("a list inside settings-facts dd is unindented", () => {
  expect(css).toContain(".settings-facts dd ul,");
  const rule = ruleIn(css, ".settings-facts dd ol");
  expect(rule).toMatch(/margin:\s*0/);
  expect(rule).toMatch(/padding:\s*0/);
  expect(rule).toMatch(/list-style:\s*none/);
});

test("facts collapse to one column inside the same container block settings-row already uses, not a new one", () => {
  const narrow = narrowBlock(css, SETTINGS_NARROW_QUERY);
  expect(ruleIn(narrow, ".settings-facts")).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\)/);
});

test("the narrow-layout block gives .settings-facts grid-template-columns: minmax(0, 1fr) so hosts without a container collapse too", () => {
  const adr011 = narrowBlock(css, "@media not all and (min-width: 800px)");
  expect(ruleIn(adr011, ".settings-facts")).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\)/);
});

test("a settings-pill is a dot plus a word, 12px, with a round border a forced palette keeps", () => {
  const pill = ruleIn(css, ".settings-pill");
  expect(pill).toMatch(/font-size:\s*12px/);
  expect(pill).toMatch(/border-radius:\s*999px/);
  expect(pill).toMatch(/border:\s*1px solid var\(--b-strong\)/);
  const dot = ruleIn(css, ".settings-pill::before");
  expect(dot).toMatch(/border:\s*1px solid currentColor/);
  expect(dot).toMatch(/border-radius:\s*50%/);
});

test("settings-pill tones change the dot's colour, and the error tone its shape too, not colour alone", () => {
  expect(ruleIn(css, '.settings-pill[data-tone="ready"]')).toMatch(/border-color:\s*var\(--or\)/);
  expect(ruleIn(css, '.settings-pill[data-tone="ready"]::before')).toMatch(
    /background:\s*var\(--or\)/,
  );
  expect(ruleIn(css, '.settings-pill[data-tone="attention"]')).toMatch(/box-shadow/);
  const errorPill = ruleIn(css, '.settings-pill[data-tone="error"]');
  expect(errorPill).toMatch(/color:\s*var\(--err\)/);
  const errorDot = ruleIn(css, '.settings-pill[data-tone="error"]::before');
  expect(errorDot).toMatch(/border-radius:\s*0/);
  expect(errorDot).toMatch(/background:\s*var\(--err\)/);
});

test("settings-pill works on a button as well as a span", () => {
  expect(ruleIn(css, "button.settings-pill")).toMatch(/cursor:\s*pointer/);
});

test("settings-actions wraps its buttons with an 8px gap, and settings-note is quiet 12px text", () => {
  const actions = ruleIn(css, ".settings-actions");
  expect(actions).toMatch(/display:\s*flex/);
  expect(actions).toMatch(/flex-wrap:\s*wrap/);
  expect(actions).toMatch(/gap:\s*8px/);
  const note = ruleIn(css, ".settings-note");
  expect(note).toMatch(/font-size:\s*12px/);
  expect(note).toMatch(/color:\s*var\(--t3\)/);
  expect(note).toMatch(/margin:\s*0/);
});

test("settings-row-meta is the one small text a row's meta line uses", () => {
  const meta = ruleIn(css, ".settings-row-meta");
  expect(meta).toMatch(/font-size:\s*12px/);
  expect(meta).toMatch(/color:\s*var\(--t3\)/);
});

test("the row Reset button is a small, 24px-high .btn.txt.sm", () => {
  const reset = ruleIn(css, ".btn.sm.settings-reset");
  expect(reset).toMatch(/height:\s*24px/);
  expect(reset).toMatch(/font-size:\s*12px/);
});

test("a refused control is scoped to the whole page and never dims a pill, which is a status", () => {
  const rule = ruleIn(css, '.settings-page [aria-disabled="true"]:not(.settings-pill)');
  expect(rule).toMatch(/opacity:\s*0\.5/);
  expect(css).not.toContain(".settings-row [aria-disabled");
  expect(css).not.toContain(".settings-readonly ~");
});

test("the back row (App.tsx's renderBack div) wraps with a centred 8px gap, its reason at 12px", () => {
  const row = ruleIn(css, ".settings-header > div:first-child");
  expect(row).toMatch(/display:\s*flex/);
  expect(row).toMatch(/flex-wrap:\s*wrap/);
  expect(row).toMatch(/align-items:\s*center/);
  expect(row).toMatch(/gap:\s*8px/);
  const reason = ruleIn(css, ".settings-header > div:first-child > span");
  expect(reason).toMatch(/font-size:\s*12px/);
  expect(reason).toMatch(/color:\s*var\(--t3\)/);
});

test("the page head under the back row takes no bottom margin, so the header gap is its only space", () => {
  expect(ruleIn(css, ".settings-header > .page-head")).toMatch(/margin-bottom:\s*0/);
});

test("a bare button in the row control column keeps its natural width, not the column's stretch", () => {
  // .settings-row-control is a flex column (stretch by default); an input or select already sets
  // its own width, so stretch is moot for them, but a bare .btn has none and would otherwise fill
  // the column, e.g. General's Setup card "Show first-run setup on next launch" button.
  const rule = ruleIn(css, ".settings-row-control > .btn");
  expect(rule).toMatch(/align-self:\s*flex-start/);
});

test("a page-level settings-actions bar gets a card's 12px top margin only when it follows one", () => {
  // Appearance's Apply/Cancel/Restore bar follows a .settings-card and wants the same separation
  // cards keep from each other; the sibling combinator leaves the Workspaces toolbar alone, since
  // that one follows the page h2 with no card before it.
  const rule = ruleIn(css, ".settings-card ~ .settings-actions");
  expect(rule).toMatch(/margin-top:\s*12px/);
});

test("JsonTree's map grid uses a 4px row gap so a deep report does not run tall, keeping a 12px column gap", () => {
  const rule = ruleIn(jsonTreeCss, ".json-tree-map");
  expect(rule).toMatch(/gap:\s*4px 12px/);
});

test("a JsonTree group's key keeps a little extra space above it, so groups still read apart", () => {
  const rule = ruleIn(jsonTreeCss, ".json-tree-group > .json-tree-key");
  expect(rule).toMatch(/margin-top:\s*2px/);
});

test("JsonTree list items are spaced 4px apart", () => {
  expect(jsonTreeCss).toMatch(/\.json-tree-list\s*>\s*li\s*\+\s*li\s*\{\s*margin-top:\s*4px/);
});
