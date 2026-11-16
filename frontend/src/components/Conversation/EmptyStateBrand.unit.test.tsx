import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { readCss } from "../../testing/css";
import { DEFAULT_IDENTITY, type Identity, IdentityContext } from "../Avatar/identity";
import { EmptyState } from "./States";

afterEach(cleanup);

const BOUND: Identity = {
  ...DEFAULT_IDENTITY,
  assistantName: "Planner",
  profile: {
    name: "Planner",
    avatarId: null,
    accent: "orange",
    shape: "circle",
    revision: 3,
  },
};

test("new-chat app branding keeps the greeting and omits the old portrait", () => {
  const { container, getByRole, getByText } = render(<EmptyState />);
  expect(getByText("VIKI").closest(".empty-brand")).not.toBeNull();
  expect(getByText("AGENT").closest(".empty-brand")).not.toBeNull();
  expect(container.querySelector(".empty-wordmark")?.textContent).toBe("VIKIAGENT");
  expect(getByRole("img", { name: "VIKI Agent" }).className).toBe("empty-wordmark");
  expect(container.querySelector(".empty-wordmark svg.tile-wordmark-large")).not.toBeNull();
  expect(getByText("Virtual Interactive Kinetic Intelligence")).toBeTruthy();
  expect(container.querySelector(".face")).toBeNull();
  expect(container.querySelector(".empty-title")?.textContent).toBe("New conversation");
  expect(container.querySelector(".empty-sub")?.textContent).toBe(
    "Talk to VIKI. Use / to call a skill.",
  );
  expect(container.textContent).not.toContain("You are talking to an AI assistant.");
  expect(container.querySelector("button")).toBeNull();
});

test("a plain profile keeps the app mark with no portrait row or chips", () => {
  const { container } = render(
    <IdentityContext.Provider value={BOUND}>
      <EmptyState />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".empty-sub")?.textContent).toBe(
    "Talk to Planner. Use / to call a skill.",
  );
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.querySelector(".face")).toBeNull();
  expect(container.querySelector(".empty-brand-face")).toBeNull();
  expect(container.querySelector(".empty-wordmark")?.textContent).toBe("VIKIAGENT");
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).not.toContain("You are talking to an AI assistant.");
  expect(container.querySelector("button")).toBeNull();
  expect(container.querySelector(".empty")?.lastElementChild?.className).toBe("empty-brand");
});

test("a Bot chat shows its face in the mark's slot inside the full brand", () => {
  const { container, getByText } = render(
    <IdentityContext.Provider
      value={{
        ...BOUND,
        profile: { ...BOUND.profile!, botProfileId: "p1" },
      }}
    >
      <EmptyState />
    </IdentityContext.Provider>,
  );
  const brand = container.querySelector(".empty-brand");
  const faceBox = brand?.firstElementChild;
  expect(faceBox?.className).toBe("empty-brand-face");
  expect(faceBox?.getAttribute("aria-hidden")).toBe("true");
  expect(brand?.querySelectorAll(".face")).toHaveLength(1);
  expect(faceBox?.querySelector(".face")?.getAttribute("style")).toContain("width: 160px");
  expect(faceBox?.querySelector(".face")?.getAttribute("style")).toContain("height: 160px");
  expect(faceBox?.querySelector("svg.bot-icon")).not.toBeNull();
  expect(getByText("VIKI").closest(".empty-brand")).not.toBeNull();
  expect(getByText("AGENT").closest(".empty-brand")).not.toBeNull();
  expect(brand?.querySelector(".empty-wordmark")?.textContent).toBe("VIKIAGENT");
  expect(brand?.querySelector(".empty-acronym")?.textContent).toBe(
    "Virtual Interactive Kinetic Intelligence",
  );
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.querySelector(".empty-sub")?.textContent).toBe(
    "Talk to Planner. Use / to call a skill.",
  );
});

test("unresolved identity retains its neutral greeting and does not introduce app branding", () => {
  const { container } = render(
    <IdentityContext.Provider value={{ ...BOUND, unresolved: true }}>
      <EmptyState />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".empty-sub")?.textContent).toBe(
    "Start talking. Use / to call a skill.",
  );
  expect(container.querySelector(".face-neutral")).not.toBeNull();
  expect(container.querySelector(".empty-brand")).toBeNull();
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(container.textContent).not.toContain("You are talking to an AI assistant.");
  expect(container.querySelector("button")).toBeNull();
  expect(container.textContent).not.toContain("VIKI");
});

