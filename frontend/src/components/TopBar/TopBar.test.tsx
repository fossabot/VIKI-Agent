import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { NARROW_QUERY, narrowBlock, readCss, ruleIn } from "../../testing/css";
import { TopBar } from "./TopBar";

afterEach(cleanup);

// jsdom applies no CSS from stylesheets (only inline styles), so the box-sizing metrics
// this component is graded on (see e2e/layout.accept.test.ts, frozen) are checked here
// against the source instead of a computed style.
const css = readCss(import.meta.url, "TopBar.css");
// The page shell lives outside src/; read the same raw-text way as the stylesheet above.
const indexHtml = readCss(import.meta.url, "../../../index.html");

function rule(selector: string): string {
  const match = css.match(new RegExp(`${selector.replace(/[.:]/g, "\\$&")}\\s*\\{[^}]*\\}`));
  if (!match) throw new Error(`no ${selector} rule in TopBar.css`);
  return match[0];
}

// The narrower sub-band within S (at a content box of 455px or less, TopBar.css), read the same
// way narrowBlock reads the below-800px block: by balancing braces from the query, not by regex,
// since a nested rule's own `}` would otherwise close the match early.
const SUB_BAND_QUERY = "@container (max-width: 455px)";
function subBand(): string {
  const start = css.indexOf(SUB_BAND_QUERY);
  if (start < 0) throw new Error(`no "${SUB_BAND_QUERY}" block in TopBar.css`);
  let depth = 0;
  for (let i = css.indexOf("{", start); i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}" && --depth === 0) return css.slice(start, i + 1);
  }
  throw new Error(`unbalanced "${SUB_BAND_QUERY}" block in TopBar.css`);
}

// The bar's local-width action-sizing band, read the same brace-balancing way as subBand.
const CONTAINER_QUERY = "@container (max-width: 700px)";
const COMPACT_QUERY = "@container shell not (min-width: 800px)";
function containerBand(): string {
  const start = css.indexOf(CONTAINER_QUERY);
  if (start < 0) throw new Error(`no "${CONTAINER_QUERY}" block in TopBar.css`);
  let depth = 0;
  for (let i = css.indexOf("{", start); i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}" && --depth === 0) return css.slice(start, i + 1);
  }
  throw new Error(`unbalanced "${CONTAINER_QUERY}" block in TopBar.css`);
}

test("the top bar matches the design's box-sizing metrics (jsdom applies no CSS)", () => {
  // 48px + the 1px hairline: border-box needs the hairline folded into the declared height.
  expect(rule(".topbar")).toMatch(/min-height:\s*49px/);
  expect(rule(".topbar")).toMatch(/flex-wrap:\s*nowrap/);
  expect(rule(".topbar-right")).toMatch(/flex-wrap:\s*nowrap/);
  expect(rule(".compact-navigation")).toMatch(/display:\s*none/);
});

test("the top bar names the app without repeating its decorative globe", () => {
  const { container } = render(<TopBar onNewSession={() => {}} />);
  expect(container.querySelector('[data-region="wordmark"]')?.textContent).toBe("VIKI Agent");
  expect(container.querySelector(".brand-mark")).toBeNull();
  expect(container.querySelector('[data-region="topbar"] img')).toBeNull();
  expect(container.querySelector('[data-region="topbar"] .face')).toBeNull();
});

test("index.html keeps the window caption blank and points the icon at the favicon", () => {
  // An invisible WORD JOINER title: Electron keeps "VIKI Agent" as the accessible name.
  expect(indexHtml).toContain("<title>&#x2060;</title>");
  expect(indexHtml).not.toContain("<title>VIKI Agent</title>");
  // The ?v= cache key is scripts/brand_icons.py's, pinned by tests/app/test_brand_icons.py.
  expect(indexHtml).toMatch(
    /<link rel="icon" href="\/favicon\.ico\?v=[0-9a-f]{8}" sizes="any" \/>/,
  );
});

