/**
 * HeaderTrail: the pill, the mode chip and the search button, in their one right-aligned row.
 * The chip's own mechanics (controlled open, click guard, row highlighting) are ModelChip.tsx's;
 * this file pins what is specific to HeaderTrail — order, the pill's count text, the mode
 * labels and menu, the locked state, the search button's wiring, and the CSS that keeps the row
 * to one auto margin.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { t } from "../../i18n/t";
import { readCss, ruleIn } from "../../testing/css";
import { ConversationHeader } from "../Conversation/ConversationHeader";
import { HeaderTrail } from "./HeaderTrail";

afterEach(() => {
  cleanup();
});

function tools(over: { open?: boolean; onToggle?: () => void; panelId?: string } = {}) {
  return {
    open: over.open ?? false,
    panelId: over.panelId ?? "tools-panel",
    onToggle: over.onToggle ?? vi.fn(),
    buttonRef: { current: null },
  };
}

// --- order ------------------------------------------------------------------------------------

test("renders the pill, the chip, the search button and children in that order", () => {
  const { container } = render(
    <HeaderTrail running={2} onShowRuns={vi.fn()} mode="auto" onMode={vi.fn()} tools={tools()}>
      <span data-testid="tail">menu</span>
    </HeaderTrail>,
  );
  const trail = container.querySelector(".header-trail") as HTMLElement;
  const kids = [...trail.children];
  expect(kids.map((el) => el.className)).toEqual([
    "agents-pill",
    "mode-chip",
    "header-tools-button",
    "",
  ]);
  expect(kids[3]?.getAttribute("data-testid")).toBe("tail");
});

test("with nothing to show, children still render inside the trail", () => {
  const { container } = render(
    <HeaderTrail>
      <span data-testid="tail" />
    </HeaderTrail>,
  );
  const trail = container.querySelector(".header-trail") as HTMLElement;
  expect(trail.children.length).toBe(1);
  expect(trail.querySelector('[data-testid="tail"]')).not.toBeNull();
});

// --- the pill: plural, gating, click ----------------------------------------------------------

describe("the agents pill", () => {
  test("reads singular at exactly one", () => {
    const { container } = render(<HeaderTrail running={1} onShowRuns={vi.fn()} />);
    expect(container.querySelector(".agents-pill")?.textContent).toContain("1 subagent running");
  });

  test("reads plural above one", () => {
    const { container } = render(<HeaderTrail running={3} onShowRuns={vi.fn()} />);
    expect(container.querySelector(".agents-pill")?.textContent).toContain("3 subagents running");
  });

  test("is absent at zero even with a handler", () => {
    const { container } = render(<HeaderTrail running={0} onShowRuns={vi.fn()} />);
    expect(container.querySelector(".agents-pill")).toBeNull();
  });

  test("is absent without a handler even with a count", () => {
    const { container } = render(<HeaderTrail running={2} />);
    expect(container.querySelector(".agents-pill")).toBeNull();
  });

  test("carries its title, its type and an aria-hidden ring", () => {
    const { container } = render(<HeaderTrail running={1} onShowRuns={vi.fn()} />);
    const pill = container.querySelector(".agents-pill") as HTMLButtonElement;
    expect(pill.type).toBe("button");
    expect(pill.getAttribute("title")).toBe("Show their runs in the VIKI panel");
    const ring = pill.querySelector(".agents-pill-ring");
    expect(ring?.className).toContain("spinner");
    expect(ring?.getAttribute("aria-hidden")).toBe("true");
  });

  test("click calls onShowRuns", () => {
    const onShowRuns = vi.fn();
    const { container } = render(<HeaderTrail running={1} onShowRuns={onShowRuns} />);
    fireEvent.click(container.querySelector(".agents-pill") as HTMLElement);
    expect(onShowRuns).toHaveBeenCalledTimes(1);
  });
});

// --- the mode chip: labels, gating -------------------------------------------------------------

describe("the mode chip's label", () => {
  test.each([
    ["default", "Manual"],
    ["manual", "Manual"],
    ["acceptEdits", "Accept edits"],
    ["auto", "Auto"],
    ["plan", "Plan"],
    ["bypassPermissions", "bypassPermissions"],
  ])("mode %s shows as %s", (raw, shown) => {
    const { container } = render(<HeaderTrail mode={raw} onMode={vi.fn()} />);
    const trigger = container.querySelector(".mode-chip") as HTMLElement;
    expect(trigger.textContent).toContain(shown);
    expect(trigger.getAttribute("aria-label")).toBe(`Mode: ${shown}. Change mode`);
  });

  test("an empty mode means no chip, even with a handler", () => {
    const { container } = render(<HeaderTrail mode="" onMode={vi.fn()} />);
    expect(container.querySelector(".mode-chip")).toBeNull();
  });

  test("no handler means no chip, even with a mode", () => {
    const { container } = render(<HeaderTrail mode="auto" />);
    expect(container.querySelector(".mode-chip")).toBeNull();
  });

  test("unlocked, the title explains what the chip governs", () => {
    const { container } = render(<HeaderTrail mode="auto" onMode={vi.fn()} />);
    expect(container.querySelector(".mode-chip")?.getAttribute("title")).toBe(
      "Permission mode for this chat",
    );
  });
});

// --- the mode menu: rows, onMode values, current no-op ------------------------------------------

describe("the mode menu", () => {
  test("lists the four choices with their verbatim descriptions, in order", () => {
    render(<HeaderTrail mode="auto" onMode={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Change mode/ }));
    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.querySelector(".chip-model-label")?.textContent)).toEqual([
      "Manual",
      "Accept edits",
      "Auto",
      "Plan",
    ]);
    expect(items.map((item) => item.querySelector(".d.chip-model-desc")?.textContent)).toEqual([
      t("settings.mode.manual.desc"),
      t("settings.mode.accept_edits.desc"),
      "A safety check approves routine actions",
      "Plans first; changes nothing until you approve",
    ]);
  });

  test("uses the catalog's Manual and Accept edits details", () => {
    const manual = "Asks before edits and commands your Claude Code settings don't allow";
    const acceptEdits =
      "Edits files, and runs rm, mv, cp and sed, in this chat's folders without asking";
    render(<HeaderTrail mode="auto" onMode={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Change mode/ }));
    const items = screen.getAllByRole("menuitem");
    expect(t("settings.mode.manual.desc")).toBe(manual);
    expect(t("settings.mode.accept_edits.desc")).toBe(acceptEdits);
    expect(items[0]?.querySelector(".chip-model-desc")?.textContent).toBe(manual);
    expect(items[1]?.querySelector(".chip-model-desc")?.textContent).toBe(acceptEdits);
  });

  test("ConversationHeader alone does not add a mode chip", () => {
    const { container } = render(<ConversationHeader title="Chat" />);
    expect(container.querySelector(".mode-chip")).toBeNull();
  });

  test("marks the current mode with .on and a checkmark, and no other row", () => {
    render(<HeaderTrail mode="auto" onMode={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Change mode/ }));
    const items = screen.getAllByRole("menuitem");
    expect(items[2]?.className).toBe("mi on");
    expect(items[2]?.querySelector(".chip-check")?.textContent).toBe("✓");
    for (const other of [items[0], items[1], items[3]]) {
      expect(other?.className).toBe("mi");
      expect(other?.querySelector(".chip-check")?.textContent).toBe("");
    }
  });

  test("picking a different row calls onMode with its ModeChoice value", () => {
    const onMode = vi.fn();
    render(<HeaderTrail mode="auto" onMode={onMode} />);
    fireEvent.click(screen.getByRole("button", { name: /Change mode/ }));
    fireEvent.click(screen.getAllByRole("menuitem")[1] as HTMLElement); // Accept edits
    expect(onMode).toHaveBeenCalledWith("acceptEdits");
    expect(onMode).toHaveBeenCalledTimes(1);
  });

  test("picking the current row is a no-op", () => {
    const onMode = vi.fn();
    render(<HeaderTrail mode="auto" onMode={onMode} />);
    fireEvent.click(screen.getByRole("button", { name: /Change mode/ }));
    fireEvent.click(screen.getAllByRole("menuitem")[2] as HTMLElement); // Auto, already current
    expect(onMode).not.toHaveBeenCalled();
  });

  test(`mode "manual" counts as the default choice for the checkmark and the no-op`, () => {
    const onMode = vi.fn();
    render(<HeaderTrail mode="manual" onMode={onMode} />);
    fireEvent.click(screen.getByRole("button", { name: /Change mode/ }));
    const items = screen.getAllByRole("menuitem");
    expect(items[0]?.className).toBe("mi on"); // Manual/default
    fireEvent.click(items[0] as HTMLElement);
    expect(onMode).not.toHaveBeenCalled();
  });
});

// --- locked --------------------------------------------------------------------------------

test("a locked mode disables the chip, swaps its title, and opens no menu", () => {
  const onMode = vi.fn();
  const { container } = render(<HeaderTrail mode="auto" onMode={onMode} modeLocked />);
  const trigger = container.querySelector(".mode-chip") as HTMLButtonElement;
  expect(trigger.disabled).toBe(true);
  expect(trigger.getAttribute("title")).toBe("Set by the bound profile");
  fireEvent.click(trigger);
  expect(screen.queryByRole("menu")).toBeNull();
});

// --- the search button: attrs, gating, click --------------------------------------------------

describe("the search button", () => {
  test("is absent without a tools toggle", () => {
    const { container } = render(<HeaderTrail />);
    expect(container.querySelector(".header-tools-button")).toBeNull();
  });

  test("carries its label, title and aria wiring, and forwards the ref", () => {
    const ref = { current: null };
    const { container } = render(
      <HeaderTrail tools={{ ...tools({ panelId: "p-9" }), buttonRef: ref }} />,
    );
    const button = container.querySelector(".header-tools-button") as HTMLButtonElement;
    expect(button.type).toBe("button");
    expect(button.getAttribute("aria-label")).toBe("Find, select and export (Ctrl+F)");
    expect(button.getAttribute("title")).toBe("Find, select and export (Ctrl+F)");
    expect(button.getAttribute("aria-controls")).toBe("p-9");
    expect(ref.current).toBe(button);
  });

  test("names Cmd+F on macOS, where that is the shortcut", () => {
    Object.defineProperty(window.navigator, "platform", { value: "MacIntel", configurable: true });
    try {
      const { container } = render(<HeaderTrail tools={tools()} />);
      const button = container.querySelector(".header-tools-button") as HTMLButtonElement;
      expect(button.getAttribute("aria-label")).toBe("Find, select and export (⌘F)");
      expect(button.getAttribute("title")).toBe("Find, select and export (⌘F)");
    } finally {
      delete (window.navigator as { platform?: string }).platform;
    }
  });

  test("aria-expanded mirrors the panel's open state", () => {
    const { container, rerender } = render(<HeaderTrail tools={tools({ open: false })} />);
    expect(container.querySelector(".header-tools-button")?.getAttribute("aria-expanded")).toBe(
      "false",
    );
    rerender(<HeaderTrail tools={tools({ open: true })} />);
    expect(container.querySelector(".header-tools-button")?.getAttribute("aria-expanded")).toBe(
      "true",
    );
  });

  test("click calls onToggle", () => {
    const onToggle = vi.fn();
    const { container } = render(<HeaderTrail tools={tools({ onToggle })} />);
    fireEvent.click(container.querySelector(".header-tools-button") as HTMLElement);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  test("carries LeftRail's own magnifier, sized up and aria-hidden", () => {
    const { container } = render(<HeaderTrail tools={tools()} />);
    const icon = container.querySelector(".header-tools-button svg.ico") as SVGElement;
    expect(icon).not.toBeNull();
    expect(icon.getAttribute("aria-hidden")).toBe("true");
    expect(icon.getAttribute("width")).toBe("16");
    expect(icon.getAttribute("height")).toBe("16");
  });
});

// --- CSS pins --------------------------------------------------------------------------------

describe("HeaderTrail.css", () => {
  const css = readCss(import.meta.url, "HeaderTrail.css");

  test("the trail is one flex row with the row's single auto margin and does not grow", () => {
    const rule = ruleIn(css, ".header-trail");
    expect(rule).toMatch(/display:\s*flex/);
    expect(rule).toMatch(/align-items:\s*center/);
    expect(rule).toMatch(/gap:\s*8px/);
    expect(rule).toMatch(/margin-left:\s*auto/);
    expect(rule).toMatch(/flex:\s*none/);
  });

  test("the agents pill wears the --or role tokens, not the shared .chip utility's own ones", () => {
    const rule = ruleIn(css, ".agents-pill");
    expect(rule).toMatch(/height:\s*22px/);
    expect(rule).toMatch(/border-radius:\s*11px/);
    expect(rule).toMatch(/color:\s*var\(--or\)/);
    expect(rule).toMatch(/background:\s*rgb\(var\(--or-rgb\)\s*\/\s*0\.08\)/);
    expect(rule).toMatch(/border:\s*1px solid rgb\(var\(--or-rgb\)\s*\/\s*0\.45\)/);
  });

  test("the pill brightens on hover and on keyboard focus alike", () => {
    expect(css).toMatch(
      /\.agents-pill:hover,\s*\.agents-pill:focus-visible\s*\{\s*background:\s*rgb\(var\(--or-rgb\)\s*\/\s*0\.16\)/,
    );
  });

  test("the ring is sized to 10px, leaving its colour and motion to the shared spinner", () => {
    const rule = ruleIn(css, ".agents-pill-ring");
    expect(rule).toMatch(/width:\s*10px/);
    expect(rule).toMatch(/height:\s*10px/);
  });

  test("the mode chip is a bordered pill in the neutral tokens, with a disabled cursor", () => {
    const rule = ruleIn(css, ".mode-chip");
    expect(rule).toMatch(/height:\s*22px/);
    expect(rule).toMatch(/border-radius:\s*11px/);
    expect(rule).toMatch(/color:\s*var\(--t1\)/);
    expect(rule).toMatch(/border:\s*1px solid var\(--b1\)/);
    expect(ruleIn(css, ".mode-chip:disabled")).toMatch(/cursor:\s*default/);
  });

  test("the search button is a 28px square whose icon draws at stroke-width 1.5", () => {
    const rule = ruleIn(css, ".header-tools-button");
    expect(rule).toMatch(/width:\s*28px/);
    expect(rule).toMatch(/height:\s*28px/);
    expect(rule).toMatch(/border-radius:\s*3px/);
    expect(ruleIn(css, ".header-tools-button .ico")).toMatch(/stroke-width:\s*1\.5/);
  });

  test("never redefines the shared .chip utility and never silences focus with outline: none", () => {
    expect(() => ruleIn(css, ".chip")).toThrow();
    expect(css).not.toMatch(/outline:\s*none/);
  });

  test("HeaderMenu's wrap no longer fights the trail for the row's one auto margin", () => {
    const headerMenuCss = readCss(import.meta.url, "HeaderMenu.css");
    expect(ruleIn(headerMenuCss, ".header-menu-wrap")).not.toMatch(/margin-left/);
  });
});
