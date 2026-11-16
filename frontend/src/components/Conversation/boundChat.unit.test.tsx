/** Bound-chat presentation (a profile pinned to this session): the header and every reply row show the pinned revision's avatar (or initials on a failed read), name, `rN` and a `Profile` badge, never VIKI's face or name; the empty state names the profile in its greeting only. A Bot's chat drops the header chips beside its Bot chat bar and the reply badges (ConversationHeader.unit, Conversation.unit). */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { Turn } from "../../state/view";
import type { AvatarUrls } from "../Avatar/avatarUrls";
import { DEFAULT_IDENTITY, type Identity, IdentityContext } from "../Avatar/identity";
import { Face } from "../Face/Face";
import { Reply } from "./Conversation";
import { ConversationHeader } from "./ConversationHeader";
import { EmptyState } from "./States";

afterEach(cleanup);

function failedUrls(): AvatarUrls {
  return { acquire: () => () => {}, get: () => "failed", dispose: () => {} };
}

function boundIdentity(): Identity {
  return {
    ...DEFAULT_IDENTITY,
    assistantName: "Planner",
    urls: failedUrls(),
    profile: { name: "Planner", avatarId: "av1", accent: "orange", shape: "circle", revision: 3 },
  };
}

function turn(): Turn {
  return {
    id: "t1",
    user: null,
    blocks: [],
    status: "success",
    usage: null,
    durationMs: 0,
    history: false,
  };
}

function noApiFaceImg(container: HTMLElement): boolean {
  return Array.from(container.querySelectorAll("img")).every(
    (img) => !(img.getAttribute("src") ?? "").startsWith("/api/face/"),
  );
}

test("EmptyState under a bound profile names it in the greeting only, never /api/face/", () => {
  const { container } = render(
    <IdentityContext.Provider value={boundIdentity()}>
      <EmptyState />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".empty-sub")?.textContent).toBe(
    "Talk to Planner. Use / to call a skill.",
  );
  expect(container.querySelector(".avatar-initials")).toBeNull();
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.textContent).not.toContain("r3");
  expect(container.textContent).not.toContain("Profile");
  expect(noApiFaceImg(container)).toBe(true);
});

test("ConversationHeader under a bound profile: a name·revision chip and a Profile chip", () => {
  const { container } = render(
    <IdentityContext.Provider value={boundIdentity()}>
      <ConversationHeader title="Untitled session" />
    </IdentityContext.Provider>,
  );
  const chips = Array.from(container.querySelectorAll(".chip")).map((el) => el.textContent);
  expect(chips).toContain("Planner · r3");
  expect(chips).toContain("Profile");
  expect(noApiFaceImg(container)).toBe(true);
});

test("every reply row under a bound profile: initials, r3, Profile; never .reply-name VIKI", () => {
  const { container } = render(
    <IdentityContext.Provider value={boundIdentity()}>
      <Reply turn={turn()} onOpenUrl={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".reply-name")?.textContent).toBe("Planner");
  expect(container.querySelector(".reply-name")?.textContent).not.toBe("VIKI");
  expect(container.querySelector(".avatar-initials")?.textContent).toBe("P");
  const chips = Array.from(container.querySelectorAll(".chip")).map((el) => el.textContent);
  expect(chips).toContain("r3");
  expect(chips).toContain("Profile");
  expect(noApiFaceImg(container)).toBe(true);
});

test("Face under a bound profile never renders /api/face/, even on a failed read", () => {
  const { container } = render(
    <IdentityContext.Provider value={boundIdentity()}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector(".avatar-initials")?.textContent).toBe("P");
});