test("Chat is selected; Agents, Routines and Vault are inert", () => {
  render(<TopBar onNewSession={() => {}} />);

  const chat = screen.getByRole("tab", { name: "Chat" });
  expect(chat.getAttribute("aria-selected")).toBe("true");

  for (const name of ["Agents", "Routines", "Vault"]) {
    const tab = screen.getByRole("tab", { name });
    expect(tab.getAttribute("aria-disabled")).toBe("true");
    expect(tab.tagName).not.toBe("BUTTON");
  }
});

test("with onRoutines the Routines tab is a live tab that follows the view", () => {
  const onRoutines = vi.fn();
  const { rerender } = render(<TopBar onNewSession={vi.fn()} onRoutines={onRoutines} />);
  const tab = screen.getByRole("tab", { name: "Routines" });
  expect(tab.tagName).toBe("BUTTON");
  expect(tab.getAttribute("aria-selected")).toBe("false");
  fireEvent.click(tab);
  expect(onRoutines).toHaveBeenCalledOnce();

  rerender(<TopBar view="routines" onNewSession={vi.fn()} onRoutines={onRoutines} />);
  const selected = screen.getByRole("tab", { name: "Routines" });
  expect(selected.getAttribute("aria-selected")).toBe("true");
  expect(selected.className).toContain("tab-active");
  expect(screen.getByRole("tab", { name: "Chat" }).getAttribute("aria-selected")).toBe("false");
});

test("the Routines tab shows an attention count with a sentence for screen readers", () => {
  const { container, rerender } = render(
    <TopBar onNewSession={vi.fn()} onRoutines={vi.fn()} routinesAttention={0} />,
  );
  expect(container.querySelector(".tab .decisions-count")).toBeNull();

  rerender(<TopBar onNewSession={vi.fn()} onRoutines={vi.fn()} routinesAttention={2} />);
  expect(container.querySelector(".tab .decisions-count")?.textContent).toBe("2");
  expect(screen.getByRole("tab", { name: /2 need you/ })).toBeTruthy();
});

test("the compact select offers Routines only with onRoutines", () => {
  const onChat = vi.fn(),
    onRoutines = vi.fn();
  const { rerender } = render(<TopBar onNewSession={vi.fn()} onChat={onChat} onVault={vi.fn()} />);
  expect(screen.queryByRole("option", { name: "Routines" })).toBeNull();

  rerender(
    <TopBar
      view="routines"
      onNewSession={vi.fn()}
      onChat={onChat}
      onVault={vi.fn()}
      onRoutines={onRoutines}
    />,
  );
  const select = screen.getByRole("combobox", { name: "Primary view" }) as HTMLSelectElement;
  expect(screen.getByRole("option", { name: "Routines" })).toBeTruthy();
  expect(select.value).toBe("routines");
  fireEvent.change(select, { target: { value: "routines" } });
  expect(onRoutines).toHaveBeenCalledOnce();
});

test("New session calls its handler once per click", () => {
  const onNewSession = vi.fn();
  render(<TopBar onNewSession={onNewSession} />);

  fireEvent.click(screen.getByRole("button", { name: "New session" }));
  expect(onNewSession).toHaveBeenCalledTimes(1);
});