test("landing sessions use exact IDs, bounded actual rows and distinct tile selectors", () => {
  const onOpenSession = vi.fn();
  const { container, getByRole, getByText } = render(
    <EmptyState
      landingSessions={Array.from({ length: 8 }, (_, index) => ({
        sessionId: `session-${index}`,
        title: index === 0 ? "<script>sample</script>" : `Session ${index}`,
        pinned: index === 0,
        description: "Local session",
      }))}
      onOpenSession={onOpenSession}
    />,
  );
  expect(getByText("Pinned")).toBeTruthy();
  expect(getByText("Recent sessions")).toBeTruthy();
  expect(container.querySelectorAll("[data-landing-session-id]")).toHaveLength(6);
  expect(container.querySelector("[data-session-id]")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  fireEvent.click(getByRole("button", { name: "<script>sample</script>, Local session" }));
  expect(onOpenSession).toHaveBeenCalledExactlyOnceWith("session-0");
});

test("unresolved identity omits session tiles", () => {
  const { container } = render(
    <IdentityContext.Provider value={{ ...BOUND, unresolved: true }}>
      <EmptyState
        landingSessions={[{ sessionId: "existing", title: "Existing session" }]}
        onOpenSession={vi.fn()}
      />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector("button")).toBeNull();
  expect(container.querySelector(".empty-interactive")).toBeNull();
});

test("fresh Bot composer cap stays in its own split pane", () => {
  const css = readCss(import.meta.url, "States.css").replace(/\/\*[\s\S]*?\*\//g, "");
  const selector = css.match(/([^{}]+)\{\s*max-height:\s*54px;\s*\}/)?.[1]?.trim();
  if (!selector) throw new Error("Fresh Bot composer cap rule is missing");
  const { container } = render(
    <main className="conversation conversation-panes">
      <section className="conversation pane">
        <div className="conversation-scroll">
          <div className="empty-brand-face" />
        </div>
        <div className="composer-slot">
          <textarea className="composer-box" data-editor="bot" />
        </div>
      </section>
      <section className="conversation pane">
        <div className="conversation-scroll">
          <div className="empty-brand" />
        </div>
        <div className="composer-slot">
          <textarea className="composer-box" data-editor="plain-profile" />
        </div>
      </section>
      <section className="conversation pane">
        <div className="conversation-scroll">
          <div className="empty" />
        </div>
        <div className="composer-slot">
          <textarea className="composer-box" data-editor="unbound" />
        </div>
      </section>
    </main>,
  );
  expect(container.querySelectorAll(".composer-box")).toHaveLength(3);
  expect(
    [...container.querySelectorAll<HTMLTextAreaElement>(selector)].map(
      (element) => element.dataset.editor,
    ),
  ).toEqual(["bot"]);
});

test("wide landing rules apply only to the interactive main landing; compact tiles are opt-in", () => {
  const css = readCss(import.meta.url, "States.css");
  expect(css).not.toContain(["empty-brand", "bound"].join("-"));
  expect(css).not.toContain(".message-column:has(> .empty > .empty-brand");
  expect(css).toContain(".message-column:has(> .empty.empty-interactive > .empty-brand");
  expect(css).toContain('html[data-density="compact"] .empty-session-desk');
  expect(ruleIn(css, ".empty-brand:has(> .empty-brand-face)::before")).toMatch(/display:\s*none/);
  expect(ruleIn(css, ".empty-brand-face")).toMatch(/width:\s*clamp\(96px, 16cqi, 160px\)/);
  expect(ruleIn(css, ".empty-brand-face")).toMatch(/height:\s*clamp\(96px, 16cqi, 160px\)/);
  expect(ruleIn(css, ".empty-brand-face .face, .empty-brand-face .avatar")).toMatch(
    /max-width:\s*100%/,
  );
  expect(ruleIn(css, ".empty-brand-face .face, .empty-brand-face .avatar")).toMatch(
    /max-height:\s*100%/,
  );
});

function ruleIn(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return css.replace(/\s+/g, " ").match(new RegExp(`${escaped}\\s*\\{[^}]*\\}`))?.[0] ?? "";
}
