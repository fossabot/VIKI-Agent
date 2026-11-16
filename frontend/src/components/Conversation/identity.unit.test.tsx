import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { Turn } from "../../state/view";
import type { AvatarUrls } from "../Avatar/avatarUrls";
import { DEFAULT_IDENTITY, type Identity, IdentityContext } from "../Avatar/identity";
import { Face } from "../Face/Face";
import { Reply, UserTurn } from "./Conversation";
import { EmptyState } from "./States";

afterEach(cleanup);

const AT = "2026-09-12T09:05:00.000Z";
const NOW = Date.parse(AT);

function fakeUrls(state: string | null | "failed"): AvatarUrls {
  return { acquire: () => () => {}, get: () => state, dispose: () => {} };
}

function withIdentity(overrides: Partial<Identity>): Identity {
  return { ...DEFAULT_IDENTITY, ...overrides };
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

test("with a provider, the reply name and the empty-state sentence use the assistant's name", () => {
  const identity = withIdentity({ assistantName: "Aoi" });
  const reply = render(
    <IdentityContext.Provider value={identity}>
      <Reply turn={turn()} onOpenUrl={vi.fn()} />
    </IdentityContext.Provider>,
  );
  expect(reply.container.querySelector(".reply-name")?.textContent).toBe("Aoi");
  reply.unmount();

  const empty = render(
    <IdentityContext.Provider value={identity}>
      <EmptyState />
    </IdentityContext.Provider>,
  );
  expect(empty.container.querySelector(".empty-sub")?.textContent).toBe(
    "Talk to Aoi. Use / to call a skill.",
  );
});

test("with a provider, Face shows the loaded object URL", () => {
  const identity = withIdentity({ assistantAvatarId: "av1", urls: fakeUrls("blob:aoi") });
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <Face size={36} />
    </IdentityContext.Provider>,
  );
  expect(container.querySelector("img")?.getAttribute("src")).toBe("blob:aoi");
});

test("with a provider, the user avatar renders as the next sibling after .user-at", () => {
  const identity = withIdentity({ userAvatarId: "av2", urls: fakeUrls("blob:user") });
  const { container } = render(
    <IdentityContext.Provider value={identity}>
      <UserTurn user={{ text: "hi", command: "", at: AT }} nowMs={NOW} userLabel="Robin" />
    </IdentityContext.Provider>,
  );
  const at = container.querySelector(".user-at");
  expect(at?.textContent).toMatch(/^Robin · \d\d:\d\d$/);
  const sibling = at?.nextElementSibling;
  expect(sibling?.classList.contains("avatar")).toBe(true);
  expect(sibling?.querySelector("img")?.getAttribute("src")).toBe("blob:user");
});

test("without a provider, the defaults render: VIKI with no assistant or user avatar", () => {
  const reply = render(<Reply turn={turn()} onOpenUrl={vi.fn()} />);
  expect(reply.container.querySelector(".reply-name")?.textContent).toBe("VIKI");
  reply.unmount();

  const face = render(<Face size={36} />);
  expect(face.container.querySelector(".face")).toBeNull();
  face.unmount();

  const userTurn = render(
    <UserTurn user={{ text: "hi", command: "", at: AT }} nowMs={NOW} userLabel="Robin" />,
  );
  expect(userTurn.container.querySelector(".avatar")).toBeNull();
});

test.each([null, "failed"] as const)(
  "an avatar read in state %s omits the old portrait",
  (state) => {
    const identity = withIdentity({ assistantAvatarId: "av1", urls: fakeUrls(state) });
    const { container } = render(
      <IdentityContext.Provider value={identity}>
        <Face size={36} />
      </IdentityContext.Provider>,
    );
    expect(container.querySelector(".face")).toBeNull();
  },
);