test("sidebar navigation removes duplicated actions while retaining window controls", () => {
  const onToggle = vi.fn();
  const { container } = render(
    <TopBar
      navigationInSidebar
      onNewSession={vi.fn()}
      onChat={vi.fn()}
      onCapabilities={vi.fn()}
      onAgents={vi.fn()}
      onRoutines={vi.fn()}
      onVault={vi.fn()}
      onCode={vi.fn()}
      appMenu={<button type="button">Help and settings</button>}
      rails={{ left: { open: false, onToggle }, right: { open: false, onToggle } }}
    />,
  );
  expect(screen.queryByRole("tablist", { name: "Primary" })).toBeNull();
  expect(screen.queryByRole("combobox", { name: "Primary view" })).toBeNull();
  expect(screen.queryByRole("button", { name: "New session" })).toBeNull();
  expect(container.querySelector('[data-region="wordmark"]')).not.toBeNull();
  expect(screen.getByRole("button", { name: "Help and settings" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Sessions and agents" }));
  fireEvent.click(screen.getByRole("button", { name: "VIKI panel" }));
  expect(onToggle).toHaveBeenCalledTimes(2);
});

test("available primary views work through tabs and compact navigation", () => {
  const onChat = vi.fn(),
    onVault = vi.fn();
  render(<TopBar view="vault" onNewSession={vi.fn()} onChat={onChat} onVault={onVault} />);
  expect(screen.getByRole("tab", { name: "Vault" }).getAttribute("aria-selected")).toBe("true");
  fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
  expect(onChat).toHaveBeenCalledOnce();
  fireEvent.change(screen.getByRole("combobox", { name: "Primary view" }), {
    target: { value: "vault" },
  });
  expect(onVault).toHaveBeenCalledOnce();
});

test("renders the Code tab", () => {
  const onChat = vi.fn(),
    onCode = vi.fn();
  const { rerender } = render(<TopBar onNewSession={vi.fn()} />);
  expect(screen.getByRole("tab", { name: "Code" }).getAttribute("aria-disabled")).toBe("true");

  rerender(<TopBar view="code" onNewSession={vi.fn()} onChat={onChat} onCode={onCode} />);
  const tab = screen.getByRole("tab", { name: "Code" });
  expect(tab.getAttribute("aria-selected")).toBe("true");
  fireEvent.click(tab);
  expect(onCode).toHaveBeenCalledOnce();
  const select = screen.getByRole("combobox", { name: "Primary view" }) as HTMLSelectElement;
  expect(select.value).toBe("code");
  fireEvent.change(select, { target: { value: "chat" } });
  fireEvent.change(select, { target: { value: "code" } });
  expect(onCode).toHaveBeenCalledTimes(2);
});

test("outside chat, agents and vault the compact select selects nothing, and Chat still works", () => {
  const onChat = vi.fn();
  render(<TopBar view="settings" onNewSession={vi.fn()} onChat={onChat} onVault={vi.fn()} />);
  const select = screen.getByRole("combobox", { name: "Primary view" }) as HTMLSelectElement;
  expect(select.value).toBe("");
  fireEvent.change(select, { target: { value: "chat" } });
  expect(onChat).toHaveBeenCalledOnce();
});

test("no drawer toggle is drawn for a rail that is docked", () => {
  render(<TopBar onNewSession={() => {}} />);
  expect(screen.queryByRole("button", { name: "Sessions and agents" })).toBeNull();
  expect(screen.queryByRole("button", { name: "VIKI panel" })).toBeNull();
});

test("each toggle says what it opens, that it opens a dialog, and whether it is open", () => {
  const onToggle = vi.fn();
  const { container, rerender } = render(
    <TopBar
      onNewSession={() => {}}
      rails={{ left: { open: false, onToggle }, right: { open: true, onToggle } }}
    />,
  );

  const left = screen.getByRole("button", { name: "Sessions and agents" });
  const right = screen.getByRole("button", { name: "VIKI panel" });
  expect(left.getAttribute("aria-haspopup")).toBe("dialog");
  expect(right.getAttribute("aria-haspopup")).toBe("dialog");
  expect(left.getAttribute("aria-expanded")).toBe("false");
  expect(right.getAttribute("aria-expanded")).toBe("true");

  // The left toggle opens the rail that would stand at the window's left edge, so it stands
  // there too; the right one sits with the other right-hand controls.
  const bar = container.querySelector(".topbar") as HTMLElement;
  expect(bar.firstElementChild).toBe(left);
  const rightSide = container.querySelector(".topbar-right") as HTMLElement;
  expect(rightSide.firstElementChild).toBe(right);

  fireEvent.click(left);
  fireEvent.click(right);
  expect(onToggle).toHaveBeenCalledTimes(2);

  // New session stays the one button with that name, whatever else the bar carries.
  rerender(<TopBar onNewSession={() => {}} rails={{ left: { open: true, onToggle } }} />);
  expect(screen.getAllByRole("button", { name: "New session" })).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "VIKI panel" })).toBeNull();
});

