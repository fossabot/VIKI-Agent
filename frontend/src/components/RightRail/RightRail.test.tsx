import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { readCss } from "../../testing/css";
import { RailSection } from "./RailSection";
import { RightRail } from "./RightRail";
import { VaultTiles } from "./VaultTiles";

afterEach(cleanup);

// jsdom applies no CSS from stylesheets, so this checks the source.
const css = readCss(import.meta.url, "RightRail.css");
const drawerCss = readCss(import.meta.url, "../Shell/RailDrawer.css");

test("Routines gets a 16px gap, not the default 18px .rail-section margin", () => {
  const rule = css.match(/\.rail-section--routines\s*\{[^}]*\}/)?.[0];
  expect(rule).toMatch(/margin-top:\s*16px/);
});

test("the rail keeps the shell's --g0 under the presence ring (the design's #0B0B0B there)", () => {
  expect(css).not.toMatch(/--face-ring/);
});

test("100 plain rows retain summary semantics outside the detail scroller", () => {
  const onOpen = vi.fn();
  const rows = Array.from({ length: 100 }, (_, index) => `Row ${index}`);
  const { container } = render(
    <RightRail
      summary={
        <>
          <VaultTiles counts={{ knowledge: 7, sources: 8, inbox: 9 }} onOpen={onOpen} />
          <p>3 native skills · partial catalog</p>
        </>
      }
    >
      <RailSection title="Needs you" count={100}>
        {rows.map((row) => (
          <div className="row" key={row}>
            {row}
          </div>
        ))}
      </RailSection>
    </RightRail>,
  );
  const summary = container.querySelector(".right-rail-summary");
  const details = container.querySelector(".right-rail-details");
  expect(summary).not.toBeNull();
  expect(details).not.toBeNull();
  expect(details?.querySelectorAll(".row")).toHaveLength(100);
  expect(details?.querySelector(".rail-section-count")?.textContent).toBe("100");
  expect(details?.contains(summary)).toBe(false);
  expect(summary?.textContent).toContain("3 native skills · partial catalog");
  const tiles = [...(summary?.querySelectorAll<HTMLElement>("[data-tile]") ?? [])];
  expect(tiles.map((tile) => tile.dataset.tile)).toEqual(["knowledge", "sources", "inbox"]);
  expect(tiles.map((tile) => tile.querySelector(".tile-n")?.textContent)).toEqual(["7", "8", "9"]);
  for (const tile of tiles) fireEvent.click(tile);
  expect(onOpen.mock.calls.map(([ref]) => ref)).toEqual(["30-knowledge", "40-sources", "00-inbox"]);
});

test("pinned content sits between the header and detail scroller", () => {
  const { container } = render(
    <RightRail pinned={<button type="button">Pinned</button>}>
      <p>Details</p>
    </RightRail>,
  );
  const rail = container.querySelector(".right-rail");
  const header = rail?.querySelector(".right-rail-header");
  const pinned = rail?.querySelector(".right-rail-pinned");
  const details = rail?.querySelector(".right-rail-details");
  expect(header?.nextElementSibling ?? null).toBe(pinned ?? null);
  expect(pinned?.nextElementSibling ?? null).toBe(details ?? null);
  expect(details?.contains(pinned ?? null)).toBe(false);
  const pinnedRule = css.match(/\.right-rail-pinned\s*\{[^}]*\}/)?.[0];
  expect(pinnedRule).toMatch(/max-height:\s*40%/);
  expect(pinnedRule).toMatch(/overflow-y:\s*auto/);
});

test("details and the footer summary can scroll within the rail", () => {
  const rail = css.match(/\.right-rail\s*\{[^}]*\}/)?.[0];
  const details = css.match(/\.right-rail-details\s*\{[^}]*\}/)?.[0];
  const summary = css.match(/\.right-rail-summary\s*\{[^}]*\}/)?.[0];
  expect(rail).toMatch(/min-height:\s*0/);
  expect(rail).toMatch(/overflow:\s*hidden/);
  expect(details).toMatch(/flex:\s*1/);
  expect(details).toMatch(/min-height:\s*0/);
  expect(details).toMatch(/overflow-y:\s*auto/);
  expect(summary).toMatch(/flex:\s*0\s+1\s+auto/);
  expect(summary).toMatch(/min-height:\s*0/);
  expect(summary).toMatch(/overflow-y:\s*auto/);
});

test("the mobile drawer scrolls the right rail at its natural height", () => {
  expect(css).not.toMatch(/\.rail-drawer\s+\.right-rail(?:\[data-region="right-rail"\])?\s*\{/);

  const drawer = drawerCss.match(/\.rail-drawer\s*\{[^}]*\}/)?.[0];
  const rightRail = [...drawerCss.matchAll(/\.rail-drawer\s+\.right-rail\s*\{[^}]*\}/g)].at(
    -1,
  )?.[0];
  expect(drawer).toMatch(/overflow-y:\s*auto/);
  expect(rightRail).toMatch(/flex:\s*none/);
  const pinned = css.match(/\.rail-drawer\s+\.right-rail-pinned\s*\{[^}]*\}/)?.[0];
  expect(pinned).toMatch(/max-height:\s*none/);
  expect(pinned).toMatch(/overflow:\s*visible/);
});
