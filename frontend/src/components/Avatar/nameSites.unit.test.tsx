/** The chrome's name sites read `identity.assistantName`: the top bar wordmark, the right rail
 * name, the agents section row, the search palette's assistant chip and the composer's
 * placeholder. With DEFAULT_IDENTITY the top bar names the app "VIKI Agent"; the other sites
 * say "VIKI". A custom assistant's name stays "Aoi" at every site. */
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { SearchHit, SearchResult } from "../../protocol";
import { Composer } from "../Composer/Composer";
import { AgentsSection } from "../LeftRail/AgentsSection";
import { RightRail } from "../RightRail/RightRail";
import { SearchPalette } from "../Search/SearchPalette";
import { TopBar } from "../TopBar/TopBar";
import { DEFAULT_IDENTITY, type Identity, IdentityContext } from "./identity";

afterEach(cleanup);

const AOI: Identity = { ...DEFAULT_IDENTITY, assistantName: "Aoi" };

function withIdentity(identity: Identity, node: ReactElement) {
  return <IdentityContext.Provider value={identity}>{node}</IdentityContext.Provider>;
}

test("TopBar wordmark: VIKI Agent's tile lettering by default, custom and profile names as text", () => {
  const { container, rerender } = render(<TopBar onNewSession={() => {}} />);
  const wordmark = () => container.querySelector('[data-region="wordmark"]');
  expect(wordmark()?.textContent).toBe("VIKI Agent");
  expect(wordmark()?.querySelector("svg.tile-wordmark-small")).not.toBeNull();
  rerender(withIdentity(AOI, <TopBar onNewSession={() => {}} />));
  expect(wordmark()?.textContent).toBe("Aoi");
  expect(wordmark()?.querySelector("svg")).toBeNull();
  rerender(
    withIdentity(
      {
        ...DEFAULT_IDENTITY,
        profile: {
          name: "VIKI",
          avatarId: null,
          accent: "orange",
          shape: "circle",
          revision: 1,
        },
      },
      <TopBar onNewSession={() => {}} />,
    ),
  );
  expect(wordmark()?.textContent).toBe("VIKI");
  expect(wordmark()?.querySelector("svg")).toBeNull();
});

test("RightRail name: VIKI with no provider, the assistant name under one", () => {
  const { container, rerender } = render(<RightRail />);
  expect(container.querySelector(".identity-name")?.textContent).toBe("VIKI");
  rerender(withIdentity(AOI, <RightRail />));
  expect(container.querySelector(".identity-name")?.textContent).toBe("Aoi");
});

test("AgentsSection row: VIKI with no provider, the assistant name under one; data-agent stays VIKI", () => {
  const { container, rerender } = render(
    <AgentsSection agents={[]} running={[]} vikiOnline={false} nowMs={0} />,
  );
  const row = () => container.querySelector('[data-agent="VIKI"]');
  expect(row()?.textContent).toBe("VIKI");
  rerender(
    withIdentity(AOI, <AgentsSection agents={[]} running={[]} vikiOnline={false} nowMs={0} />),
  );
  expect(row()?.getAttribute("data-agent")).toBe("VIKI");
  expect(row()?.textContent).toBe("Aoi");
});

test("SearchPalette assistant chip: VIKI with no provider, the assistant name under one", async () => {
  const hits: SearchHit[] = [
    { session_id: "s1", title: "T1", role: "assistant", snippet: "", at: "" },
  ];
  const result: SearchResult = {
    hits,
    state: "ready",
    reason: "",
    scanned: 1,
    total: 1,
    request_id: "",
  };
  const search = vi.fn(() => Promise.resolve(result));

  vi.useFakeTimers();
  try {
    const { container } = render(
      <SearchPalette open search={search} onOpen={() => {}} onClose={() => {}} nowMs={0} />,
    );
    const input = container.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "milk" } });
    act(() => void vi.advanceTimersByTime(150));
    await act(async () => {
      await Promise.resolve();
    });
    expect(container.querySelector(".chip.dim")?.textContent).toBe("VIKI");
    cleanup();
  } finally {
    vi.useRealTimers();
  }

  vi.useFakeTimers();
  try {
    const { container: aoiContainer } = render(
      withIdentity(
        AOI,
        <SearchPalette open search={search} onOpen={() => {}} onClose={() => {}} nowMs={0} />,
      ),
    );
    const aoiInput = aoiContainer.querySelector("input") as HTMLInputElement;
    fireEvent.change(aoiInput, { target: { value: "milk" } });
    act(() => void vi.advanceTimersByTime(150));
    await act(async () => {
      await Promise.resolve();
    });
    expect(aoiContainer.querySelector(".chip.dim")?.textContent).toBe("Aoi");
  } finally {
    vi.useRealTimers();
  }
});

test("Composer placeholder: VIKI with no provider, the assistant name under one", () => {
  const { container, rerender } = render(
    <Composer running={false} onSend={vi.fn()} onStop={vi.fn()} />,
  );
  const box = () => container.querySelector("textarea");
  expect(box()?.getAttribute("placeholder")).toBe("Talk to VIKI… / for skills");
  rerender(withIdentity(AOI, <Composer running={false} onSend={vi.fn()} onStop={vi.fn()} />));
  expect(box()?.getAttribute("placeholder")).toBe("Talk to Aoi… / for skills");
});