test("the left toggle's inward pull uses the child combinator, so it never reaches the right toggle", () => {
  // `.topbar-right`'s own first child is the right toggle when a rail toggle sits there; the
  // descendant form (`.topbar .rail-toggle:first-child`) would match that one too.
  expect(ruleIn(css, ".topbar > .rail-toggle:first-child")).toMatch(/margin-left:\s*-8px/);
  expect(css).not.toContain(".topbar .rail-toggle:first-child");
});

test("the toggle is a 32px icon button with a visible focus ring", () => {
  expect(rule(".rail-toggle")).toMatch(/width:\s*32px/);
  expect(rule(".rail-toggle")).toMatch(/height:\s*32px/);
  expect(rule(".rail-toggle:focus-visible")).toMatch(/outline:\s*1px solid var\(--or\)/);
});

test("below 800 local px the tab strip gives room to the toggles and New session", () => {
  const narrow = narrowBlock(css, COMPACT_QUERY);
  expect(narrow).toMatch(/\.tabs\s*\{\s*display:\s*none/);
  expect(narrowBlock(css)).toMatch(/\.topbar\s*\{\s*padding:\s*0 12px/);
  // The design's height remains the floor when a narrow bar needs additional rows.
  expect(rule(".topbar")).toMatch(/min-height:\s*49px/);
});

test("the compact select is sized like the tab strip it replaces, not the body's inherited 16px", () => {
  // At `font: inherit` alone (the body's 16px IBM Plex Sans), "Agents" is cut off at the select's
  // 64px max-width (measured in the review's probe). 14px, the tab strip's own size, fits it whole.
  expect(rule(".compact-navigation")).toMatch(/font-size:\s*14px/);
});

test("at a content box of 455px or less the right-hand cluster gives up its own gap and padding", () => {
  // Without these trims the compact select (bounded to the S block above) widens the bar past
  // 400 CSS px on its own; at a content box of 455px or less the right-hand cluster's own gap
  // and New session's padding give the rest of the fit back. See
  // e2e/scenarios/shell/narrow-layout.test.ts and e2e/scenarios/shell/narrow-views.test.ts for
  // the measured proof, and TopBar.css for the width band each rule pins.
  const narrow = narrowBlock(css, COMPACT_QUERY);
  expect(narrow).toMatch(/\.compact-navigation\s*\{[^}]*max-width:\s*64px/);
  expect(narrow).not.toMatch(/new-session/);

  const sub = subBand();
  expect(sub).toMatch(/\.topbar-right\s*\{\s*gap:\s*2px/);
  expect(sub).toMatch(/\.new-session\s*\{\s*padding:\s*0 4px/);
});

test("at a content box of 455px or less the rail toggle narrows to 28px, the app menu toggle stays 32px", () => {
  // The app menu toggle (AppMenu.css) stays 32px, which e2e/scenarios/shell/narrow-views.test.ts pins.
  // The rail toggle narrows to 28px.
  const sub = subBand();
  expect(sub).toMatch(/\.rail-toggle\s*\{\s*width:\s*28px;\s*\n\s*\}/);
});

test("Help is not hidden in narrow bars", () => {
  expect(css).not.toMatch(/\.help-menu-toggle\s*\{[^}]*display:\s*none/);
});

test("the bar is its own query container, so a rule can key off its local width under zoom", () => {
  // A real-viewport media query cannot see this: at font scale 1.5 a 900px window lays the bar
  // out in 600 local CSS px, well past the S band's own 800px `min-width` check. Only a query on
  // the bar's own rendered size catches that — `container-type: inline-size`, on the base rule so
  // it applies at every width, not only inside some other band.
  expect(rule(".topbar")).toMatch(/container-type:\s*inline-size/);
});

test("the shared 800 local px cutoff keeps compact navigation and wrapping aligned", () => {
  // At 900px and scale 1.5, keeping tabs in a wrapped row increased the header by 47.5 painted
  // pixels. The existing select keeps every primary view reachable while controls can wrap.
  const container = narrowBlock(css, COMPACT_QUERY);
  expect(ruleIn(narrowBlock(css, "@container (max-width: 1000px)"), ".topbar")).toMatch(
    /flex-wrap:\s*wrap/,
  );
  expect(ruleIn(container, ".tabs")).toMatch(/display:\s*none/);
  expect(ruleIn(container, ".compact-navigation")).toMatch(/display:\s*block/);
  expect(ruleIn(container, ".compact-navigation")).toMatch(/max-width:\s*64px/);
  expect(ruleIn(container, ".topbar-right")).toMatch(/flex-wrap:\s*wrap/);
  expect(ruleIn(container, ".topbar-right")).toMatch(/gap:\s*6px/);
  expect(ruleIn(containerBand(), ".new-session")).toMatch(/padding:\s*0 10px/);
});

test("the 700px local-width band is declared before the S band and the 455px one, so 400px follows cascade order and 480px keeps only the 700px block's trims", () => {
  // At a real 400px viewport (scale 1) all three bands are true at once (local width equals real
  // width there): declaration order is what lets the sub-band's narrower values win the cascade
  // on New session's padding rather than the 700px block's looser value taking over. At
  // 480px the sub-band no longer applies, so the 700px block's values stand alone.
  const containerIndex = css.indexOf(CONTAINER_QUERY);
  const sBandIndex = css.indexOf(NARROW_QUERY);
  const subBandIndex = css.indexOf(SUB_BAND_QUERY);
  expect(containerIndex).toBeGreaterThan(-1);
  expect(containerIndex).toBeLessThan(sBandIndex);
  expect(containerIndex).toBeLessThan(subBandIndex);
});

test("topbar-right can shrink and New session never does", () => {
  expect(rule(".topbar-right")).toMatch(/min-width:\s*0/);
  expect(rule(".new-session")).toMatch(/flex:\s*none/);
});

test("the Chat tab follows the view, and the app menu sits before New session", () => {
  const { container, rerender } = render(
    <TopBar onNewSession={vi.fn()} appMenu={<button type="button">menu</button>} />,
  );
  const chat = container.querySelector(".tab-active") as HTMLElement;
  expect(chat.getAttribute("aria-selected")).toBe("true");
  expect(chat.getAttribute("tabindex")).toBe("-1");

  rerender(
    <TopBar onNewSession={vi.fn()} view="settings" appMenu={<button type="button">menu</button>} />,
  );
  const away = container.querySelector(".tab-active") as HTMLElement;
  expect(away.getAttribute("aria-selected")).toBe("false");
  expect(away.getAttribute("tabindex")).toBe("-1");

  const right = [...(container.querySelector(".topbar-right")?.children ?? [])];
  expect(right.at(-2)?.textContent).toBe("menu");
  expect(right.at(-1)?.className).toBe("new-session");
});

test("renders no Approvals button", () => {
  render(<TopBar onNewSession={() => {}} appMenu={<button type="button">menu</button>} />);
  expect(screen.queryByRole("button", { name: /^Approvals/ })).toBeNull();
});

test("under forced colours the selected tab draws a border, leaving the outline channel to the UA's own focus ring", () => {
  // A keyboard user tabbed onto the selected tab has to see focus move, not just the selection
  // mark that was already on screen: an outline here would otherwise replace the UA's own
  // `:focus-visible` ring, which is the only other mark this control has.
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, ".tab-active")).toMatch(/border-bottom:\s*2px solid Highlight/);
  expect(ruleIn(forced, ".tab-active")).not.toMatch(/outline/);
  expect(css.slice(css.indexOf(forced) + forced.length)).not.toContain(".tab-active");
});

test("a refused New session keeps its place and its focus, and does nothing", () => {
  const onNewSession = vi.fn();
  const { container, rerender } = render(<TopBar onNewSession={onNewSession} />);
  const button = container.querySelector(".new-session") as HTMLButtonElement;
  // Nothing refuses it by default: no attribute at all, which is what a caller that passes no
  // `newSession` sees.
  expect(button.hasAttribute("aria-disabled")).toBe(false);

  rerender(
    <TopBar
      onNewSession={onNewSession}
      newSession={{ disabled: true, describedBy: "readiness-reason" }}
    />,
  );
  const refused = container.querySelector(".new-session") as HTMLButtonElement;
  expect(refused.getAttribute("aria-disabled")).toBe("true");
  expect(refused.getAttribute("aria-describedby")).toBe("readiness-reason");
  // `disabled` is never used: an unfocusable button is never announced, so the user would find
  // the command simply gone instead of gone with a reason.
  expect(refused.disabled).toBe(false);
  refused.focus();
  expect(document.activeElement).toBe(refused);
  fireEvent.click(refused);
  expect(onNewSession).not.toHaveBeenCalled();
});

test("TopBar.css declares no color-scheme", () => {
  expect(css).not.toContain("color-scheme");
});

test("primary arrows wrap available tabs without selecting or navigating", () => {
  const onChat = vi.fn(),
    onVault = vi.fn(),
    onCode = vi.fn();
  render(
    <TopBar
      view="vault"
      onNewSession={vi.fn()}
      onChat={onChat}
      onVault={onVault}
      onCode={onCode}
    />,
  );
  const tabs = screen.getAllByRole("tab") as [
    HTMLElement,
    HTMLElement,
    HTMLElement,
    HTMLElement,
    HTMLElement,
  ];
  const [chat, agents, routines, vault, code] = tabs;
  expect(tabs.map((tab) => tab.textContent)).toEqual([
    "Chat",
    "Agents",
    "Routines",
    "Vault",
    "Code",
  ]);
  const tabStop = (target: HTMLElement) =>
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toEqual([target]);
  tabStop(vault);
  expect([agents.tabIndex, routines.tabIndex]).toEqual([-1, -1]);
  act(() => vault.focus());
  for (const [key, target] of [
    ["ArrowRight", code],
    ["ArrowRight", chat],
    ["ArrowLeft", code],
    ["Home", chat],
    ["End", code],
  ] as const) {
    expect(fireEvent.keyDown(document.activeElement as HTMLElement, { key })).toBe(false);
    expect(document.activeElement).toBe(target);
    tabStop(target);
  }
  expect(vault.getAttribute("aria-selected")).toBe("true");
  expect(chat.getAttribute("aria-selected")).toBe("false");
  for (const callback of [onChat, onVault, onCode]) expect(callback).not.toHaveBeenCalled();
});

test("tab stops follow changed view and capabilities while spans remain inert", () => {
  const onVault = vi.fn(),
    onCode = vi.fn();
  const { rerender } = render(<TopBar onNewSession={vi.fn()} onVault={onVault} onCode={onCode} />);
  const stops = () =>
    screen
      .getAllByRole("tab")
      .filter((tab) => tab.tabIndex === 0)
      .map((tab) => tab.textContent);
  const chat = screen.getByRole("tab", { name: "Chat" });
  expect(chat.tagName).toBe("SPAN");
  expect(chat.getAttribute("aria-disabled")).toBe("true");
  expect(chat.tabIndex).toBe(-1);
  expect(stops()).toEqual(["Vault"]);
  rerender(<TopBar view="code" onNewSession={vi.fn()} onVault={onVault} onCode={onCode} />);
  expect(stops()).toEqual(["Code"]);
  rerender(<TopBar view="code" onNewSession={vi.fn()} onVault={onVault} />);
  expect(stops()).toEqual(["Vault"]);
  rerender(<TopBar onNewSession={vi.fn()} />);
  expect(stops()).toEqual([]);
});

test.each([
  { isComposing: true },
  { keyCode: 229 },
  { ctrlKey: true },
  { altKey: true },
  { metaKey: true },
])("primary arrows ignore IME and modifiers: %j", (modifier) => {
  const onChat = vi.fn(),
    onVault = vi.fn();
  render(<TopBar onNewSession={vi.fn()} onChat={onChat} onVault={onVault} />);
  const chat = screen.getByRole("tab", { name: "Chat" });
  act(() => chat.focus());
  expect(fireEvent.keyDown(chat, { key: "ArrowRight", ...modifier })).toBe(true);
  expect(document.activeElement).toBe(chat);
  expect(onChat).not.toHaveBeenCalled();
  expect(onVault).not.toHaveBeenCalled();
});

test.each(["Enter", " "])("%j leaves one native activation and caller-owned selection", (key) => {
  const onChat = vi.fn(),
    onVault = vi.fn();
  render(<TopBar onNewSession={vi.fn()} onChat={onChat} onVault={onVault} />);
  const vault = screen.getByRole("tab", { name: "Vault" });
  act(() => vault.focus());
  expect(fireEvent.keyDown(vault, { key })).toBe(true);
  fireEvent.keyUp(vault, { key });
  expect(onVault).not.toHaveBeenCalled();
  // jsdom has no button key default; this click represents that single native activation.
  fireEvent.click(vault);
  expect(onVault).toHaveBeenCalledOnce();
  expect(onChat).not.toHaveBeenCalled();
  expect(screen.getByRole("tab", { name: "Chat" }).getAttribute("aria-selected")).toBe("true");
  expect(vault.getAttribute("aria-selected")).toBe("false");
});

test.each([
  ["settings", "Settings"],
  ["data", "Data"],
  ["recovery", "Recovery"],
  ["onboarding", "Welcome"],
  ["agents", "Agents"],
  ["routines", "Routines"],
  ["vault", "Vault"],
  ["code", "Code"],
] as const)("compact %s shows its current page without making empty navigate", (view, label) => {
  const onChat = vi.fn(),
    onVault = vi.fn(),
    onCode = vi.fn();
  render(
    <TopBar
      view={view}
      onNewSession={vi.fn()}
      onChat={onChat}
      {...(view === "vault" ? {} : { onVault })}
      {...(view === "code" ? {} : { onCode })}
    />,
  );
  const select = screen.getByRole("combobox", { name: "Primary view" }) as HTMLSelectElement;
  const current = screen.getByRole("option", { name: label }) as HTMLOptionElement;
  expect(select.value).toBe("");
  expect(current.value).toBe("");
  expect(current.selected).toBe(true);
  expect(current.disabled).toBe(true);
  expect(current.hidden).toBe(false);
  fireEvent.change(select, { target: { value: "" } });
  for (const callback of [onChat, onVault, onCode]) expect(callback).not.toHaveBeenCalled();
});

test("compact primary options keep their order and existing actions", () => {
  const callbacks = [vi.fn(), vi.fn(), vi.fn(), vi.fn(), vi.fn()] as const;
  render(
    <TopBar
      onNewSession={vi.fn()}
      onChat={callbacks[0]}
      onAgents={callbacks[1]}
      onRoutines={callbacks[2]}
      onVault={callbacks[3]}
      onCode={callbacks[4]}
    />,
  );
  expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
    "Chat",
    "Agents",
    "Routines",
    "Vault",
    "Code",
  ]);
  for (const value of ["chat", "agents", "routines", "vault", "code"]) {
    fireEvent.change(screen.getByRole("combobox", { name: "Primary view" }), { target: { value } });
  }
  for (const callback of callbacks) expect(callback).toHaveBeenCalledOnce();
});

