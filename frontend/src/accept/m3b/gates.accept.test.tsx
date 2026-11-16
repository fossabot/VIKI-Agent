/** Acceptance (frozen): Vault tiles. */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { VaultTiles } from "../../components/RightRail/VaultTiles";

afterEach(cleanup);

/* AMENDMENT (2026-10-20). Retiring the retro
   feed retires GatesSection's own frozen cases too: its four tests and `gates` fixture are deleted
   here. Routines and vault-tile coverage below is unaffected. */

/* AMENDMENT (2026-10-20). The right rail's
   RoutinesSection is deleted: RoutinesRail replaced it, so
   its "routines list when and name per row, labelled planned" test and import are retired
   outright, and the header names the vault tiles only. Vault-tile coverage below is unaffected. */

test("vault tiles show the three counts; the inbox tile is hot only when it holds something", () => {
  const { container, rerender } = render(
    <VaultTiles counts={{ knowledge: 116, sources: 202, inbox: 1 }} />,
  );
  const tiles = [...container.querySelectorAll<HTMLElement>(".tile")];
  expect(tiles.map((t) => t.getAttribute("data-tile"))).toEqual(["knowledge", "sources", "inbox"]);
  expect(tiles.map((t) => t.querySelector(".tile-n")?.textContent)).toEqual(["116", "202", "1"]);
  expect(tiles.map((t) => t.querySelector(".tile-l")?.textContent)).toEqual([
    "knowledge",
    "sources",
    "inbox",
  ]);
  expect(tiles[2]?.classList.contains("tile-hot")).toBe(true);
  expect(tiles[0]?.classList.contains("tile-hot")).toBe(false);
  rerender(<VaultTiles counts={{ knowledge: 116, sources: 202, inbox: 0 }} />);
  expect(container.querySelector('[data-tile="inbox"]')?.classList.contains("tile-hot")).toBe(
    false,
  );
});
