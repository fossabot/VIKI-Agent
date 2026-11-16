/**
 * ConversationHeader's `tabs` mode and SessionTabs' `asTitle` mode (the tab strip standing in
 * for the title row), plus the CSS rules that let the strip shrink and scroll in that row and let
 * the selected tab read in full while the row has room.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SessionMetadata } from "../../clients/conversations/sessionMetadataApi";
import { readCss, ruleIn } from "../../testing/css";
import { DEFAULT_IDENTITY, IdentityContext } from "../Avatar/identity";
import { SessionTabs } from "../Sessions/Sessions";
import { ConversationHeader } from "./ConversationHeader";

afterEach(cleanup);

test("given tabs, the strip takes the title's place and no root-label renders even with a folder", () => {
  const { container } = render(
    <ConversationHeader
      title="Chat"
      folder="code"
      tabs={<div className="fake-strip">strip</div>}
    />,
  );
  expect(container.querySelector(".fake-strip")?.textContent).toBe("strip");
  expect(container.querySelector(".conversation-title")).toBeNull();
  expect(container.querySelector(".root-label")).toBeNull();
});

const boundIdentity = {
  ...DEFAULT_IDENTITY,
  profile: {
    name: "Planner",
    avatarId: null,
    accent: "blue" as const,
    shape: "circle" as const,
    revision: 3,
  },
};

test("a bound profile shows the name·revision and Profile chips by default", () => {
  const { container } = render(
    <IdentityContext.Provider value={boundIdentity}>
      <ConversationHeader title="Chat" />
    </IdentityContext.Provider>,
  );
  expect(Array.from(container.querySelectorAll(".chip")).map((chip) => chip.textContent)).toEqual([
    "Planner · r3",
    "Profile",
  ]);
});

test("profileChips={false} drops both chips", () => {
  const { container } = render(
    <IdentityContext.Provider
      value={{ ...boundIdentity, profile: { ...boundIdentity.profile, botProfileId: "p1" } }}
    >
      <ConversationHeader title="Chat" profileChips={false} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelectorAll(".chip")).toHaveLength(0);
});

const first: SessionMetadata = {
  session_id: "a",
  revision: 1,
  title: "Chat",
  pinned: false,
  archived: false,
};
const second: SessionMetadata = {
  session_id: "b",
  revision: 1,
  title: "Second",
  pinned: false,
  archived: false,
};

test("asTitle marks only the selected tab's label as the conversation title", () => {
  const { container, rerender } = render(
    <SessionTabs
      sessions={[first, second]}
      selectedId={first.session_id}
      onSelect={vi.fn()}
      onClose={vi.fn()}
      asTitle
    />,
  );
  const labels = () => Array.from(container.querySelectorAll(".session-tab-label"));
  expect(labels()[0]?.className).toBe("session-tab-label conversation-title");
  expect(labels()[1]?.className).toBe("session-tab-label");

  rerender(
    <SessionTabs
      sessions={[first, second]}
      selectedId={first.session_id}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  expect(labels()[0]?.className).toBe("session-tab-label");
});

test("the session-tabs strip shrinks and scrolls inside the header row before the chips do", () => {
  const css = readCss(import.meta.url, "Conversation.css");
  const rule = ruleIn(css, ".conversation-header .session-tabs");
  expect(rule).toMatch(/flex:\s*0 1 auto;/);
  expect(rule).toMatch(/padding:\s*0;/);
  const tabs = ruleIn(readCss(import.meta.url, "../Sessions/Sessions.css"), ".session-tabs");
  expect(tabs).toMatch(/overflow-x:\s*auto;/);
});

test("the selected tab, standing in as the title, reads in full while the row has room", () => {
  const css = readCss(import.meta.url, "Conversation.css");
  const rule = ruleIn(css, '.conversation-header .session-tab[data-active="true"]');
  // Both selectors in the rule
  expect(css).toMatch(
    /\.conversation-header \.session-tab\[data-selected="true"\]\s*,\s*\.conversation-header \.session-tab\[data-active="true"\]/,
  );
  expect(rule).toMatch(/flex:\s*0 1 auto;/);
  expect(rule).toMatch(/max-width:\s*min\(480px, 100%\);/);
});

test("Conversation.css no longer defines a participants rule", () => {
  expect(readCss(import.meta.url, "Conversation.css")).not.toMatch(/\.participants/);
});
