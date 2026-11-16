/** While a chat's binding is not known, `Identity.unresolved` renders no assistant name and no
 * assistant face — at `Face`, `EmptyState`, `Reply` and `Composer` — even when `profile` or an
 * avatar id is also set. See identity.tsx's `unresolved` field. */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { AvatarsApi } from "../../clients/media/avatarsApi";
import type { Turn } from "../../state/view";
import { Composer } from "../Composer/Composer";
import { Reply } from "../Conversation/Conversation";
import { EmptyState } from "../Conversation/States";
import { Face } from "../Face/Face";
import { createAvatarUrls } from "./avatarUrls";
import { type BoundProfile, DEFAULT_IDENTITY, type Identity, IdentityContext } from "./identity";

afterEach(cleanup);

const PROFILE: BoundProfile = {
  name: "Planner",
  avatarId: "av1",
  accent: "orange",
  shape: "circle",
  revision: 3,
};

function withUnresolved(overrides: Partial<Identity> = {}): Identity {
  return { ...DEFAULT_IDENTITY, unresolved: true, ...overrides };
}

function turn(blocks: Turn["blocks"] = []): Turn {
  return {
    id: "t1",
    user: null,
    blocks,
    status: "success",
    usage: null,
    durationMs: 0,
    history: false,
  };
}

test("Face unresolved: no img, no /api/face/, no role=img, a neutral element, no avatar fetch", () => {
  const variant = vi.fn<AvatarsApi["variant"]>(async () => new Blob(["x"]));
  const avatars: AvatarsApi = { transform: vi.fn<AvatarsApi["transform"]>(), variant };
  const identity = withUnresolved({ assistantAvatarId: "av1", urls: createAvatarUrls(avatars) });
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector('[src^="/api/face/"]')).toBeNull();
  expect(container.querySelector('[role="img"]')).toBeNull();
  expect(container.querySelector(".face-neutral")).not.toBeNull();
  expect(variant).not.toHaveBeenCalled();
});

test("Face unresolved with a profile set too: still neutral, no profile Avatar", () => {
  const identity = withUnresolved({ profile: PROFILE });
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".avatar")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector(".face-neutral")).not.toBeNull();
});

test("EmptyState unresolved with a profile set too: the exact neutral sentence, no chips, no VIKI", () => {
  const identity = withUnresolved({ profile: PROFILE });
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <EmptyState />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".empty-sub")?.textContent).toBe(
    "Start talking. Use / to call a skill.",
  );
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.textContent).not.toContain("VIKI");
});

test("Reply unresolved with a profile set too: no .reply-name, no chip; the copy button still renders", () => {
  const identity = withUnresolved({ profile: PROFILE });
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Reply
        turn={turn([{ kind: "text", id: "b1", text: "hi", done: true }])}
        onOpenUrl={vi.fn()}
      />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector(".reply-name")).toBeNull();
  expect(container.querySelector(".chip")).toBeNull();
  expect(container.querySelector("button")).not.toBeNull();
});

test("Composer unresolved: the exact neutral placeholder", () => {
  const identity = withUnresolved();
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Composer running={false} onSend={vi.fn()} onStop={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector("textarea")?.getAttribute("placeholder")).toBe(
    "Talk… / for skills",
  );
});

test("resolved identity shows its name without a default portrait", () => {
  const identity: Identity = { ...DEFAULT_IDENTITY, unresolved: false, assistantName: "Aoi" };

  const face = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(face.container.querySelector(".face")).toBeNull();
  face.unmount();

  const empty = render(
    <IdentityContext.Provider value={identity}>
      <EmptyState />
    </IdentityContext.Provider>,
  );
  expect(empty.container.querySelector(".empty-sub")?.textContent).toBe(
    "Talk to Aoi. Use / to call a skill.",
  );
  empty.unmount();

  const reply = render(
    <IdentityContext.Provider value={identity}>
      <Reply turn={turn()} onOpenUrl={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(reply.container.querySelector(".reply-name")?.textContent).toBe("Aoi");
  reply.unmount();

  const composer = render(
    <IdentityContext.Provider value={identity}>
      <Composer running={false} onSend={vi.fn()} onStop={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(composer.container.querySelector("textarea")?.getAttribute("placeholder")).toBe(
    "Talk to Aoi… / for skills",
  );
});