test("optional Bots navigation uses existing primary tabs and compact choices", () => {
  const onBots = vi.fn();
  render(<TopBar onNewSession={vi.fn()} onChat={vi.fn()} onBots={onBots} view="bots" />);
  const tab = screen.getByRole("tab", { name: "Bots" });
  expect(tab.getAttribute("aria-selected")).toBe("true");
  fireEvent.click(tab);
  fireEvent.change(screen.getByRole("combobox", { name: "Primary view" }), {
    target: { value: "bots" },
  });
  expect(onBots).toHaveBeenCalledTimes(2);
});

test("the right rail toggle shows a dot when attention is above 0", () => {
  const onToggle = vi.fn();
  const { rerender } = render(
    <TopBar onNewSession={() => {}} rails={{ right: { open: false, onToggle, attention: 0 } }} />,
  );

  let toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.querySelector("svg")).not.toBeNull();
  expect(toggle.querySelector(".rail-toggle-dot")).toBeNull();

  rerender(
    <TopBar onNewSession={() => {}} rails={{ right: { open: false, onToggle, attention: 3 } }} />,
  );
  toggle = screen.getByRole("button", { name: "VIKI panel" });
  // The CSS and the design put the dot over the icon (absolutely positioned in the
  // toggle's corner), never in place of it: the icon stays for attention 0 and above alike.
  expect(toggle.querySelector("svg")).not.toBeNull();
  expect(toggle.querySelector(".rail-toggle-dot")).not.toBeNull();
  expect(toggle.querySelector(".rail-toggle-dot")?.getAttribute("aria-hidden")).toBe("true");
});

test("the right rail toggle describes its waiting count accessibly", () => {
  const onToggle = vi.fn();
  const { rerender } = render(
    <TopBar onNewSession={vi.fn()} rails={{ right: { open: false, onToggle, attention: 0 } }} />,
  );
  const toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.getAttribute("aria-describedby")).toBeNull();
  expect(screen.queryByText("0 waiting for you")).toBeNull();

  rerender(
    <TopBar onNewSession={vi.fn()} rails={{ right: { open: false, onToggle, attention: 5 } }} />,
  );
  const describedToggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(screen.getByRole("button", { name: "VIKI panel", description: "5 waiting for you" })).toBe(
    describedToggle,
  );
  const description = screen.getByText("5 waiting for you");
  expect(description.className).toContain("sr-only");
  expect(describedToggle.getAttribute("aria-describedby")).toBe(description.id);
  expect(describedToggle.getAttribute("title")).toBe("5 waiting for you");
  expect(describedToggle.querySelector(".rail-toggle-dot")).not.toBeNull();
  expect(describedToggle.getAttribute("aria-label")).toBe("VIKI panel");
});

test("the right rail toggle's title reflects the attention count", () => {
  const onToggle = vi.fn();
  const { rerender } = render(
    <TopBar onNewSession={() => {}} rails={{ right: { open: false, onToggle, attention: 0 } }} />,
  );

  let toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.getAttribute("title")).toBeNull();

  rerender(
    <TopBar onNewSession={() => {}} rails={{ right: { open: false, onToggle, attention: 5 } }} />,
  );
  toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.getAttribute("title")).toBe("5 waiting for you");

  rerender(
    <TopBar onNewSession={() => {}} rails={{ right: { open: false, onToggle, attention: 1 } }} />,
  );
  toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.getAttribute("title")).toBe("1 waiting for you");
});

test("the right rail toggle's aria-label stays 'VIKI panel' when attention changes", () => {
  const onToggle = vi.fn();
  const { rerender } = render(
    <TopBar onNewSession={() => {}} rails={{ right: { open: false, onToggle, attention: 0 } }} />,
  );

  let toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.getAttribute("aria-label")).toBe("VIKI panel");

  rerender(
    <TopBar onNewSession={() => {}} rails={{ right: { open: false, onToggle, attention: 10 } }} />,
  );
  toggle = screen.getByRole("button", { name: "VIKI panel" });
  expect(toggle.getAttribute("aria-label")).toBe("VIKI panel");
});

test("the rail toggle dot is styled with position absolute", () => {
  expect(rule(".rail-toggle-dot")).toMatch(/position:\s*absolute/);
  expect(rule(".rail-toggle-dot")).toMatch(/top:\s*5px/);
  expect(rule(".rail-toggle-dot")).toMatch(/right:\s*5px/);
  expect(rule(".rail-toggle-dot")).toMatch(/width:\s*6px/);
  expect(rule(".rail-toggle-dot")).toMatch(/height:\s*6px/);
  expect(rule(".rail-toggle-dot")).toMatch(/border-radius:\s*50%/);
  expect(rule(".rail-toggle-dot")).toMatch(/background:\s*var\(--or\)/);
});

test("the rail toggle is positioned relative to contain the dot", () => {
  expect(rule(".rail-toggle")).toMatch(/position:\s*relative/);
});

test("under forced colours the rail toggle dot uses Highlight", () => {
  const forced = narrowBlock(css, "@media (forced-colors: active)");
  expect(ruleIn(forced, ".rail-toggle-dot")).toMatch(/background:\s*Highlight/);
});
